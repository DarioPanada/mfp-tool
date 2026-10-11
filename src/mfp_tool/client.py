"""MyFitnessPal web API client authenticated by the NextAuth session cookie.

MFP's website keeps the login in the `__Secure-next-auth.session-token` cookie and refreshes it
on most responses. The refresh token inside is single-use, so once MFP has issued a new cookie the
old one stops working: every cookie MFP sends back is written to the token file straight away.
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Protocol

BASE_URL = "https://www.myfitnesspal.com"
COOKIE = "__Secure-next-auth.session-token"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0 Safari/537.36"
DEFAULT_TOKEN_FILE = Path("~/.config/mfp/session-token").expanduser()


class MfpError(Exception):
    """MFP answered with an error."""


class SessionExpired(MfpError):
    """The saved cookie no longer logs in; a fresh one has to be saved."""


class _Response(Protocol):
    headers: Any

    def read(self) -> bytes: ...
    def __enter__(self) -> _Response: ...
    def __exit__(self, *exc: object) -> None: ...


Opener = Callable[..., _Response]


class TokenStore:
    """The session token on disk, owner-only, replaced atomically."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> str:
        try:
            return "".join(self.path.read_text().split())
        except FileNotFoundError:
            raise SessionExpired(f"no session token at {self.path}") from None

    def save(self, token: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".session-token.")
        try:
            with os.fdopen(fd, "w") as f:  # mkstemp creates the file 0600
                f.write(token)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def session_token_from(set_cookies: list[str]) -> str | None:
    """The session token in a response's Set-Cookie headers, joining NextAuth's `.0`, `.1` chunks."""
    parts: dict[str, str] = {}
    for header in set_cookies:
        for name, morsel in SimpleCookie(header).items():
            if name == COOKIE or name.startswith(COOKIE + "."):
                parts[name] = morsel.value
    if COOKIE in parts:
        return parts[COOKIE] or None
    chunks = sorted((int(n.rsplit(".", 1)[1]), v) for n, v in parts.items() if n.rsplit(".", 1)[1].isdigit())
    return "".join(v for _, v in chunks) or None


class Client:
    def __init__(self, store: TokenStore, opener: Opener = urllib.request.urlopen, base_url: str = BASE_URL) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("base_url must be https")
        self.store = store
        self.opener = opener
        self.base_url = base_url
        self.token = store.load()
        self.cookies: dict[str, str] = {}  # other cookies (CSRF, Cloudflare), for this client only
        self.csrf: str | None = None
        self.username: str | None = None

    def _absorb(self, headers: Any) -> None:
        set_cookies = headers.get_all("Set-Cookie") or []
        for header in set_cookies:
            for name, morsel in SimpleCookie(header).items():
                if name != COOKIE and not name.startswith(COOKIE + "."):
                    self.cookies[name] = morsel.value
        new = session_token_from(set_cookies)
        if new and new != self.token:
            self.store.save(new)
            self.token = new

    def request(self, method: str, path: str, query: dict[str, str] | None = None, body: Any = None) -> Any:
        url = self.base_url + path + ("?" + urllib.parse.urlencode(query) if query else "")
        cookie = "; ".join([f"{COOKIE}={self.token}", *(f"{k}={v}" for k, v in self.cookies.items())])
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json", "Cookie": cookie}
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310 - https only, see __init__
        try:
            with self.opener(req, timeout=30) as resp:
                self._absorb(resp.headers)
                raw = resp.read()
        except urllib.error.HTTPError as e:
            self._absorb(e.headers)
            detail = e.read()[:300].decode(errors="replace")
            raise MfpError(f"{method} {path}: HTTP {e.code} {detail}") from None
        return json.loads(raw) if raw else None

    def login(self) -> str:
        """Checks the session (which also refreshes the cookie) and fetches a CSRF token."""
        user = (self.request("GET", "/api/auth/session") or {}).get("user") or {}
        name = user.get("username") or user.get("name")
        if not name:
            raise SessionExpired("the saved MFP session has expired")
        self.username = str(name)
        self.csrf = self.request("GET", "/api/auth/csrf")["csrfToken"]
        return self.username

    def diary(self, day: str) -> list[dict[str, Any]]:
        """Food entries for `day` (YYYY-MM-DD), without deleted ones."""
        username = self.username or self.login()
        query = {"entry_date": day, "fields": "all", "types": "food_entry", "username": username}
        entries = self.request("GET", "/api/services/diary/read_diary", query)
        return [e for e in entries if not e.get("deleted")]
