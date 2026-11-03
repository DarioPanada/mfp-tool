"""The session lock and `mfp app-summary`. Temp token files and fake openers only, never the network."""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import http.client
import io
import json
import os
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from test_client import CSRF, SESSION, FakeMfp, FakeResponse, headers, logged_in

from mfp_tool import cli
from mfp_tool.cli import EXIT_ERROR, EXIT_EXPIRED, main
from mfp_tool.client import COOKIE, REQUEST_TIMEOUT_SECONDS, Client, MfpError, TokenStore

RUNNER = Path(__file__).with_name("two_process_run.py")
READ_DIARY = "/api/services/diary/read_diary"
SIGN_IN = {"unavailable": "MyFitnessPal needs you to sign in again"}
OLD_TOKEN = "old-token"
NEW_TOKEN = "new-token"


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    p = tmp_path / "session-token"
    p.write_text(f"{OLD_TOKEN}\n")
    p.chmod(0o600)
    return p


def lock_file(token_file: Path) -> Path:
    return token_file.with_name(token_file.name + ".lock")


@contextlib.contextmanager
def held_lock(token_file: Path) -> Iterator[None]:
    """The lock held through a file description of its own, as another process would hold it."""
    with lock_file(token_file).open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def lock_is_free(token_file: Path) -> bool:
    with lock_file(token_file).open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


@pytest.fixture
def short_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "LOCK_WAIT_SECONDS", 0.2, raising=False)
    monkeypatch.setattr(cli, "SUMMARY_LOCK_WAIT_SECONDS", 0.2, raising=False)


def no_network(req: urllib.request.Request, timeout: float) -> FakeResponse:
    raise AssertionError(f"called MFP: {req.full_url}")


# ---------- two real processes on one real token file ----------


