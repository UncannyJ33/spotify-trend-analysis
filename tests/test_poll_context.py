"""tests/test_poll_context.py — Stage 6 records which playlist/context a play came from.

poll.to_rows gains context_uri from the recently-played API's `context.uri`.
store() must:
  - migrate an existing old-schema polled_plays.parquet with
    ALTER TABLE ... ADD COLUMN IF NOT EXISTS context_uri VARCHAR,
  - insert BY NAME (positional INSERT breaks once the schema widens),
  - CAST context_uri AS VARCHAR on every insert, including the first-ever
    CREATE, because a pandas column that is all None arrives in DuckDB typed
    INTEGER (as reason_end and shuffle already do) rather than VARCHAR,
  - dedupe with ORDER BY context_uri NULLS LAST, so a re-fetched play keeps
    its non-null context over a later, context-less re-fetch of the same play.

Offline: everything happens in temp Parquet files; poll.POLLED_PARQUET is
pointed at them, never the real one.
"""
import sys
sys.path.insert(0, ".")
import pathlib
import tempfile
from datetime import datetime

import duckdb

import poll

failures = []
def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")


d = pathlib.Path(tempfile.mkdtemp())
poll.POLLED_PARQUET = d / "polled_plays.parquet"


def old_row(ts, uri, **overrides):
    """A row in the pre-Task-1.3 shape: no context_uri key at all."""
    row = {
        "ts": ts, "platform": "spotify-api-poll", "ms_played": 180000,
        "ms_played_estimated": True, "conn_country": None,
        "artist_name": "Artist", "track_name": "Track", "album_name": "Album",
        "spotify_track_uri": uri, "track_artists": "Artist",
        "reason_start": "playlist", "reason_end": None,
        "shuffle": None, "skipped": None, "offline": None,
        "incognito_mode": None, "source_kind": "poll", "content_type": "music",
    }
    row.update(overrides)
    return row


def new_row(ts, uri, context_uri, **overrides):
    row = old_row(ts, uri, **overrides)
    row["context_uri"] = context_uri
    return row


con = duckdb.connect()

# --- 1. Rows stored by the OLD to_rows shape create an old-schema file ----
dupes = poll.store(con, [old_row(datetime(2026, 9, 1, 12, 0), "spotify:track:aaa")])
check("first store: no dupes", dupes, 0)
cols = {c[0] for c in con.execute(
    f"DESCRIBE SELECT * FROM '{poll.POLLED_PARQUET}'").fetchall()}
check("old-schema file has no context_uri column yet",
      "context_uri" in cols, False)

# --- 2. New rows append; old rows read NULL, new rows keep their URI ------
dupes = poll.store(con, [new_row(datetime(2026, 9, 2, 12, 0), "spotify:track:bbb",
                                  "spotify:playlist:2e3BtqEo7Y7sdDKzzyHkez")])
check("second store: no dupes", dupes, 0)
got = con.execute(
    f"SELECT spotify_track_uri, context_uri FROM '{poll.POLLED_PARQUET}' ORDER BY ts"
).fetchall()
check("old row's context_uri backfilled to NULL",
      got[0], ("spotify:track:aaa", None))
check("new row keeps its context_uri",
      got[1], ("spotify:track:bbb", "spotify:playlist:2e3BtqEo7Y7sdDKzzyHkez"))

# --- 3. A batch with every context null must not retype the column --------
dupes = poll.store(con, [
    new_row(datetime(2026, 9, 3, 12, 0), "spotify:track:ccc", None),
    new_row(datetime(2026, 9, 3, 13, 0), "spotify:track:ddd", None),
])
check("all-null-context batch: no dupes", dupes, 0)
coltype = con.execute(
    f"SELECT typeof(context_uri) FROM '{poll.POLLED_PARQUET}' LIMIT 1").fetchone()[0]
check("context_uri column is still VARCHAR after an all-null batch",
      coltype, "VARCHAR")

# --- 4. Re-storing the same (ts, uri) is still a dupe; non-null context wins
resend = old_row(datetime(2026, 9, 2, 12, 0), "spotify:track:bbb")
resend["context_uri"] = None   # re-fetched later with no context this time
dupes = poll.store(con, [resend])
check("re-fetch of a known play is counted as a dupe", dupes, 1)
kept = con.execute(
    f"SELECT context_uri FROM '{poll.POLLED_PARQUET}' "
    f"WHERE spotify_track_uri = 'spotify:track:bbb'").fetchone()[0]
check("the earlier non-null context wins over the dupe's NULL",
      kept, "spotify:playlist:2e3BtqEo7Y7sdDKzzyHkez")

# --- 5. Nothing lost: total rows stored equals distinct (ts, uri) pairs ---
n = con.execute(f"SELECT count(*) FROM '{poll.POLLED_PARQUET}'").fetchone()[0]
check("row count matches the 4 distinct plays stored (aaa,bbb,ccc,ddd)", n, 4)

# --- extra: the first-ever CREATE path also casts, even an all-null batch -
fresh = pathlib.Path(tempfile.mkdtemp()) / "polled_plays_fresh.parquet"
poll.POLLED_PARQUET = fresh
con2 = duckdb.connect()
poll.store(con2, [
    new_row(datetime(2026, 9, 4, 9, 0), "spotify:track:eee", None),
    new_row(datetime(2026, 9, 4, 9, 5), "spotify:track:fff", None),
])
fresh_type = con2.execute(
    f"SELECT typeof(context_uri) FROM '{fresh}' LIMIT 1").fetchone()[0]
check("a fresh CREATE from an all-null-context batch is still VARCHAR",
      fresh_type, "VARCHAR")

print()
if failures:
    print(f"{len(failures)} check(s) failed:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All checks passed.")
