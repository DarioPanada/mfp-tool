"""`mfp` command line. Never prints the session token."""

from __future__ import annotations

import argparse
import datetime
import importlib.metadata
import importlib.resources
import json
import os
import sys
import urllib.request
from pathlib import Path

from .client import DEFAULT_TOKEN_FILE, Client, MfpError, Opener, SessionExpired, TokenStore

EXIT_EXPIRED = 2
EXIT_ERROR = 1

# What `mfp manifest` tells a host such as the cockpit: every command Claude may run, and which
# ones change the diary (a host asks before running those). Every command that logs in, read-only
# ones too, rewrites the cookie file when MFP refreshes it; `writes` is about the diary only.
COMMANDS = [
    {"name": "session", "usage": "mfp session", "summary": "check the login, refresh the cookie", "writes": False},
    {"name": "diary", "usage": "mfp diary [YYYY-MM-DD]", "summary": "food entries for a day, as JSON", "writes": False},
    {
        "name": "search",
        "usage": "mfp search <text> [--max N] [--page P] [--country CC]",
        "summary": "search MFP's food database, as JSON",
        "writes": False,
    },
    {
        "name": "log",
        "usage": "mfp log <food id> <food version> --meal M --servings N --value V --unit U --multiplier X [--date D]",
        "summary": "add a food entry to the diary",
        "writes": True,
    },
    {"name": "delete", "usage": "mfp delete <entry id>", "summary": "delete a diary entry", "writes": True},
    {
        "name": "quick-add",
        "usage": "mfp quick-add --kcal N --meal M [--carbs G] [--fat G] [--protein G] [--date D]",
        "summary": "add calories (and macros) to a meal without a food",
        "writes": True,
    },
    {
        "name": "exercises",
        "usage": "mfp exercises [YYYY-MM-DD]",
        "summary": "exercise entries for a day, as JSON",
        "writes": False,
    },
    {
        "name": "exercise-search",
        "usage": "mfp exercise-search <text>",
        "summary": "search MFP's exercises, as JSON",
        "writes": False,
    },
    {
        "name": "exercise-log",
        "usage": "mfp exercise-log <exercise id> --minutes N --kcal K [--date D] [--time HH:MM]",
        "summary": "add an exercise entry (calories burned) to the diary",
        "writes": True,
    },
    {"name": "meals", "usage": "mfp meals", "summary": "saved meals and their foods, as JSON", "writes": False},
    {"name": "foods", "usage": "mfp foods [text]", "summary": "foods this account created, as JSON", "writes": False},
    {
        "name": "food-create",
        "usage": "mfp food-create <description> --kcal K --value V --unit U [--brand B] [--carbs G] [--fat G] "
        "[--protein G] [--sugar G] [--fiber G] [--sodium MG] [--country CC]",
        "summary": "create a private food, such as a recipe per serving",
        "writes": True,
    },
    {
        "name": "food-delete",
        "usage": "mfp food-delete <food id>",
        "summary": "delete a food this account created",
        "writes": True,
    },
    {
        "name": "weights",
        "usage": "mfp weights [--from D] [--to D] [--limit N]",
        "summary": "weigh-ins, newest first, as JSON",
        "writes": False,
    },
    {
        "name": "weight-log",
        "usage": "mfp weight-log <weight> --unit kg|lb [--date D]",
        "summary": "save a weigh-in",
        "writes": True,
    },
    {
        "name": "weight-delete",
        "usage": "mfp weight-delete <measurement id>",
        "summary": "delete a weigh-in",
        "writes": True,
    },
]

WEIGHT_UNITS = {"kg": "kilograms", "lb": "pounds"}

NUTRIENTS = {"carbs": "carbohydrates", "fat": "fat", "protein": "protein"}
MORE_NUTRIENTS = {"sugar": "sugar", "fiber": "fiber", "sodium": "sodium"}


def build_parser(today: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mfp", description="MyFitnessPal from the command line.")
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path(os.environ.get("MFP_TOKEN_FILE", DEFAULT_TOKEN_FILE)),
        help="session cookie file (default: $MFP_TOKEN_FILE or %(default)s)",
    )
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
    quick = sub.add_parser("quick-add", help="add calories to a meal without a food; prints the new entry")
    quick.add_argument("--kcal", type=float, required=True)
    quick.add_argument("--meal", required=True, help="meal name (Breakfast, Lunch, ...) or position")
    _nutrient_args(quick, NUTRIENTS)
    quick.add_argument("--date", default=today, help="YYYY-MM-DD (default today)")
    exercises = sub.add_parser("exercises", help="exercise entries for a day, as JSON")
    exercises.add_argument("date", nargs="?", default=today, help="YYYY-MM-DD (default today)")
    exercise_search = sub.add_parser("exercise-search", help="search MFP's exercises, as JSON")
    exercise_search.add_argument("text")
    exercise_log = sub.add_parser("exercise-log", help="add an exercise entry; prints the new entry")
    exercise_log.add_argument("exercise_id")
    exercise_log.add_argument("--minutes", type=float, required=True)
    exercise_log.add_argument("--kcal", type=float, required=True, help="calories burned")
    exercise_log.add_argument("--date", default=today, help="YYYY-MM-DD (default today)")
    exercise_log.add_argument("--time", help="local start time HH:MM (default now, or 12:00 on another day)")
    sub.add_parser("meals", help="saved meals and their foods, as JSON")
    foods = sub.add_parser("foods", help="foods this account created, as JSON")
    foods.add_argument("text", nargs="?", default="")
    food_create = sub.add_parser("food-create", help="create a private food; prints it with its id and version")
    food_create.add_argument("description")
    food_create.add_argument("--kcal", type=float, required=True, help="calories per serving")
    food_create.add_argument("--value", type=float, required=True, help="serving size value, such as 1")
    food_create.add_argument("--unit", required=True, help="serving size unit, such as serving or g")
    food_create.add_argument("--brand", default="", help="brand, or a label such as Homemade")
    _nutrient_args(food_create, NUTRIENTS | MORE_NUTRIENTS)
    food_create.add_argument("--country", default="US", help="country code (default %(default)s)")
    food_delete = sub.add_parser("food-delete", help="delete a food this account created")
    food_delete.add_argument("food_id")
    weights = sub.add_parser("weights", help="weigh-ins, newest first, as JSON")
    weights.add_argument("--from", dest="since", help="YYYY-MM-DD")
    weights.add_argument("--to", dest="until", help="YYYY-MM-DD")
    weights.add_argument("--limit", type=int, default=30, help="at most this many (default %(default)s)")
    weight_log = sub.add_parser("weight-log", help="save a weigh-in; prints MFP's answer")
    weight_log.add_argument("weight", type=float)
    weight_log.add_argument("--unit", required=True, choices=WEIGHT_UNITS)
    weight_log.add_argument("--date", default=today, help="YYYY-MM-DD (default today)")
    weight_delete = sub.add_parser("weight-delete", help="delete a weigh-in by its id")
    weight_delete.add_argument("measurement_id")
    sub.add_parser("skill", help="print the Claude Code skill for mfp (SKILL.md)")
    sub.add_parser("manifest", help="describe this tool for a host such as the cockpit, as JSON")
    return parser


