"""Stage 6 — keep listening history current between exports.

    .venv/bin/python poll.py            # fetch anything new since last run
    .venv/bin/python poll.py --status   # what is stored, no network
    .venv/bin/python poll.py --logout   # forget the stored token

The export is a snapshot. Everything downstream freezes at whatever date it was
generated. This tops it up from Spotify's recently-played endpoint so the trend
lines stay live.

Two limits are inherent to that endpoint, not to this code, and both matter:

  1. NO PLAY DURATION. The response carries `played_at` and the track's full
     `duration_ms`, but never how much of it was actually heard. The whole
     analysis weights by `ms_played`, so polled rows carry an ESTIMATE and are
     flagged `ms_played_estimated = true`. Spotify only lists a track here once
     it has played past ~30s, so the estimate is sound for the 30s floor but
     will overstate a track abandoned at 45 seconds.

  2. FIFTY ITEMS, ONE PAGE. Only the 50 most recent plays are reachable. This
     history averages ~36 plays a day, so polling less often than daily WILL
     silently drop plays. The run warns when a result comes back full, which is
     the signal that older plays fell off the end before it got there.

First run opens a browser for consent. The token is stored under `.cache/` and
refreshed automatically after that; `.cache/` is gitignored.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import socketserver
import threading
import urllib.parse
import webbrowser
from datetime import datetime, timezone

import duckdb
import requests

import config

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
RECENT_URL = "https://api.spotify.com/v1/me/player/recently-played"

SCOPE = "user-read-recently-played"
REDIRECT_URI = "http://127.0.0.1:3000"   # must match the app's setting exactly
REDIRECT_PORT = 3000

TOKEN_FILE = config.CACHE_DIR / "spotify_token.json"
POLLED_PARQUET = config.DATA_DIR / "polled_plays.parquet"

PAGE_LIMIT = 50


# --------------------------------------------------------------------------
# OAuth (Authorization Code with PKCE)
# --------------------------------------------------------------------------


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Catches Spotify's redirect and hands the code back to the main thread."""

    code: str | None = None
    error: str | None = None

    def do_GET(self):  # noqa: N802 - stdlib naming
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.code = (qs.get("code") or [None])[0]
        _CallbackHandler.error = (qs.get("error") or [None])[0]
        body = ("Authorised — you can close this tab and return to the terminal."
                if _CallbackHandler.code else
                f"Authorisation failed: {_CallbackHandler.error}")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            f"<html><body style='font-family:system-ui;padding:3rem'>"
            f"<h2>{body}</h2></body></html>".encode()
        )

    def log_message(self, *_):  # silence the default request logging
        return


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def save_tokens(d: dict) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(d), encoding="utf-8")
    os.chmod(TOKEN_FILE, 0o600)


def load_tokens() -> dict:
    if not TOKEN_FILE.exists():
        return {}
    try:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def authorize(client_id: str) -> dict:
    """Interactive first-run consent. Uses PKCE, so no client secret is needed."""
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": client_id, "response_type": "code",
        "redirect_uri": REDIRECT_URI, "scope": SCOPE, "state": state,
        "code_challenge_method": "S256", "code_challenge": challenge,
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", REDIRECT_PORT), _CallbackHandler)
    threading.Thread(target=httpd.handle_request, daemon=True).start()

    print("\nOpening your browser to authorise access to your listening history.")
    print("If it does not open, paste this into a browser:\n")
    print(f"  {url}\n")
    webbrowser.open(url)

    print("Waiting for the redirect ...")
    for _ in range(180):
        if _CallbackHandler.code or _CallbackHandler.error:
            break
        threading.Event().wait(1)
    httpd.server_close()

    if _CallbackHandler.error or not _CallbackHandler.code:
        raise SystemExit(f"Authorisation failed: {_CallbackHandler.error or 'timed out'}")

    r = requests.post(TOKEN_URL, timeout=30, data={
        "grant_type": "authorization_code", "code": _CallbackHandler.code,
        "redirect_uri": REDIRECT_URI, "client_id": client_id,
        "code_verifier": verifier,
    })
    if r.status_code != 200:
        raise SystemExit(f"Token exchange failed ({r.status_code}): {r.text[:200]}")
    tok = r.json()
    save_tokens(tok)
    print("Authorised. Token stored in .cache/ (gitignored).\n")
    return tok


