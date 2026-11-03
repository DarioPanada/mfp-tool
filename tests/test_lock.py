"""The session lock and the token's safety. Temp token files and fake openers only, never the network."""

from __future__ import annotations

import contextlib
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
    monkeypatch.setattr(cli, "LOCK_WAIT_SECONDS", 0.2)


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
@pytest.mark.parametrize("second", ["session", "diary"])
def test_second_run_waits_for_the_whole_first_run_then_reads_its_rotated_token(
    two: TwoProcesses, gate: str, second: str
) -> None:
    a = two.start("a", "diary", gate)
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
    assert json.loads(a_out) == []
    assert b_out == "logged in as someone\n" if second == "session" else json.loads(b_out) == []


def test_a_killed_run_frees_the_lock_and_keeps_a_cookie_whose_headers_had_arrived(two: TwoProcesses) -> None:
    a = two.start("a", "diary", "/api/auth/session", "in-body")
    two.wait_for("a-held", a)
    a.kill()
    a.communicate()
    assert two.token_file.read_text() == "a-rotated"

    b = two.start("b", "diary")
    out, err = b.communicate(timeout=30)
    assert b.returncode == 0 and json.loads(out) == [], err
    assert two.token_file.read_text() == "b-rotated"


def test_a_run_killed_before_the_response_arrives_can_still_lose_the_session(two: TwoProcesses) -> None:
    """What the lock can't do: MFP has spent the old token and the new one died on the wire."""
    a = two.start("a", "diary", "/api/auth/session", "before-response")
    two.wait_for("a-held", a)
    a.kill()
    a.communicate()
    assert two.token_file.read_text() == OLD_TOKEN

    b = two.start("b", "diary")
    out, err = b.communicate(timeout=30)
    assert (b.returncode, out) == (EXIT_EXPIRED, "") and "expired" in err


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


@pytest.mark.parametrize("command", ["session", "diary"])
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

    assert main(["--token-file", str(token_file), "diary"], opener=opener) == 0
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
    assert "expired" in capsys.readouterr().err


def test_lock_wait_is_above_the_worst_full_run(token_file: Path) -> None:
    """The heaviest command is `log` by meal name on another day; see MOST_REQUESTS_IN_A_RUN."""
    diary = [{"id": "a", "meal_name": "Lunch", "meal_position": 2}]
    fake = FakeMfp(logged_in(**{READ_DIARY: (diary, []), "/api/services/diary": ([{"id": "e1"}], [])}))
    args = ["log", "1", "2", "--meal", "lunch", "--servings", "1", "--value", "1", "--unit", "g", "--multiplier", "1"]
    assert main(["--token-file", str(token_file), *args, "--date", "2026-01-02"], opener=fake) == 0
    assert len(fake.requests) == cli.MOST_REQUESTS_IN_A_RUN
    assert cli.LOCK_WAIT_SECONDS > cli.MOST_REQUESTS_IN_A_RUN * REQUEST_TIMEOUT_SECONDS


# ---------- requests, and MFP that can't be reached ----------


def test_requests_use_the_timeout(token_file: Path) -> None:
    fake = FakeMfp(logged_in(**{READ_DIARY: ([], [])}))
    timeouts: list[float] = []

    def opener(req: urllib.request.Request, timeout: float) -> FakeResponse:
        timeouts.append(timeout)
        return fake(req, timeout)

    assert main(["--token-file", str(token_file), "diary"], opener=opener) == 0
    assert timeouts == [30, 30, 30, 30]


def test_a_cookie_is_saved_when_only_the_body_is_lost(token_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    class BodyLost(FakeResponse):
        def read(self) -> bytes:
            raise TimeoutError("timed out")

    def opener(req: urllib.request.Request, timeout: float) -> FakeResponse:
        if f"{COOKIE}=" not in (req.get_header("Cookie") or ""):
            return FakeResponse({}, headers("anon-device-id=device"))
        return BodyLost({}, headers(f"{COOKIE}={NEW_TOKEN}"))

    assert main(["--token-file", str(token_file), "session"], opener=opener) == EXIT_ERROR
    assert token_file.read_text() == NEW_TOKEN
    assert "couldn't reach MFP" in capsys.readouterr().err


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


@pytest.mark.parametrize("error", UNREACHABLE, ids=lambda e: type(e).__name__)
def test_unreachable_is_an_error_not_a_traceback(
    token_file: Path, error: Exception, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--token-file", str(token_file), "session"], opener=raising(error)) == EXIT_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "couldn't reach MFP" in captured.err and "Traceback" not in captured.err


def test_unreachable_partway_through_keeps_the_new_cookie(token_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeMfp({"/api/auth/session": (SESSION, [f"{COOKIE}={NEW_TOKEN}"]), "/api/auth/csrf": (CSRF, [])})

    def opener(req: urllib.request.Request, timeout: float) -> FakeResponse:
        if READ_DIARY in req.full_url:
            raise TimeoutError("timed out")
        return fake(req, timeout)

    assert main(["--token-file", str(token_file), "diary"], opener=opener) == EXIT_ERROR
    assert "couldn't reach MFP" in capsys.readouterr().err
    assert token_file.read_text() == NEW_TOKEN


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
@pytest.mark.parametrize("command", ["session", "diary"])
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


def test_manifest_declares_a_daily_tool_and_nothing_more(capsys: pytest.CaptureFixture[str]) -> None:
    """mfp declares a label (at most 30 characters) and the private flag, and nothing else: it has no views to pin."""
    assert main(["manifest"], opener=no_network) == 0
    app = json.loads(capsys.readouterr().out)["app"]
    assert app == {"label": "Food", "private": True}
    assert len(app["label"]) <= 30


def test_there_is_no_app_summary_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert "app-summary" not in {c["name"] for c in cli.COMMANDS}
    with pytest.raises(SystemExit) as e:
        main(["app-summary"], opener=no_network)
    assert e.value.code == 2  # argparse: an unknown command
    capsys.readouterr()


def test_skill_tells_claude_about_the_busy_status(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["skill"], opener=no_network) == 0
    out = capsys.readouterr().out
    assert "**3" in out and ".lock" in out
    assert "app-summary" not in out
