"""MyFitnessPal web API client authenticated by the NextAuth session cookie.

MFP's website keeps the login in the `__Secure-next-auth.session-token` cookie and refreshes it
on most responses. The refresh token inside is single-use, so once MFP has issued a new cookie the
old one stops working: every cookie MFP sends back is written to the token file straight away.

MFP also wants the device cookies (`anon-device-id` and the rest) it hands a first-time visitor:
a logged-in request without them gets the homepage HTML instead of JSON. The client picks them up
with one anonymous request before logging in.
"""

from __future__ import annotations

import json
import os
import re
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

    def _absorb(self, headers: Any, session: bool = True) -> None:
        set_cookies = headers.get_all("Set-Cookie") or []
        for header in set_cookies:
            for name, morsel in SimpleCookie(header).items():
                if name != COOKIE and not name.startswith(COOKIE + "."):
                    self.cookies[name] = morsel.value
        if not session:
            return
        new = session_token_from(set_cookies)
        if new and new != self.token:
            self.store.save(new)
            self.token = new

    def request(
        self, method: str, path: str, query: dict[str, str] | None = None, body: Any = None, session: bool = True
    ) -> Any:
        """Calls MFP and returns the decoded JSON; `session=False` leaves the session cookie out."""
        url = self.base_url + path + ("?" + urllib.parse.urlencode(query) if query else "")
        cookies = [f"{k}={v}" for k, v in self.cookies.items()]
        if session:
            cookies.insert(0, f"{COOKIE}={self.token}")
        # Food search (/api/nutrition) answers 404 {"items":[]} to a request without a Referer.
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json", "Referer": self.base_url + "/"}
        if cookies:
            headers["Cookie"] = "; ".join(cookies)
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310 - https only, see __init__
        try:
            with self.opener(req, timeout=30) as resp:
                self._absorb(resp.headers, session)
                raw = resp.read()
        except urllib.error.HTTPError as e:
            self._absorb(e.headers, session)
            detail = e.read()[:300].decode(errors="replace")
            raise MfpError(f"{method} {path}: HTTP {e.code} {detail}") from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise MfpError(f"{method} {path}: expected JSON, got {raw[:60]!r}") from None

    def login(self) -> str:
        """Checks the session (which also refreshes the cookie) and fetches a CSRF token."""
        self.request("GET", "/api/auth/session", session=False)  # device cookies, see the module docstring
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

    def meals(self, day: str) -> dict[str, int]:
        """Meal name to `meal_position`, from the meals that have entries on `day`."""
        return {e["meal_name"]: e["meal_position"] for e in self.diary(day)}

    def search(self, text: str, max_items: int = 10, page: int = 1, country_code: str = "US") -> list[dict[str, Any]]:
        """Foods matching `text`: id, version, description, brand and serving sizes of each."""
        if not self.username:
            self.login()
        query = {
            "query": text,
            "max_items": str(max_items),
            "page": str(page),
            "offset": str((page - 1) * max_items),
            "country_code": country_code,
            "resource_type": "normal_food",
        }
        found = self.request("GET", "/api/nutrition", query) or {}
        items = found.get("items", []) if isinstance(found, dict) else found
        return [_food_summary(i.get("item", i)) for i in items]

    def log(
        self,
        day: str,
        food_id: str,
        food_version: str,
        meal_position: int,
        servings: float,
        serving_size: dict[str, Any],
    ) -> Any:
        """Adds a food entry and returns MFP's answer: a list holding the new entry, with its id."""
        if not self.username:
            self.login()
        entry = {
            "type": "food_entry",
            "date": day,
            "food": {"id": food_id, "version": food_version},
            "servings": servings,
            "meal_position": meal_position,
            "serving_size": {k: serving_size[k] for k in ("nutrition_multiplier", "unit", "value")},
        }
        return self.request("POST", "/api/services/diary", body={"items": [entry]})

    def delete(self, entry_id: str) -> None:
        """Deletes a diary entry (food, quick add or exercise)."""
        _check_id(entry_id, "entry")
        if not self.username:
            self.login()
        self.request("DELETE", f"/api/services/diary/{entry_id}")

    def meal_names(self) -> list[str]:
        """The diary's meal names, in order: a meal's index is its `meal_position`."""
        if not self.username:
            self.login()
        return list((self.request("GET", "/api/services/account/diary_settings") or {}).get("meal_names") or [])

    def quick_add(self, day: str, meal_name: str, nutrition: dict[str, float]) -> Any:
        """Adds calories (and any of carbohydrates, fat, protein in grams) to a meal without a food."""
        if not self.username:
            self.login()
        contents: dict[str, Any] = {k: v for k, v in nutrition.items() if k != "energy"}
        contents["energy"] = {"value": nutrition["energy"], "unit": "calories"}
        entry = {"type": "quick_add", "date": day, "meal_name": meal_name, "nutritional_contents": contents}
        return self.request("POST", "/api/services/diary", body={"items": [entry]})

    def exercises(self, day: str) -> list[dict[str, Any]]:
        """Exercise entries for `day` (YYYY-MM-DD), without deleted ones."""
        username = self.username or self.login()
        query = {"entry_date": day, "fields": "all", "types": "exercise_entry", "username": username}
        entries = self.request("GET", "/api/services/diary/read_diary", query)
        return [e for e in entries if e.get("type") == "exercise_entry" and not e.get("deleted")]

    def search_exercises(self, text: str) -> list[dict[str, Any]]:
        """Exercises matching `text`: id and description of each."""
        if not self.username:
            self.login()
        found = self.request("GET", "/api/services/exercises/search", {"search": text, "locale": "en"}) or {}
        return list(found.get("exercises") or [])

    def log_exercise(self, day: str, exercise_id: str, seconds: int, kcal: float, start_time: str) -> Any:
        """Adds an exercise entry; `start_time` is UTC, as YYYY-MM-DDTHH:MM:SSZ."""
        _check_id(exercise_id, "exercise")
        if not self.username:
            self.login()
        exercise = self.request("GET", f"/api/services/exercises/{exercise_id}")
        entry = {
            "type": "exercise_entry",
            "date": day,
            "exercise": {"id": exercise_id, "version": exercise["version"]},
            "duration": seconds,
            "energy": {"unit": "calories", "value": kcal},
            "start_time": start_time,
        }
        return self.request("POST", "/api/services/diary", body={"items": [entry]})

    def saved_meals(self) -> list[dict[str, Any]]:
        """Saved meals: id, description and each food's name and nutrition (MFP gives no food ids)."""
        if not self.username:
            self.login()
        return list(self.request("GET", "/api/services/users/meals/mine", {"limit": "100", "search": ""}) or [])

    def my_foods(self, text: str = "") -> list[dict[str, Any]]:
        """Foods this account created (recipes saved as foods among them)."""
        if not self.username:
            self.login()
        found = self.request("GET", "/api/services/users/foods/mine", {"search": text}) or []
        items = found.get("items", []) if isinstance(found, dict) else found
        return [_food_summary(i.get("item", i)) for i in items]

    def create_food(
        self, description: str, brand: str, serving: dict[str, Any], nutrition: dict[str, float], country_code: str
    ) -> Any:
        """Creates a private food (energy in kcal, nutrients in grams, sodium in mg) and returns MFP's answer."""
        if not self.username:
            self.login()
        user = self.request("GET", "/api/services/users")
        contents: dict[str, Any] = {k: v for k, v in nutrition.items() if k != "energy"}
        contents["energy"] = {"unit": "calories", "value": nutrition["energy"]}
        item = {
            "user_id": user["id"],
            "brand_name": brand,
            "description": description,
            "nutritional_contents": contents,
            "serving_sizes": [{"value": serving["value"], "unit": serving["unit"], "nutrition_multiplier": 1}],
            "public": False,
            "country_code": country_code,
        }
        return self.request("POST", "/api/services/foods", body={"item": item})

    def delete_food(self, food_id: str) -> None:
        """Deletes a food this account created."""
        _check_id(food_id, "food")
        if not self.username:
            self.login()
        self.request("DELETE", f"/api/services/foods/{food_id}")


def _check_id(value: str, what: str) -> None:
    """Ids go into URL paths: letters, digits, `_` and `-` only."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError(f"not an {what} id: {value!r}" if what[0] in "aeiou" else f"not a {what} id: {value!r}")


def _food_summary(food: dict[str, Any]) -> dict[str, Any]:
    energy = ((food.get("nutritional_contents") or {}).get("energy") or {}).get("value")
    sizes = [
        {k: s.get(k) for k in ("value", "unit", "nutrition_multiplier")} | {"index": n}
        for n, s in enumerate(food.get("serving_sizes") or [])
    ]
    return {
        "id": food.get("id"),
        "version": food.get("version"),
        "description": food.get("description"),
        "brand": food.get("brand_name"),
        "kcal": energy,  # for the food's base nutrition, i.e. a nutrition_multiplier of 1
        "serving_sizes": sizes,
    }
