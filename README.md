# mfp-tool

MyFitnessPal from the command line. Built so a Claude Code session can read (and later log) the
food diary on a machine where a browser login is not possible.

MFP's login needs a browser (reCAPTCHA), so the tool runs on a session cookie copied from one.
No dependencies beyond the Python standard library.

## Install

```sh
uv tool install git+https://github.com/DarioPanada/mfp-tool
```

## The session cookie

The tool reads the cookie `__Secure-next-auth.session-token` from `~/.config/mfp/session-token`
(or `--token-file`, or `$MFP_TOKEN_FILE`).

MFP refreshes the cookie on most responses, and its refresh token is single-use: once MFP has
issued a new cookie the old one stops working. So the tool writes every cookie MFP sends back to
that file, and **the machine running the tool needs a session of its own**. Copying the cookie of
the browser you keep using fails as soon as the browser refreshes it.

To give a machine its own session:

1. In the browser, on myfitnesspal.com, open DevTools → Application → Cookies and copy the value
   of `__Secure-next-auth.session-token`.
2. Save it on the machine without echoing it:
   ```sh
   mkdir -p ~/.config/mfp && chmod 700 ~/.config/mfp
   read -rs T && printf %s "$T" > ~/.config/mfp/session-token && chmod 600 ~/.config/mfp/session-token && unset T
   ```
3. Back in DevTools, Application → Storage → **Clear site data** (not "Log out", which may end the
   session you just copied), then log in again in the browser. The browser now has a new session
   and the machine keeps the old one.

The session lasts 30 days from its last use. When it has expired the tool exits with status 2.

## Use

```sh
mfp session              # check the login, refresh the cookie
mfp diary [YYYY-MM-DD]   # food entries for a day (default today), as JSON
```

Exit status: 0 ok, 1 error, 2 the session has expired.

## Development

```sh
uv sync
uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest
```

Tests never call MFP; they use a fake opener. Never commit a cookie or diary data.

## API notes

The client uses the website's own JSON routes (`https://www.myfitnesspal.com/api/...`), which proxy
MFP API v2. Authentication is the session cookie plus, for `/api/services/*`, an `X-CSRF-Token`
header and the CSRF cookie from `GET /api/auth/csrf`.