def access_token(client_id: str) -> str:
    """A usable access token, refreshing or re-authorising as needed."""
    tok = load_tokens()
    if not tok.get("refresh_token"):
        tok = authorize(client_id)
        return tok["access_token"]

    r = requests.post(TOKEN_URL, timeout=30, data={
        "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
        "client_id": client_id,
    })
    if r.status_code != 200:
        print(f"  refresh failed ({r.status_code}); re-authorising")
        return authorize(client_id)["access_token"]
    new = r.json()
    # Spotify does not always reissue the refresh token; keep the old one.
    new.setdefault("refresh_token", tok["refresh_token"])
    save_tokens(new)
    return new["access_token"]


# --------------------------------------------------------------------------
# Fetch and store
# --------------------------------------------------------------------------


def last_seen(con: duckdb.DuckDBPyConnection) -> datetime | None:
    """The newest play already known, across the export and previous polls."""
    stamps = []
    for path in (config.PLAYS_RAW_PARQUET, POLLED_PARQUET):
        if path.exists():
            ts = con.execute(f"SELECT max(ts) FROM '{path}'").fetchone()[0]
            if ts:
                stamps.append(ts)
    if not stamps:
        return None
    newest = max(stamps)
    return newest.replace(tzinfo=timezone.utc) if newest.tzinfo is None else newest


def fetch_recent(token: str, after: datetime | None) -> tuple[list[dict], bool]:
    """Recently played since `after`. Returns (rows, page_was_full)."""
    params = {"limit": PAGE_LIMIT}
    if after:
        params["after"] = int(after.timestamp() * 1000)
    r = requests.get(RECENT_URL, headers={"Authorization": f"Bearer {token}"},
                     params=params, timeout=30)
    if r.status_code == 403:
        raise SystemExit(
            "403 from recently-played. The app needs the "
            "'user-read-recently-played' scope — run with --logout and retry.")
    if r.status_code != 200:
        raise SystemExit(f"recently-played failed ({r.status_code}): {r.text[:200]}")
    items = r.json().get("items", [])
    return items, len(items) >= PAGE_LIMIT


def to_rows(items: list[dict]) -> list[dict]:
    """Normalise into the plays_raw shape, flagging the estimated duration."""
    rows = []
    for it in items:
        tr = it.get("track") or {}
        if not tr.get("uri"):
            continue
        artists = tr.get("artists") or []
        album = tr.get("album") or {}
        album_artists = album.get("artists") or []
        rows.append({
            "ts": datetime.fromisoformat(
                it["played_at"].replace("Z", "+00:00")).replace(tzinfo=None),
            "platform": "spotify-api-poll",
            # Spotify only surfaces a track here once it has played past ~30s,
            # so full duration is a defensible estimate — but it IS an estimate.
            "ms_played": int(tr.get("duration_ms") or 0),
            "ms_played_estimated": True,
            "conn_country": None,
            "artist_name": (album_artists[0]["name"] if album_artists
                            else (artists[0]["name"] if artists else None)),
            "track_name": tr.get("name"),
            "album_name": album.get("name"),
            "spotify_track_uri": tr.get("uri"),
            # The API DOES give real track artists, unlike the export. Kept so a
            # later stage can use it to repair the album-artist flaw.
            "track_artists": ", ".join(a["name"] for a in artists) or None,
            "reason_start": (it.get("context") or {}).get("type"),
            "reason_end": None,
            "shuffle": None, "skipped": None, "offline": None,
            "incognito_mode": None,
            "source_kind": "poll",
            "content_type": "music",
        })
    return rows