def main(argv: list[str] | None = None, opener: Opener = urllib.request.urlopen) -> int:
    today = datetime.date.today().isoformat()
    args = build_parser(today).parse_args(argv)

    if args.command == "skill":
        print(_skill(), end="")
        return 0
    if args.command == "manifest":
        _print_json(_manifest())
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
        elif args.command == "quick-add":
            datetime.date.fromisoformat(args.date)
            meal = _meal_name(client.meal_names(), args.meal)
            _print_json(client.quick_add(args.date, meal, _nutrition(args, NUTRIENTS)))
        elif args.command == "exercises":
            datetime.date.fromisoformat(args.date)
            _print_json(client.exercises(args.date))
        elif args.command == "exercise-search":
            _print_json(client.search_exercises(args.text))
        elif args.command == "exercise-log":
            start = _start_time(args.date, args.time, today)
            seconds = round(args.minutes * 60)
            _print_json(client.log_exercise(args.date, args.exercise_id, seconds, args.kcal, start))
        elif args.command == "meals":
            _print_json(client.saved_meals())
        elif args.command == "foods":
            _print_json(client.my_foods(args.text))
        elif args.command == "food-create":
            serving = {"value": args.value, "unit": args.unit}
            nutrition = _nutrition(args, NUTRIENTS | MORE_NUTRIENTS)
            _print_json(client.create_food(args.description, args.brand, serving, nutrition, args.country))
        elif args.command == "food-delete":
            client.delete_food(args.food_id)
            print(f"deleted food {args.food_id}")
        elif args.command == "weights":
            for day in (args.since, args.until):
                if day:
                    datetime.date.fromisoformat(day)
            _print_json(client.weights(args.since, args.until, args.limit))
        elif args.command == "weight-log":
            datetime.date.fromisoformat(args.date)
            _print_json(client.log_weight(args.date, args.weight, WEIGHT_UNITS[args.unit]))
        elif args.command == "weight-delete":
            client.delete_weight(args.measurement_id)
            print(f"deleted weigh-in {args.measurement_id}")
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


def _skill() -> str:
    return importlib.resources.files("mfp_tool").joinpath("SKILL.md").read_text()


def _version() -> str:
    try:
        return importlib.metadata.version("mfp-tool")
    except importlib.metadata.PackageNotFoundError:  # run from a source tree, not installed
        return "unknown"


def _manifest() -> dict[str, object]:
    return {
        "manifest": 1,
        "name": "mfp",
        "version": _version(),
        "description": "MyFitnessPal diary: read and log food, quick adds, exercise and weigh-ins; search foods and "
        "exercises; create foods such as recipes.",
        "commands": COMMANDS,
        "skill": _skill(),
    }


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


def _nutrient_args(parser: argparse.ArgumentParser, names: dict[str, str]) -> None:
    for flag in names:
        parser.add_argument(f"--{flag}", type=float, help="mg" if flag == "sodium" else "grams")


def _nutrition(args: argparse.Namespace, names: dict[str, str]) -> dict[str, float]:
    """`energy` in kcal plus each nutrient given on the command line, under MFP's names."""
    given = {mfp: getattr(args, flag) for flag, mfp in names.items() if getattr(args, flag) is not None}
    return {"energy": args.kcal} | given


def _meal_name(names: dict[str, int], meal: str) -> str:
    """The diary's own name for a meal given by name (any case) or position."""
    for name, position in names.items():
        if name.casefold() == meal.casefold() or (meal.isdigit() and int(meal) == position):
            return name
    listed = ", ".join(f"{n}={p}" for n, p in sorted(names.items(), key=lambda kv: kv[1])) or "none"
    raise ValueError(f"no meal {meal!r} in the diary (meals: {listed})")


def _start_time(day: str, time: str | None, today: str) -> str:
    """A local date and HH:MM as UTC; no time means now today, or 12:00 on another day."""
    date = datetime.date.fromisoformat(day)
    if time is None:
        local = (
            datetime.datetime.now().astimezone() if day == today else datetime.datetime.combine(date, datetime.time(12))
        )
    else:
        local = datetime.datetime.combine(date, datetime.time.fromisoformat(time))
    return local.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def main_exit() -> None:
    sys.exit(main())


if __name__ == "__main__":
    main_exit()
