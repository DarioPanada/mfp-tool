"""One real `mfp` process against a fake MFP kept in files, for test_daily.py. Never uses the network.

Usage: two_process_run.py <name> <state dir> <gate path or -> <before-response|in-body> <mfp command>

The fake MFP keeps the one valid token in `<state dir>/valid`, and spends it as MFP does: using it
issues a new one, and using a spent one gets an empty session. Every logged-in request is appended
to `<state dir>/log`. At the gate path the process touches `<state dir>/<name>-held` and waits for
`<state dir>/release`, either before the response comes back (MFP has rotated, the client doesn't
know yet) or after its headers, while the body is being read.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from collections.abc import Callable
from email.message import Message
from pathlib import Path
from typing import Any

from mfp_tool.cli import main
from mfp_tool.client import COOKIE


class Response:
    def __init__(self, body: Any, set_cookie: str | None = None, before_read: Callable[[], None] | None = None) -> None:
        self.headers = Message()
        if set_cookie:
            self.headers["Set-Cookie"] = set_cookie
        self._body = json.dumps(body).encode()
        self._before_read = before_read

    def read(self) -> bytes:
        if self._before_read:
            self._before_read()
        return self._body

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *exc: object) -> None:
        pass


class FileMfp:
    def __init__(self, name: str, state: Path, gate: str, mode: str) -> None:
        self.name = name
        self.state = state
        self.gate = gate
        self.mode = mode

    def hold(self) -> None:
        (self.state / f"{self.name}-held").touch()
        for _ in range(600):
            if (self.state / "release").exists():
                return
            time.sleep(0.05)
        raise AssertionError("never released")

    def __call__(self, req: urllib.request.Request, timeout: float) -> Response:
        path = req.full_url.split("myfitnesspal.com", 1)[1].split("?", 1)[0]
        cookies = dict(c.split("=", 1) for c in (req.get_header("Cookie") or "").split("; ") if c)
        token = cookies.get(COOKIE)
        if token is None:
            return Response({}, "anon-device-id=device; Path=/")
        with (self.state / "log").open("a") as log:
            log.write(f"{self.name} {path} {token}\n")

        body: Any = {}
        set_cookie = None
        if path == "/api/auth/session":
            if token == (self.state / "valid").read_text():
                new = f"{self.name}-rotated"
                (self.state / "valid").write_text(new)
                set_cookie = f"{COOKIE}={new}; Path=/"
                body = {"user": {"username": "someone"}}
        elif path == "/api/auth/csrf":
            body = {"csrfToken": "csrf-value"}
        elif path == "/api/services/diary/read_diary":
            body = []

        if path == self.gate and self.mode == "before-response":
            self.hold()
        return Response(body, set_cookie, self.hold if path == self.gate and self.mode == "in-body" else None)


if __name__ == "__main__":
    name, state_dir, gate, mode, command = sys.argv[1:]
    state = Path(state_dir)
    (state / f"{name}-started").touch()
    sys.exit(main([command], opener=FileMfp(name, state, gate, mode)))