def store(con: duckdb.DuckDBPyConnection, rows: list[dict]) -> int:
    """Append, de-duplicating on the play identity. Idempotent like Stage 1."""
    import pandas as pd

    incoming = pd.DataFrame(rows)
    con.register("incoming", incoming)
    if POLLED_PARQUET.exists():
        con.execute(f"CREATE OR REPLACE TABLE polled AS SELECT * FROM '{POLLED_PARQUET}'")
        con.execute("INSERT INTO polled SELECT * FROM incoming")
    else:
        con.execute("CREATE OR REPLACE TABLE polled AS SELECT * FROM incoming")

    before = con.execute("SELECT count(*) FROM polled").fetchone()[0]
    con.execute("""
        CREATE OR REPLACE TABLE polled AS
        SELECT * FROM polled
        QUALIFY row_number() OVER (PARTITION BY ts, spotify_track_uri) = 1
    """)
    after = con.execute("SELECT count(*) FROM polled").fetchone()[0]
    con.execute(f"COPY (SELECT * FROM polled ORDER BY ALL) TO '{POLLED_PARQUET}' "
                f"(FORMAT PARQUET, COMPRESSION ZSTD)")
    return before - after


def status(con: duckdb.DuckDBPyConnection) -> None:
    print("\n" + "=" * 70)
    print("STAGE 6 — POLLED HISTORY")
    print("=" * 70)
    print(f"authorised : {'yes' if load_tokens().get('refresh_token') else 'no'}")
    if not POLLED_PARQUET.exists():
        print("polled     : nothing stored yet")
    else:
        n, lo, hi = con.execute(
            f"SELECT count(*), min(ts), max(ts) FROM '{POLLED_PARQUET}'").fetchone()
        print(f"polled     : {n:,} plays   {lo:%Y-%m-%d %H:%M} -> {hi:%Y-%m-%d %H:%M}")
    if config.PLAYS_RAW_PARQUET.exists():
        n, hi = con.execute(
            f"SELECT count(*), max(ts) FROM '{config.PLAYS_RAW_PARQUET}'").fetchone()
        print(f"export     : {n:,} plays   through {hi:%Y-%m-%d}")
    print("=" * 70)


def main() -> None:
    ap = argparse.ArgumentParser(description="Stage 6 history poller")
    ap.add_argument("--status", action="store_true", help="show state, no network")
    ap.add_argument("--logout", action="store_true", help="delete the stored token")
    args = ap.parse_args()

    config.ensure_dirs()
    con = duckdb.connect()

    if args.logout:
        TOKEN_FILE.unlink(missing_ok=True)
        print("Stored token deleted.")
        return
    if args.status:
        status(con)
        return

    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    if not client_id:
        raise SystemExit(
            "SPOTIFY_CLIENT_ID missing from .env.\n"
            "The poller needs the Spotify app after all — user-scoped auth is the\n"
            "only way to read your own recently-played. Its redirect URI must be\n"
            f"exactly {REDIRECT_URI}")

    token = access_token(client_id)
    after = last_seen(con)
    print(f"Fetching plays since {after:%Y-%m-%d %H:%M} UTC" if after
          else "Fetching the most recent plays (no prior history found)")

    items, page_full = fetch_recent(token, after)
    rows = to_rows(items)
    if not rows:
        print("Nothing new since the last run.")
        status(con)
        return

    dupes = store(con, rows)
    print(f"\nFetched {len(items)} plays, stored {len(rows) - dupes} new "
          f"({dupes} already known).")

    if page_full:
        print("\n  ! The page came back FULL (50 items), so older plays may have\n"
              "    fallen off the end before this run. Poll at least daily —\n"
              "    this history averages ~36 plays a day.")
    print("\n  Note: polled rows carry an ESTIMATED ms_played (full track length).\n"
          "  Spotify does not report actual play duration here.")
    status(con)


if __name__ == "__main__":
    main()
