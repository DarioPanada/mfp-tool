from __future__ import annotations

import argparse
import io
import json
import stat
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from mfp_tool import cli
from mfp_tool.cli import EXIT_EXPIRED, main
from mfp_tool.client import COOKIE, Client, MfpError, SessionExpired, TokenStore, session_token_from


def headers(*set_cookies: str) -> Message:
    m = Message()
    for c in set_cookies:
        m["Set-Cookie"] = c
    return m


class FakeResponse:
    def __init__(self, body: Any, hdrs: Message) -> None:
        self.headers = hdrs
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        pass


DEVICE_COOKIE = "anon-device-id=device; Path=/; Secure"
HOMEPAGE = b"<!DOCTYPE html><html><head><title>Free Calorie Counter</title></head></html>"


class FakeMfp:
    """Answers by path and records each request.

    Like MFP: an anonymous session check hands out the device cookie, and a logged-in request
    without it gets the homepage HTML.
    """

    def __init__(self, routes: dict[str, tuple[Any, list[str]]]) -> None:
        self.routes = routes
        self.requests: list[urllib.request.Request] = []

    def __call__(self, req: urllib.request.Request, timeout: float) -> FakeResponse:
        self.requests.append(req)
        path = req.full_url.split("myfitnesspal.com", 1)[1].split("?", 1)[0]
        sent = req.get_header("Cookie") or ""
        if f"{COOKIE}=" not in sent:
            assert path == "/api/auth/session"
            return FakeResponse({}, headers(DEVICE_COOKIE))
        if "anon-device-id=device" not in sent:
            return FakeResponse(HOMEPAGE, headers())
        body, cookies = self.routes[path]
        if isinstance(body, int):
            raise urllib.error.HTTPError(req.full_url, body, "err", headers(*cookies), io.BytesIO(b'{"message":"no"}'))
        return FakeResponse(body, headers(*cookies))


SESSION = {"user": {"username": "someone"}, "expires": "2026-11-10T00:00:00Z"}
CSRF = {"csrfToken": "csrf-value"}


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    p = tmp_path / "session-token"
    p.write_text("\nold-token\n")
    p.chmod(0o600)
    return p


def test_token_is_read_without_whitespace(token_file: Path) -> None:
    assert TokenStore(token_file).load() == "old-token"


def test_missing_token_file_is_an_expired_session(tmp_path: Path) -> None:
    with pytest.raises(SessionExpired):
        TokenStore(tmp_path / "nope").load()


def test_session_token_joins_chunks_in_numeric_order() -> None:
    cookies = [f"{COOKIE}.10=c; Path=/", f"{COOKIE}.0=a; Path=/", f"{COOKIE}.1=b; Path=/"]
    assert session_token_from(cookies) == "abc"


def test_session_token_ignores_other_cookies() -> None:
    assert session_token_from(["__Host-next-auth.csrf-token=x; Path=/"]) is None


def test_refreshed_cookie_is_saved_owner_only(token_file: Path) -> None:
    fake = FakeMfp(
        {
            "/api/auth/session": (SESSION, [f"{COOKIE}=new-token; Path=/; Secure; HttpOnly"]),
            "/api/auth/csrf": (CSRF, []),
        }
    )
    client = Client(TokenStore(token_file), opener=fake)
    assert client.login() == "someone"
    assert token_file.read_text() == "new-token"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    # The next request already uses the new token.
    assert f"{COOKIE}=new-token" in (fake.requests[2].get_header("Cookie") or "")


def test_unchanged_cookie_is_not_rewritten(token_file: Path) -> None:
    before = token_file.stat().st_mtime_ns
    fake = FakeMfp({"/api/auth/session": (SESSION, [f"{COOKIE}=old-token"]), "/api/auth/csrf": (CSRF, [])})
    Client(TokenStore(token_file), opener=fake).login()
    assert token_file.stat().st_mtime_ns == before


def test_cookie_refreshed_on_an_error_response_is_still_saved(token_file: Path) -> None:
    fake = FakeMfp({"/api/auth/session": (500, [f"{COOKIE}=new-token"])})
    with pytest.raises(MfpError):
        Client(TokenStore(token_file), opener=fake).login()
    assert token_file.read_text() == "new-token"


