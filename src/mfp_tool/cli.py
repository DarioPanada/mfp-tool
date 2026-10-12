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
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("session", help="check the login and keep the cookie fresh")
    diary = sub.add_parser("diary", help="food diary for a day, as JSON")
    diary.add_argument("date", nargs="?", default=datetime.date.today().isoformat(), help="YYYY-MM-DD (default today)")
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
            json.dump(client.diary(args.date), sys.stdout, indent=1, ensure_ascii=False)
            print()
    except SessionExpired as e:
        print(f"MFP login expired ({e}): save a fresh session-token cookie in {args.token_file}", file=sys.stderr)
        return EXIT_EXPIRED
    except (MfpError, ValueError, OSError) as e:
        print(f"mfp: {e}", file=sys.stderr)
        return EXIT_ERROR
    return 0


def main_exit() -> None:
    sys.exit(main())


if __name__ == "__main__":
    main_exit()
