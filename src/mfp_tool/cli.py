"""`mfp` command line. Never prints the session token."""

from __future__ import annotations

import argparse
import datetime
import importlib.resources
import json
import os
import sys
import urllib.request
from pathlib import Path

from .client import DEFAULT_TOKEN_FILE, Client, MfpError, Opener, SessionExpired, TokenStore

EXIT_EXPIRED = 2
EXIT_ERROR = 1


def main(argv: list[str] | None = None, opener: Opener = urllib.request.urlopen) -> int:
    parser = argparse.ArgumentParser(prog="mfp", description="MyFitnessPal from the command line.")
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path(os.environ.get("MFP_TOKEN_FILE", DEFAULT_TOKEN_FILE)),
        help="session cookie file (default: $MFP_TOKEN_FILE or %(default)s)",
    )
    today = datetime.date.today().isoformat()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("session", help="check the login and keep the cookie fresh")
    diary = sub.add_parser("diary", help="food diary for a day, as JSON")
    diary.add_argument("date", nargs="?", default=today, help="YYYY-MM-DD (default today)")
    search = sub.add_parser("search", help="search MFP's food database, as JSON")
    search.add_argument("text")
    search.add_argument("--max", type=int, default=10, help="results per page (default %(default)s)")
    search.add_argument("--page", type=int, default=1)
    search.add_argument("--country", default="US", help="country code (default %(default)s)")
    log = sub.add_parser("log", help="add a food entry to the diary; prints the new entry as JSON")
    log.add_argument("food_id")
    log.add_argument("food_version")
    log.add_argument("--meal", required=True, help="meal name as in the diary (Breakfast, Lunch, ...) or position")
    log.add_argument("--servings", type=float, required=True)
    log.add_argument("--value", type=float, required=True, help="serving size value, from mfp search")
    log.add_argument("--unit", required=True, help="serving size unit, from mfp search")
    log.add_argument("--multiplier", type=float, required=True, help="serving size nutrition_multiplier")
    log.add_argument("--date", default=today, help="YYYY-MM-DD (default today)")
    delete = sub.add_parser("delete", help="delete a diary entry by its id")
    delete.add_argument("entry_id")
    sub.add_parser("skill", help="print the Claude Code skill for mfp (SKILL.md)")
    args = parser.parse_args(argv)

    if args.command == "skill":
        print(importlib.resources.files("mfp_tool").joinpath("SKILL.md").read_text(), end="")
        return 0

    try:
        client = Client(TokenStore(args.token_file), opener=opener)
        user = client.login()
        if args.command == "session":
            print(f"logged in as {user}")
        elif args.command == "diary":
            datetime.date.fromisoformat(args.date)
            _print_json(client.diary(args.date))
        elif args.command == "search":
            _print_json(client.search(args.text, max_items=args.max, page=args.page, country_code=args.country))
        elif args.command == "log":
            datetime.date.fromisoformat(args.date)
            position = _meal_position(client, args.meal, args.date, today)
            size = {"value": args.value, "unit": args.unit, "nutrition_multiplier": args.multiplier}
            _print_json(client.log(args.date, args.food_id, args.food_version, position, args.servings, size))
        elif args.command == "delete":
            client.delete(args.entry_id)
            print(f"deleted {args.entry_id}")
    except SessionExpired as e:
        print(f"MFP login expired ({e}): save a fresh session-token cookie in {args.token_file}", file=sys.stderr)
        return EXIT_EXPIRED
    except (MfpError, ValueError, OSError) as e:
        print(f"mfp: {e}", file=sys.stderr)
        return EXIT_ERROR
    return 0


def _print_json(value: object) -> None:
    json.dump(value, sys.stdout, indent=1, ensure_ascii=False)
    print()


def _meal_position(client: Client, meal: str, day: str, today: str) -> int:
    """A position as given, or a meal name looked up in the diary of `day` and of today."""
    if meal.isdigit():
        return int(meal)
    known = client.meals(today) | (client.meals(day) if day != today else {})
    for name, position in known.items():
        if name.casefold() == meal.casefold():
            return position
    listed = ", ".join(f"{n}={p}" for n, p in sorted(known.items(), key=lambda kv: kv[1])) or "none"
    days = today if day == today else f"{today} or {day}"
    raise ValueError(f"no meal {meal!r} in the diary of {days} (seen: {listed}); give its position")


def main_exit() -> None:
    sys.exit(main())


if __name__ == "__main__":
    main_exit()