def test_diary_sends_csrf_header_and_cookie_and_drops_deleted(token_file: Path) -> None:
    entries = [{"id": "1"}, {"id": "2", "deleted": True}]
    fake = FakeMfp(
        {
            "/api/auth/session": (SESSION, []),
            "/api/auth/csrf": (CSRF, ["__Host-next-auth.csrf-token=pair; Path=/; Secure"]),
            "/api/services/diary/read_diary": (entries, []),
        }
    )
    client = Client(TokenStore(token_file), opener=fake)
    assert client.diary("2026-10-11") == [{"id": "1"}]
    req = fake.requests[-1]
    assert req.get_header("X-csrf-token") == "csrf-value"
    assert "__Host-next-auth.csrf-token=pair" in (req.get_header("Cookie") or "")
    assert "entry_date=2026-10-11" in req.full_url and "username=someone" in req.full_url


def test_cli_reports_expired_session_with_exit_2(token_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeMfp({"/api/auth/session": ({}, [])})
    assert main(["--token-file", str(token_file), "session"], opener=fake) == EXIT_EXPIRED
    err = capsys.readouterr().err
    assert "expired" in err and "old-token" not in err


def test_login_first_fetches_device_cookies_without_the_session(token_file: Path) -> None:
    fake = FakeMfp({"/api/auth/session": (SESSION, []), "/api/auth/csrf": (CSRF, [])})
    assert Client(TokenStore(token_file), opener=fake).login() == "someone"
    assert fake.requests[0].get_header("Cookie") is None
    for req in fake.requests[1:]:
        cookie = req.get_header("Cookie") or ""
        assert f"{COOKIE}=old-token" in cookie and "anon-device-id=device" in cookie


def test_html_instead_of_json_is_an_mfp_error(token_file: Path) -> None:
    client = Client(TokenStore(token_file), opener=FakeMfp({"/api/auth/session": (SESSION, [])}))
    with pytest.raises(MfpError, match="expected JSON") as e:
        client.request("GET", "/api/auth/session")  # no device cookie yet
    assert "old-token" not in str(e.value)


def test_skill_prints_without_a_login(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def no_network(req: urllib.request.Request, timeout: float) -> FakeResponse:
        raise AssertionError("mfp skill must not call MFP")

    assert main(["--token-file", str(tmp_path / "missing"), "skill"], opener=no_network) == 0
    out = capsys.readouterr().out
    assert out.startswith("---\nname: mfp\n") and "session-token" in out


def logged_in(**routes: tuple[Any, list[str]]) -> dict[str, tuple[Any, list[str]]]:
    return {"/api/auth/session": (SESSION, []), "/api/auth/csrf": (CSRF, [])} | {
        path: route for path, route in routes.items()
    }


def sent(req: urllib.request.Request) -> Any:
    assert isinstance(req.data, bytes)
    return json.loads(req.data)


FOOD = {
    "id": "123",
    "version": "456",
    "description": "Water",
    "brand_name": None,
    "nutritional_contents": {"energy": {"unit": "calories", "value": 0}},
    "serving_sizes": [{"id": "9", "value": 1, "unit": "cup", "nutrition_multiplier": 2.4}],
}


def test_search_sends_the_query_and_summarizes_each_food(token_file: Path) -> None:
    fake = FakeMfp(logged_in(**{"/api/nutrition": ({"items": [{"item": FOOD, "type": "food"}]}, [])}))
    found = Client(TokenStore(token_file), opener=fake).search("water", max_items=5, page=2, country_code="GB")
    assert found == [
        {
            "id": "123",
            "version": "456",
            "description": "Water",
            "brand": None,
            "kcal": 0,
            "serving_sizes": [{"value": 1, "unit": "cup", "nutrition_multiplier": 2.4, "index": 0}],
        }
    ]
    url = fake.requests[-1].full_url
    for param in ("query=water", "max_items=5", "page=2", "offset=5", "country_code=GB", "resource_type=normal_food"):
        assert param in url


def test_log_posts_one_food_entry_with_csrf(token_file: Path) -> None:
    fake = FakeMfp(logged_in(**{"/api/services/diary": ([{"id": "e1"}], [])}))
    size = {"value": 1, "unit": "cup", "nutrition_multiplier": 2.4, "id": "9"}
    client = Client(TokenStore(token_file), opener=fake)
    assert client.log("2026-10-12", "123", "456", 2, 0.5, size) == [{"id": "e1"}]
    req = fake.requests[-1]
    assert req.get_method() == "POST"
    assert req.get_header("X-csrf-token") == "csrf-value"
    assert sent(req) == {
        "items": [
            {
                "type": "food_entry",
                "date": "2026-10-12",
                "food": {"id": "123", "version": "456"},
                "servings": 0.5,
                "meal_position": 2,
                "serving_size": {"nutrition_multiplier": 2.4, "unit": "cup", "value": 1},
            }
        ]
    }


def cli_log(token_file: Path, fake: FakeMfp, meal: str) -> int:
    args = ["--token-file", str(token_file), "log", "123", "456", "--meal", meal, "--servings", "1"]
    return main([*args, "--value", "1", "--unit", "cup", "--multiplier", "2.4", "--date", "2026-10-12"], opener=fake)


def test_cli_log_takes_the_meal_position_from_the_diary(token_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    diary = [
        {"id": "a", "meal_name": "Breakfast", "meal_position": 0},
        {"id": "b", "meal_name": "Snack 2", "meal_position": 3},
    ]
    fake = FakeMfp(
        logged_in(**{"/api/services/diary/read_diary": (diary, []), "/api/services/diary": ([{"id": "e1"}], [])})
    )
    assert cli_log(token_file, fake, "snack 2") == 0
    assert sent(fake.requests[-1])["items"][0]["meal_position"] == 3
    out = capsys.readouterr().out
    assert json.loads(out) == [{"id": "e1"}] and "old-token" not in out


def test_cli_log_refuses_an_unknown_meal_without_posting(token_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    diary = [{"id": "a", "meal_name": "Breakfast", "meal_position": 0}]
    fake = FakeMfp(logged_in(**{"/api/services/diary/read_diary": (diary, [])}))
    assert cli_log(token_file, fake, "Elevenses") == 1
    assert all(r.get_method() == "GET" for r in fake.requests)
    assert "Breakfast=0" in capsys.readouterr().err


def test_cli_log_takes_a_numeric_meal_position_as_given(token_file: Path) -> None:
    fake = FakeMfp(logged_in(**{"/api/services/diary": ([{"id": "e1"}], [])}))
    assert cli_log(token_file, fake, "4") == 0
    assert sent(fake.requests[-1])["items"][0]["meal_position"] == 4


def test_delete_sends_delete_for_the_entry(token_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeMfp(logged_in(**{"/api/services/diary/abc123": (b"", [])}))
    assert main(["--token-file", str(token_file), "delete", "abc123"], opener=fake) == 0
    assert fake.requests[-1].get_method() == "DELETE"
    assert fake.requests[-1].get_header("X-csrf-token") == "csrf-value"
    assert capsys.readouterr().out == "deleted abc123\n"


@pytest.mark.parametrize("entry_id", ["../read_diary", "a/b", "", "1?x=2"])
def test_delete_rejects_anything_but_an_id_before_calling_mfp(token_file: Path, entry_id: str) -> None:
    fake = FakeMfp(logged_in())
    with pytest.raises(ValueError):
        Client(TokenStore(token_file), opener=fake).delete(entry_id)
    assert fake.requests == []


def test_requests_carry_the_sites_referer(token_file: Path) -> None:
    fake = FakeMfp({"/api/auth/session": (SESSION, []), "/api/auth/csrf": (CSRF, [])})
    Client(TokenStore(token_file), opener=fake).login()
    assert {r.get_header("Referer") for r in fake.requests} == {"https://www.myfitnesspal.com/"}


def test_manifest_describes_every_command_without_a_login(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def no_network(req: urllib.request.Request, timeout: float) -> FakeResponse:
        raise AssertionError("mfp manifest must not call MFP")

    assert main(["--token-file", str(tmp_path / "missing"), "manifest"], opener=no_network) == 0
    m = json.loads(capsys.readouterr().out)
    assert m["manifest"] == 1 and m["name"] == "mfp" and m["version"]
    assert m["skill"].startswith("---\nname: mfp\n")
    assert {c["name"]: c["writes"] for c in m["commands"]} == {
        "session": False,
        "diary": False,
        "search": False,
        "log": True,
        "delete": True,
    }
    assert all(c["usage"].startswith(f"mfp {c['name']}") and c["summary"] for c in m["commands"])


def test_manifest_lists_every_command_but_its_own_helpers() -> None:
    sub = next(a for a in cli.build_parser("2026-01-01")._actions if isinstance(a, argparse._SubParsersAction))
    assert set(sub.choices) - {"skill", "manifest"} == {c["name"] for c in cli.COMMANDS}


def test_manifest_works_from_a_source_tree(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import importlib.metadata

    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    assert main(["manifest"]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == "unknown"