class TwoProcesses:
    def __init__(self, tmp_path: Path) -> None:
        self.state = tmp_path / "mfp-state"
        self.state.mkdir()
        self.token_file = tmp_path / "session-token"
        self.token_file.write_text(OLD_TOKEN)
        self.token_file.chmod(0o600)
        (self.state / "valid").write_text(OLD_TOKEN)
        self.procs: list[subprocess.Popen[str]] = []

    def start(self, name: str, command: str, gate: str = "-", mode: str = "before-response") -> subprocess.Popen[str]:
        proc = subprocess.Popen(  # noqa: S603 - this interpreter and a script of this repo
            [sys.executable, str(RUNNER), name, str(self.state), gate, mode, command],
            env=os.environ | {"MFP_TOKEN_FILE": str(self.token_file)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.procs.append(proc)
        self.wait_for(f"{name}-started", proc)
        return proc

    def wait_for(self, marker: str, proc: subprocess.Popen[str]) -> None:
        deadline = time.monotonic() + 30
        while not (self.state / marker).exists():
            assert proc.poll() is None, proc.communicate()
            assert time.monotonic() < deadline, f"no {marker}"
            time.sleep(0.01)

    def release(self) -> None:
        (self.state / "release").touch()

    def log(self) -> list[list[str]]:
        path = self.state / "log"
        return [line.split() for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def two(tmp_path: Path) -> Iterator[TwoProcesses]:
    t = TwoProcesses(tmp_path)
    yield t
    t.release()
    for proc in t.procs:
        proc.kill()
        proc.communicate()


@pytest.mark.parametrize("gate", ["/api/auth/session", "/api/auth/csrf", READ_DIARY])
@pytest.mark.parametrize("second", ["app-summary", "diary"])
def test_second_run_waits_for_the_whole_first_run_then_reads_its_rotated_token(
    two: TwoProcesses, gate: str, second: str
) -> None:
    a = two.start("a", "app-summary", gate)
    two.wait_for("a-held", a)
    b = two.start("b", second)

    time.sleep(1)  # plenty for b to have read the token and sent a request, were it not waiting
    assert b.poll() is None
    assert [e for e in two.log() if e[0] == "b"] == []

    two.release()
    a_out, a_err = a.communicate(timeout=30)
    b_out, b_err = b.communicate(timeout=30)
    assert (a.returncode, b.returncode) == (0, 0), (a_err, b_err)

    log = two.log()
    b_first = next(i for i, e in enumerate(log) if e[0] == "b")
    assert all(e[0] == "a" for e in log[:b_first]) and all(e[0] == "b" for e in log[b_first:])
    assert log[b_first][2] == "a-rotated"  # the token a was given, which b read after a had finished
    assert two.token_file.read_text() == "b-rotated"
    assert "unavailable" not in a_out and "unavailable" not in b_out


def test_a_killed_run_frees_the_lock_and_keeps_a_cookie_whose_headers_had_arrived(two: TwoProcesses) -> None:
    a = two.start("a", "app-summary", "/api/auth/session", "in-body")
    two.wait_for("a-held", a)
    a.kill()
    a.communicate()
    assert two.token_file.read_text() == "a-rotated"

    b = two.start("b", "app-summary")
    out, _ = b.communicate(timeout=30)
    assert b.returncode == 0 and "headline" in json.loads(out)
    assert two.token_file.read_text() == "b-rotated"


def test_a_run_killed_before_the_response_arrives_can_still_lose_the_session(two: TwoProcesses) -> None:
    """What the lock can't do: MFP has spent the old token and the new one died on the wire."""
    a = two.start("a", "app-summary", "/api/auth/session", "before-response")
    two.wait_for("a-held", a)
    a.kill()
    a.communicate()
    assert two.token_file.read_text() == OLD_TOKEN

    b = two.start("b", "app-summary")
    out, _ = b.communicate(timeout=30)
    assert b.returncode == 0 and json.loads(out) == SIGN_IN


# ---------- the lock ----------

ARGS: dict[str, list[str]] = {
    "session": [],
    "diary": [],
    "search": ["x"],
    "log": ["1", "2", "--meal", "1", "--servings", "1", "--value", "1", "--unit", "g", "--multiplier", "1"],
    "delete": ["a"],
    "quick-add": ["--kcal", "1", "--meal", "1"],
    "exercises": [],
    "exercise-search": ["x"],
    "exercise-log": ["1", "--minutes", "1", "--kcal", "1"],
    "meals": [],
    "foods": [],
    "food-create": ["x", "--kcal", "1", "--value", "1", "--unit", "g"],
    "food-delete": ["a"],
    "weights": [],
    "weight-log": ["80", "--unit", "kg"],
    "weight-delete": ["a"],
    "app-summary": [],
}


def test_every_command_of_the_manifest_is_in_the_lock_tests() -> None:
    assert set(ARGS) == {str(c["name"]) for c in cli.COMMANDS}


@pytest.mark.parametrize("command", ARGS)
@pytest.mark.usefixtures("short_waits")
def test_every_command_gives_up_on_a_held_lock_without_touching_token_or_mfp(
    token_file: Path, command: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_load(self: TokenStore) -> str:
        raise AssertionError("read the token without the lock")

    monkeypatch.setattr(TokenStore, "load", must_not_load)
    with held_lock(token_file):
        status = main(["--token-file", str(token_file), command, *ARGS[command]], opener=no_network)
    captured = capsys.readouterr()
    assert status == cli.EXIT_BUSY
    assert status not in (0, EXIT_ERROR, EXIT_EXPIRED)  # EXIT_EXPIRED's 2 is also argparse's usage error
    assert captured.out == ""
    assert "another mfp run" in captured.err and "nothing was sent" in captured.err


@pytest.mark.parametrize("command", ["session", "app-summary"])
def test_the_token_is_read_after_the_lock_is_held(
    token_file: Path, command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_load = TokenStore.load
    free_at_load: list[bool] = []

    def load(self: TokenStore) -> str:
        free_at_load.append(lock_is_free(self.path))
        return real_load(self)

    monkeypatch.setattr(TokenStore, "load", load)
    assert main(["--token-file", str(token_file), command], opener=FakeMfp(logged_in(**{READ_DIARY: ([], [])}))) == 0
    assert free_at_load == [False]


def test_the_lock_is_held_during_every_request(token_file: Path) -> None:
    fake = FakeMfp(logged_in(**{READ_DIARY: ([], [])}))
    free: list[bool] = []

    def opener(req: urllib.request.Request, timeout: float) -> FakeResponse:
        free.append(lock_is_free(token_file))
        return fake(req, timeout)

    assert main(["--token-file", str(token_file), "app-summary"], opener=opener) == 0
    assert free == [False, False, False, False]


@pytest.mark.parametrize(
    "routes, status",
    [(logged_in(), 0), ({"/api/auth/session": (500, [])}, EXIT_ERROR), ({"/api/auth/session": ({}, [])}, EXIT_EXPIRED)],
)
def test_the_lock_is_free_after_a_run_however_it_ended(
    token_file: Path, routes: dict[str, tuple[Any, list[str]]], status: int
) -> None:
    assert main(["--token-file", str(token_file), "session"], opener=FakeMfp(routes)) == status
    assert lock_is_free(token_file)


@pytest.mark.parametrize("command", ["skill", "manifest"])
def test_commands_that_dont_talk_to_mfp_take_no_lock(token_file: Path, command: str) -> None:
    with held_lock(token_file):
        assert main(["--token-file", str(token_file), command], opener=no_network) == 0


def test_skill_and_manifest_leave_no_lock_file(token_file: Path) -> None:
    assert main(["--token-file", str(token_file), "manifest"], opener=no_network) == 0
    assert not lock_file(token_file).exists()


def test_lock_file_is_beside_the_token_owner_only_and_holds_nothing(token_file: Path) -> None:
    fake = FakeMfp(
        {"/api/auth/session": (SESSION, [f"{COOKIE}={NEW_TOKEN}"]), "/api/auth/csrf": (CSRF, [])},
    )
    assert main(["--token-file", str(token_file), "session"], opener=fake) == 0
    assert stat.S_IMODE(lock_file(token_file).stat().st_mode) == 0o600
    assert lock_file(token_file).read_bytes() == b""


def test_a_lock_that_cant_be_taken_never_means_running_unlocked(
    token_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock_file(token_file).mkdir()
    assert main(["--token-file", str(token_file), "session"], opener=no_network) == EXIT_ERROR
    assert "lock" in capsys.readouterr().err


def test_no_token_directory_is_an_expired_session(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "no-such-dir" / "session-token"
    assert main(["--token-file", str(missing), "session"], opener=no_network) == EXIT_EXPIRED
    capsys.readouterr()
    assert main(["--token-file", str(missing), "app-summary"], opener=no_network) == 0
    assert json.loads(capsys.readouterr().out) == SIGN_IN


def test_lock_wait_is_above_the_worst_full_run(token_file: Path) -> None:
    """The heaviest command is `log` by meal name on another day; see MOST_REQUESTS_IN_A_RUN."""
    diary = [{"id": "a", "meal_name": "Lunch", "meal_position": 2}]
    fake = FakeMfp(logged_in(**{READ_DIARY: (diary, []), "/api/services/diary": ([{"id": "e1"}], [])}))
    args = ["log", "1", "2", "--meal", "lunch", "--servings", "1", "--value", "1", "--unit", "g", "--multiplier", "1"]
    assert main(["--token-file", str(token_file), *args, "--date", "2026-01-02"], opener=fake) == 0
    assert len(fake.requests) == cli.MOST_REQUESTS_IN_A_RUN
    assert cli.LOCK_WAIT_SECONDS > cli.MOST_REQUESTS_IN_A_RUN * REQUEST_TIMEOUT_SECONDS


# ---------- app-summary against the cockpit's kill ----------


def test_every_request_of_a_summary_fits_before_the_hosts_kill() -> None:
    assert REQUEST_TIMEOUT_SECONDS == 30
    assert cli.SUMMARY_LAST_START_SECONDS + REQUEST_TIMEOUT_SECONDS < cli.HOST_KILL_SECONDS == 45
    assert cli.SUMMARY_LOCK_WAIT_SECONDS < cli.SUMMARY_LAST_START_SECONDS


def test_requests_use_the_timeout(token_file: Path) -> None:
    fake = FakeMfp(logged_in(**{READ_DIARY: ([], [])}))
    timeouts: list[float] = []

    def opener(req: urllib.request.Request, timeout: float) -> FakeResponse:
        timeouts.append(timeout)
        return fake(req, timeout)

    assert main(["--token-file", str(token_file), "app-summary"], opener=opener) == 0
    assert timeouts == [30, 30, 30, 30]


def test_a_summary_past_its_last_start_sends_nothing(
    token_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "SUMMARY_LAST_START_SECONDS", -1, raising=False)
    assert main(["--token-file", str(token_file), "app-summary"], opener=no_network) == 0
    assert "unavailable" in json.loads(capsys.readouterr().out)
    assert token_file.read_text().strip() == OLD_TOKEN


def test_a_slow_summary_stops_before_the_request_it_couldnt_finish(
    token_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "SUMMARY_LAST_START_SECONDS", 0.05, raising=False)
    fake = FakeMfp(logged_in(**{READ_DIARY: ([], [])}))

    def slow(req: urllib.request.Request, timeout: float) -> FakeResponse:
        time.sleep(0.1)
        return fake(req, timeout)

    assert main(["--token-file", str(token_file), "app-summary"], opener=slow) == 0
    assert len(fake.requests) == 1 and fake.requests[0].get_header("Cookie") is None  # the anonymous one only
    assert "unavailable" in json.loads(capsys.readouterr().out)


def test_other_commands_have_no_last_start(
    token_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "SUMMARY_LAST_START_SECONDS", -1, raising=False)
    fake = FakeMfp(logged_in(**{READ_DIARY: ([], [])}))
    assert main(["--token-file", str(token_file), "diary"], opener=fake) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_a_cookie_is_saved_when_only_the_body_is_lost(token_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    class BodyLost(FakeResponse):
        def read(self) -> bytes:
            raise TimeoutError("timed out")

    def opener(req: urllib.request.Request, timeout: float) -> FakeResponse:
        if f"{COOKIE}=" not in (req.get_header("Cookie") or ""):
            return FakeResponse({}, headers("anon-device-id=device"))
        return BodyLost({}, headers(f"{COOKIE}={NEW_TOKEN}"))

    assert main(["--token-file", str(token_file), "app-summary"], opener=opener) == 0
    assert token_file.read_text() == NEW_TOKEN
    assert "unavailable" in json.loads(capsys.readouterr().out)


# ---------- app-summary ----------


def food(meal: str, position: int | None, kcal: float | None, **more: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": "food_entry",
        "meal_name": meal,
        "food": {"description": "Some food"},
        "nutritional_contents": {"energy": {"unit": "calories", "value": kcal}},
    }
    if position is not None:
        entry["meal_position"] = position
    entry["nutritional_contents"] |= more
    return entry


def quick_add(meal: str, kcal: float, **more: Any) -> dict[str, Any]:
    """The shape `mfp quick-add` posts: no food. Whether MFP reads it back like this is unverified."""
    contents = {"energy": {"value": kcal, "unit": "calories"}} | more
    return {"type": "quick_add", "meal_name": meal, "nutritional_contents": contents}


def run_summary(token_file: Path, entries: Any, capsys: pytest.CaptureFixture[str]) -> Any:
    fake = FakeMfp(logged_in(**{READ_DIARY: (entries, [])}))
    assert main(["--token-file", str(token_file), "app-summary"], opener=fake) == 0
    return json.loads(capsys.readouterr().out)


DIARY = [
    food("Dinner", 4, 500.4, protein=30, carbohydrates=50.5, fat=20),
    food("Breakfast", 0, 300, protein=20.2, carbohydrates=None, fat=10),
    food("Breakfast", 0, 110, protein=None, carbohydrates=15),
    quick_add("Lunch", 620, protein=41.8),
    food("Lunch", 2, None),  # gives Lunch its place in the diary, adds nothing
    food("Dinner", 4, 9000, protein=900) | {"deleted": True},
    quick_add("Lunch", 7000, fat=700) | {"deleted": True},
    {"type": "exercise_entry", "meal_name": "Dinner", "nutritional_contents": {"energy": {"value": 800}}},
    {"type": "steps_aggregate", "steps": 12000},
    food("Dinner", 4, 700) | {"type": "water_entry"},
]


def test_summary_counts_live_food_and_quick_adds_only(token_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    summary = run_summary(token_file, DIARY, capsys)
    as_of = summary.pop("asOf")
    assert summary == {
        "headline": {"value": "1,530 kcal", "label": "Eaten today"},
        "note": "Protein 92 g, carbs 66 g, fat 30 g.",
        "rows": [
            {"label": "Breakfast", "value": "410"},
            {"label": "Lunch", "value": "620"},
            {"label": "Dinner", "value": "500"},
        ],
    }
    parsed = datetime.datetime.fromisoformat(as_of)
    assert parsed.utcoffset() is not None
    assert abs((datetime.datetime.now(datetime.UTC) - parsed).total_seconds()) < 60


def test_summary_note_is_a_plain_sentence(token_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    note = run_summary(token_file, DIARY, capsys)["note"]
    assert "·" not in note and "•" not in note and note.endswith(".")


def test_summary_has_no_exercise_weight_or_goal(token_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    summary = run_summary(token_file, DIARY, capsys)
    assert set(summary) == {"headline", "note", "rows", "asOf"}
    text = json.dumps(summary).casefold()
    assert not any(word in text for word in ("exercise", "weigh", "goal", "burned", "steps", "someone"))


def test_summary_makes_as_few_requests_as_a_diary_read_needs(token_file: Path) -> None:
    fake = FakeMfp(logged_in(**{READ_DIARY: ([], [])}))
    assert main(["--token-file", str(token_file), "app-summary"], opener=fake) == 0
    paths = [r.full_url.split("myfitnesspal.com", 1)[1].split("?", 1)[0] for r in fake.requests]
    assert paths == ["/api/auth/session", "/api/auth/session", "/api/auth/csrf", READ_DIARY]
    today = datetime.date.today().isoformat()
    assert f"entry_date={today}" in fake.requests[-1].full_url


def test_summary_of_an_empty_diary(token_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    summary = run_summary(token_file, [], capsys)
    assert summary["headline"]["value"] == "0 kcal"
    assert summary["note"] == "Protein 0 g, carbs 0 g, fat 0 g."
    assert summary["rows"] == []


def test_summary_puts_a_meal_with_no_known_position_last_in_the_order_seen(
    token_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    entries = [quick_add("Snack 2", 50), quick_add("Elevenses", 60), food("Dinner", 4, 500), food("Breakfast", 0, 100)]
    rows = run_summary(token_file, entries, capsys)["rows"]
    assert [r["label"] for r in rows] == ["Breakfast", "Dinner", "Snack 2", "Elevenses"]


def test_summary_counts_an_entry_with_no_type(token_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The diary was asked for food entries, and whether every one carries `type` is unverified."""
    entry = food("Lunch", 2, 123)
    del entry["type"]
    assert run_summary(token_file, [entry], capsys)["headline"]["value"] == "123 kcal"


def test_summary_has_at_most_eight_rows(token_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    entries = [food(f"Meal {n}", n, 10) for n in range(10)]
    summary = run_summary(token_file, entries, capsys)
    assert len(summary["rows"]) == 8
    assert summary["headline"]["value"] == "100 kcal"


def test_summary_shows_thousands_in_a_row(token_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_summary(token_file, [food("Dinner", 4, 1234)], capsys)["rows"] == [{"label": "Dinner", "value": "1,234"}]


# ---------- unavailable ----------


def raising(error: BaseException) -> Callable[[urllib.request.Request, float], FakeResponse]:
    def opener(req: urllib.request.Request, timeout: float) -> FakeResponse:
        raise error

    return opener


UNREACHABLE = [
    urllib.error.URLError("Name or service not known"),
    TimeoutError("timed out"),
    ConnectionResetError("reset"),
    http.client.RemoteDisconnected("closed"),
    http.client.IncompleteRead(b"partial"),
]


def assert_unreachable(capsys: pytest.CaptureFixture[str]) -> None:
    captured = capsys.readouterr()
    assert list(json.loads(captured.out)) == ["unavailable"]
    assert json.loads(captured.out) != SIGN_IN
    assert "MyFitnessPal" in captured.out
    assert captured.err == ""


@pytest.mark.parametrize("error", UNREACHABLE, ids=lambda e: type(e).__name__)
def test_unreachable_is_unavailable_with_exit_0(
    token_file: Path, error: Exception, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--token-file", str(token_file), "app-summary"], opener=raising(error)) == 0
    assert_unreachable(capsys)


def test_unreachable_partway_through_is_unavailable_and_keeps_the_new_cookie(
    token_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeMfp({"/api/auth/session": (SESSION, [f"{COOKIE}={NEW_TOKEN}"]), "/api/auth/csrf": (CSRF, [])})

    def opener(req: urllib.request.Request, timeout: float) -> FakeResponse:
        if READ_DIARY in req.full_url:
            raise TimeoutError("timed out")
        return fake(req, timeout)

    assert main(["--token-file", str(token_file), "app-summary"], opener=opener) == 0
    assert_unreachable(capsys)
    assert token_file.read_text() == NEW_TOKEN


@pytest.mark.parametrize(
    "routes",
    [
        {"/api/auth/session": (503, [])},
        logged_in(**{READ_DIARY: (500, [])}),
        logged_in(**{READ_DIARY: (b"<html>challenge</html>", [])}),
        logged_in(**{READ_DIARY: ({"error": "nope"}, [])}),
        logged_in(**{READ_DIARY: ([food("Lunch", 2, 100) | {"nutritional_contents": {"energy": 100}}], [])}),
        {"/api/auth/session": (SESSION, []), "/api/auth/csrf": ({}, [])},
    ],
    ids=["session-503", "diary-500", "diary-html", "diary-not-a-list", "energy-not-an-object", "csrf-missing"],
)
def test_an_answer_that_cant_be_used_is_unavailable_with_exit_0(
    token_file: Path, routes: dict[str, tuple[Any, list[str]]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--token-file", str(token_file), "app-summary"], opener=FakeMfp(routes)) == 0
    assert_unreachable(capsys)


def test_expired_session_is_unavailable_with_exit_0(token_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeMfp({"/api/auth/session": ({}, [])})
    assert main(["--token-file", str(token_file), "app-summary"], opener=fake) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == SIGN_IN and captured.err == ""


def test_missing_token_file_is_unavailable_with_exit_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--token-file", str(tmp_path / "nope"), "app-summary"], opener=no_network) == 0
    assert json.loads(capsys.readouterr().out) == SIGN_IN


def test_other_commands_report_unreachable_as_an_error_not_a_traceback(
    token_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    opener = raising(http.client.IncompleteRead(b"partial"))
    assert main(["--token-file", str(token_file), "session"], opener=opener) == EXIT_ERROR
    assert "couldn't reach MFP" in capsys.readouterr().err


def test_a_cookie_that_cant_be_saved_is_not_reported_as_mfp_being_unreachable(
    token_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def full(self: TokenStore, token: str) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(TokenStore, "save", full)
    fake = FakeMfp({"/api/auth/session": (SESSION, [f"{COOKIE}={NEW_TOKEN}"])})
    assert main(["--token-file", str(token_file), "session"], opener=fake) == EXIT_ERROR
    captured = capsys.readouterr()
    assert "couldn't save" in captured.err and "couldn't reach" not in captured.err
    assert NEW_TOKEN not in captured.err + captured.out


# ---------- the token stays secret ----------


def echoing(status: int | None) -> Callable[[urllib.request.Request, float], FakeResponse]:
    """A server whose error page or non-JSON body repeats the cookies it was sent and just issued."""

    def opener(req: urllib.request.Request, timeout: float) -> FakeResponse:
        cookie = req.get_header("Cookie") or ""
        if f"{COOKIE}=" not in cookie:
            return FakeResponse({}, headers("anon-device-id=device"))
        body = f"debug: {cookie} issued {NEW_TOKEN}".encode()
        rotated = headers(f"{COOKIE}={NEW_TOKEN}")
        if status is not None:
            raise urllib.error.HTTPError(req.full_url, status, "err", rotated, io.BytesIO(body))
        return FakeResponse(body, rotated)

    return opener


TOKEN_PATHS: list[tuple[str, Callable[[urllib.request.Request, float], FakeResponse]]] = [
    ("error-page", echoing(500)),
    ("not-json", echoing(None)),
    ("expired", FakeMfp({"/api/auth/session": ({}, [f"{COOKIE}={NEW_TOKEN}"])})),
    ("unreachable", raising(TimeoutError("timed out"))),
    (
        "ok",
        FakeMfp(
            logged_in(
                **{
                    "/api/auth/session": (SESSION, [f"{COOKIE}={NEW_TOKEN}"]),
                    READ_DIARY: ([], []),
                    "/api/services/account/diary_settings": ({"meal_names": []}, []),
                }
            )
        ),
    ),
]


@pytest.mark.parametrize("name, opener", TOKEN_PATHS, ids=[n for n, _ in TOKEN_PATHS])
@pytest.mark.parametrize("command", ["app-summary", "session", "diary"])
def test_no_token_in_any_output(
    token_file: Path,
    name: str,
    opener: Callable[[urllib.request.Request, float], FakeResponse],
    command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["--token-file", str(token_file), command], opener=opener)
    captured = capsys.readouterr()
    assert OLD_TOKEN not in captured.out + captured.err
    assert NEW_TOKEN not in captured.out + captured.err


@pytest.mark.parametrize("status", [500, None], ids=["http-error", "not-json"])
def test_no_token_in_exception_text(token_file: Path, status: int | None) -> None:
    client = Client(TokenStore(token_file), opener=echoing(status))
    client.request("GET", "/api/auth/session", session=False)
    with pytest.raises(MfpError) as e:
        client.request("GET", "/api/auth/session")
    assert OLD_TOKEN not in str(e.value) and NEW_TOKEN not in str(e.value)
    assert "debug:" in str(e.value)  # the rest of the detail survives


def test_a_token_is_scrubbed_before_the_detail_is_cut_to_length(token_file: Path) -> None:
    def opener(req: urllib.request.Request, timeout: float) -> FakeResponse:
        cookie = req.get_header("Cookie") or ""
        if f"{COOKIE}=" not in cookie:
            return FakeResponse({}, headers())
        body = b"x" * 296 + OLD_TOKEN.encode()  # a cut at 300 would keep the token's first letters
        raise urllib.error.HTTPError(req.full_url, 500, "err", headers(), io.BytesIO(body))

    client = Client(TokenStore(token_file), opener=opener)
    with pytest.raises(MfpError) as e:
        client.request("GET", "/api/auth/session")
    assert "xold" not in str(e.value)


def test_lock_timeout_text_has_no_token(
    token_file: Path, short_waits: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with held_lock(token_file):
        main(["--token-file", str(token_file), "session"], opener=no_network)
    captured = capsys.readouterr()
    assert OLD_TOKEN not in captured.out + captured.err


# ---------- manifest ----------


def test_manifest_declares_the_daily_surface(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["manifest"], opener=no_network) == 0
    m = json.loads(capsys.readouterr().out)
    assert m["app"] == {"label": "Food", "summary": "app-summary", "chat": {"label": "Log food"}, "private": True}
    command = next(c for c in m["commands"] if c["name"] == m["app"]["summary"])
    assert command["writes"] is False


def test_skill_tells_claude_about_the_busy_status(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["skill"], opener=no_network) == 0
    out = capsys.readouterr().out
    assert "**3" in out and "app-summary" in out and ".lock" in out
