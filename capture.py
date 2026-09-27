"""Stage 11 — capture: read what gets saved, render the newest of it.

    .venv/bin/python capture.py --source "My dump"                  # dry run
    .venv/bin/python capture.py --source "My dump" --source liked   # two sources
    .venv/bin/python capture.py --source "My dump" --write          # refresh Fresh · Claude

The playlist SJ saves into is a capture log, not something anyone plays.
Adding to it works like a heart — 242 of Driving #2's 475 tracks were played
BEFORE they were added — and its music is heard, just not from it: of the last
50 plays, 9 were its tracks and none came from it. In position order it opens
on the stalest music (116 of its first 117 slots are four bulk pastes from
January 2025), and on shuffle it is a 28-hour genre salad in which the newest
100 tracks, which held 57% of its 2026 plays, get 21% of the picks.

So this stage READS the source and renders its newest FRESH_SIZE tracks, newest
first, into one playlist it owns: Fresh · Claude. That fixes the shuffle case —
shuffling a bounded newest-100 is shuffling what is actually being played.

THE SOURCE IS NEVER WRITTEN.
    No reorder, prune, dedupe or rename. A `uris` replace restamps added_at on
    every track and destroys the only dated record of what was chosen when —
    the very column this stage sorts on. The target is refused wherever it
    could be a source: by name before anything is read, by stored ID once the
    sources are known, and by resolved ID after ensure_playlist's exact-name
    fallback. A test asserts that no PUT or POST ever carries a source ID.

LIKED SONGS IS READ-ONLY TOO, AND EASIER TO DAMAGE THAN A PLAYLIST.
    Its save endpoint accepts no timestamp, and re-saving a track that is
    already liked RE-STAMPS its added_at to now: the no-op probe of 2026-09-27
    moved "My Home" to the top of Liked Songs. A "harmless" save rewrites the
    date this stage orders on. Only the library READ scope is ever requested,
    and only when `liked` is a source, so a run on a named playlist never
    prompts for it.

capture.parquet IS A FULL SNAPSHOT, NOT A CACHE.
    The append-only rule protects answers that cost MusicBrainz requests; these
    dates live on Spotify and a re-read costs one request per 100 tracks. A
    save removed at the source disappears from the next snapshot, as it should.
    What was actually rendered is kept anyway, as fresh_selection rows in
    data/playlists.parquet.

Dry run by default, like Stage 9: until --write, nothing is sent to Spotify and
no state or archive row is written. Every Stage 8 lifecycle rule holds — the
target is found by stored ID, then exact name, then created; it is snapshotted
before every replace; nothing is ever deleted or unfollowed.

Outputs: data/capture.parquet; with --write, Fresh · Claude,
data/capture_state.json and rows in data/playlists.parquet.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

import duckdb

import config
from consolidate import find_playlist, gentle_token, read_playlist
from playlists import (
    FORBIDDEN_NOTE,
    SP_API,
    Spotify,
    ensure_playlist,
    playlist_items,
    write_archive,
)
from poll import ARTIST_SEP

# Read is enough for a dry run. Writes need BOTH modify scopes, as Stage 10's
# do: Spotify reports these playlists as public whatever was asked for.
SCOPES_READ = "playlist-read-private playlist-read-collaborative"
SCOPES_WRITE = SCOPES_READ + " playlist-modify-private playlist-modify-public"
# Added only when Liked Songs is a source. Never user-library-modify: this
# stage has no reason to hold a scope that can re-stamp a heart.
SCOPE_LIBRARY_READ = "user-library-read"

LIKED = "liked"              # the --source keyword for Liked Songs
OWN_SUFFIX = " · Claude"     # every pipeline-owned playlist carries it
# /me/tracks answers 400 "Invalid limit" to 51 (verified 2026-09-27), unlike a
# playlist's /items, which pages at 100.
LIKED_PAGE = 50
# One PUT /items carries at most 100 URIs and replaces the whole playlist.
# Above that, a replace becomes PUT-then-POST, a half-written playlist is
# possible, and nothing here needs it — so --size is refused, not chunked.
SP_PUT_MAX_URIS = 100

ROW_KEYS = ("spotify_track_uri", "track_name", "track_artists", "duration_ms",
            "added_at", "source", "source_rank")


# --------------------------------------------------------------------------
# Refusals — before anything is read or requested
# --------------------------------------------------------------------------


def refuse_own(source: str) -> None:
    """A pipeline playlist is output, never capture.

    Reading Workout · Claude as "what SJ saved" would feed the pipeline's own
    choices back to it as taste, and a Fresh · Claude source would render
    itself. Trailing spaces are stripped for this test only — it is a refusal,
    so it errs on the side of refusing.
    """
    if source.rstrip().endswith(OWN_SUFFIX):
        raise SystemExit(
            f"{source!r} ends in {OWN_SUFFIX!r}: that is the pipeline's own "
            "output, and reading it as capture is circular. Name the playlist "
            "you save into.")


def check_sources(sources: list[str]) -> list[str]:
    """Validated, de-duplicated --source values, in the order given."""
    out: list[str] = []
    for s in sources:
        refuse_own(s)
        if s == config.FRESH_PLAYLIST_NAME:
            raise SystemExit(f"{s!r} is this stage's own target; it cannot also "
                             "be its source.")
        if s not in out:
            out.append(s)
    if not out:
        raise SystemExit("--source is required: name the playlist you save "
                         f"into, or {LIKED!r} for Liked Songs.")
    return out


def check_size(n: int) -> int:
    if not 1 <= n <= SP_PUT_MAX_URIS:
        raise SystemExit(
            f"--size {n} refused: it must be 1..{SP_PUT_MAX_URIS}, because one "
            "PUT replaces the whole playlist and carries at most "
            f"{SP_PUT_MAX_URIS} tracks.")
    return n


def scopes_for(sources: list[str], write: bool) -> str:
    """The narrowest grant this run needs; poll.access_token unions it with
    whatever the token already holds, so nothing another stage uses is lost."""
    scope = SCOPES_WRITE if write else SCOPES_READ
    return scope + (f" {SCOPE_LIBRARY_READ}" if LIKED in sources else "")


def guard_target(meta: list[dict], state: dict) -> None:
    """Refuse when the target could be a source, before anything is written.

    The name check repeats check_sources against what Spotify actually
    returned. The stored-ID check catches a hand-edited or stale
    capture_state.json, which ensure_playlist would otherwise trust first.
    """
    names = {m["name"] for m in meta}
    if config.FRESH_PLAYLIST_NAME in names:
        raise SystemExit(f"{config.FRESH_PLAYLIST_NAME!r} is a source; refusing "
                         "to write it.")
    stored = (state.get("fresh") or {}).get("id")
    hit = next((m for m in meta if m["id"] == stored), None)
    if stored and hit:
        raise SystemExit(
            f"{config.CAPTURE_STATE_JSON.name} names {hit['name']!r} ({stored}) "
            "as the Fresh playlist, but it is a source. Refusing to write it; "
            "remove the 'fresh' entry from that file to create a new target.")


# --------------------------------------------------------------------------
# Reading the source
# --------------------------------------------------------------------------


def read_liked(sp) -> list[dict]:
    """Every Liked Songs track, newest first, as Spotify serves it.

    Verified live 2026-09-27: GET /me/tracks survived the February 2026 rename,
    pages at 50 via `next`, and — unlike a playlist's `/items` — still nests
    each track under `track`, not `item`. /me/tracks/contains and PUT /me/tracks
    answer 403; their replacements live under /me/library, which this stage
    never calls.

    Same keys as consolidate.read_playlist, so the two sources are one shape
    from here on. Two failures refuse rather than return short, because a
    short read becomes a full snapshot missing real saves: an error envelope
    part-way through, and a page whose rows carry no `track` key at all — the
    shape of the next nesting rename, which would otherwise read as an empty
    library.
    """
    out: list[dict] = []
    resp = sp.get(f"/me/tracks?limit={LIKED_PAGE}", params=None)
    while isinstance(resp, dict) and "_status" not in resp:
        page = resp.get("items", [])
        if page and not any("track" in it for it in page):
            raise SystemExit(
                "Liked Songs rows carry no 'track' key (keys: "
                f"{sorted(page[0])}). The nesting may have moved, as playlists' "
                "did in Feb 2026 (track -> item); refusing to read it as empty.")
        for it in page:
            t = it.get("track") or {}
            if not t.get("uri"):
                continue  # unavailable rows come back with a null track
            out.append({
                "spotify_track_uri": t["uri"],
                "track_name": t.get("name") or "",
                "artists": [a.get("name", "") for a in t.get("artists", [])],
                "added_at": it.get("added_at") or "",
                "duration_ms": t.get("duration_ms"),
            })
        nxt = resp.get("next")
        if not nxt:
            break
        resp = sp.get(nxt.removeprefix(SP_API), params=None)
    if not isinstance(resp, dict) or "_status" in resp:
        raise SystemExit(
            f"Liked Songs read failed part-way: {resp}" + (
                "\n  /me/tracks is the verified read path (2026-09-27); a 403 "
                "means the token lacks user-library-read, or the path moved."
                if isinstance(resp, dict) and resp.get("_status") == 403 else ""))
    return out


def _read_raw(sp, meta: dict) -> list[dict]:
    """The source's tracks, NEWEST FIRST whichever kind it is.

    Liked Songs arrives newest first. A playlist arrives in position order, and
    for a dump that is only ever appended to position IS age — 2 of Driving
    #2's 475 dates are out of order — so it is reversed. That reversal is what
    breaks ties: the January 2025 bulk pastes share an added_at to the second,
    and within one the later position is the later add.
    """
    if meta["kind"] == "liked":
        return read_liked(sp)
    return list(reversed(read_playlist(sp, meta["id"])))


def _rows(meta: dict, raw: list[dict]) -> list[dict]:
    """Capture rows in ROW_KEYS shape; source_rank 0 is the newest.

    Only spotify:track: URIs survive. A local file carries a spotify:local: URI
    and an episode a spotify:episode: one; neither can go into a track
    playlist or be matched against the history.
    """
    kept = [r for r in raw if r["spotify_track_uri"].startswith("spotify:track:")]
    meta["n_read"], meta["n_skipped"] = len(raw), len(raw) - len(kept)
    return [{
        "spotify_track_uri": r["spotify_track_uri"],
        "track_name": r["track_name"],
        # 0x1f, never ", " — "Tyler, The Creator" is one artist.
        "track_artists": ARTIST_SEP.join(r["artists"]) or None,
        "duration_ms": r.get("duration_ms"),
        "added_at": r["added_at"] or None,
        "source": meta["source"],
        "source_rank": i,
    } for i, r in enumerate(kept)]


def read_source(sp, source: str) -> tuple[dict, list[dict]]:
    """(meta, rows) for one --source: `liked`, or a playlist's exact name.

    `liked` always means Liked Songs, even if a playlist carries that name. A
    playlist goes through consolidate.find_playlist, which is exact including
    case and errors on a missing or duplicated name: a near-miss is somebody's
    other playlist, and rendering the wrong source is silent.
    """
    refuse_own(source)
    if source == LIKED:
        meta = {"kind": "liked", "id": LIKED, "name": "Liked Songs"}
    else:
        pl = find_playlist(sp, source)
        meta = {"kind": "playlist", "id": pl["id"], "name": pl["name"]}
    meta["source"] = source
    return meta, _rows(meta, _read_raw(sp, meta))


# --------------------------------------------------------------------------
# The snapshot and the selection — DuckDB, as everywhere else
# --------------------------------------------------------------------------


def write_capture(con: duckdb.DuckDBPyConnection, rows: list[dict]) -> int:
    """Union the sources, one row per URI, and write capture.parquet whole.

    The NEWEST added_at wins a URI that several sources carry: the latest time
    it was chosen is the one that says it is fresh. Ties fall to the source
    name and then source_rank, so the choice never depends on read order.
    """
    con.execute("""
        CREATE OR REPLACE TABLE capture_raw (
            spotify_track_uri VARCHAR, track_name VARCHAR, track_artists VARCHAR,
            duration_ms BIGINT, added_at TIMESTAMP, source VARCHAR,
            source_rank INTEGER)
    """)
    if rows:
        con.executemany(
            f"INSERT INTO capture_raw VALUES ({', '.join('?' for _ in ROW_KEYS)})",
            [[r[k] for k in ROW_KEYS] for r in rows])
    con.execute("""
        CREATE OR REPLACE TABLE capture AS
        SELECT * FROM capture_raw
        QUALIFY row_number() OVER (
            PARTITION BY spotify_track_uri
            ORDER BY added_at DESC NULLS LAST, source, source_rank) = 1
    """)
    config.CAPTURE_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    # URI is unique after the dedupe, so ORDER BY ALL is a total order.
    con.execute(f"COPY (SELECT * FROM capture ORDER BY ALL) TO "
                f"'{config.CAPTURE_PARQUET}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    return con.execute("SELECT count(*) FROM capture").fetchone()[0]


def select_fresh(con: duckdb.DuckDBPyConnection, n: int) -> list[dict]:
    """The n newest captures, newest first, in a total order.

    added_at alone is not one: the bulk pastes tie to the second, and a LIMIT
    through a tie picks at random. source_rank breaks it by position, the URI
    breaks anything left.
    """
    cur = con.execute(f"""
        SELECT {', '.join(ROW_KEYS)} FROM capture
        ORDER BY added_at DESC NULLS LAST, source_rank, spotify_track_uri
        LIMIT ?
    """, [n])
    return [dict(zip(ROW_KEYS, r), position=i) for i, r in enumerate(cur.fetchall())]


def count_export_played(con: duckdb.DuckDBPyConnection, uris: list[str]) -> int:
    """How many of these tracks have at least one EXPORT play.

    Export rows only (`source_kind = 'audio'`). Polled rows carry estimated
    durations and no reason_end, and past the export's coverage they are
    provisional — the same cut every stage applies to time.
    """
    if not uris:
        return 0
    return con.execute("""
        SELECT count(*) FROM (SELECT DISTINCT unnest(?::VARCHAR[]) AS u) s
        WHERE EXISTS (SELECT 1 FROM plays p
                      WHERE p.spotify_track_uri = s.u AND p.source_kind = 'audio')
    """, [list(uris)]).fetchone()[0]


# --------------------------------------------------------------------------
# Spotify writes — Stage 8's lifecycle, for one playlist
# --------------------------------------------------------------------------


def load_state() -> dict:
    if not config.CAPTURE_STATE_JSON.exists():
        return {}
    try:
        return json.loads(config.CAPTURE_STATE_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(d: dict) -> None:
    config.CAPTURE_STATE_JSON.parent.mkdir(parents=True, exist_ok=True)
    config.CAPTURE_STATE_JSON.write_text(
        json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")


def _artists(track_artists: str | None) -> str:
    """Display form for the archive, like playlist_items' snapshot rows. The
    0x1f list stays in capture.parquet; this column is never split."""
    return ", ".join((track_artists or "").split(ARTIST_SEP))


def publish(sp, con, meta: list[dict], tracks: list[dict]) -> str:
    """Refresh Fresh · Claude from `tracks`; `meta` is every source's meta.

    Modelled on running.publish for one playlist. The snapshot of what is
    about to be overwritten reaches the archive BEFORE the PUT, not after it
    with the selection: a PUT that lands but answers badly would otherwise
    leave the replaced contents unrecorded.
    """
    state = load_state()
    guard_target(meta, state)
    name = config.FRESH_PLAYLIST_NAME
    pid = ensure_playlist(sp, "fresh", name, state)
    # ensure_playlist trusts the stored ID, then the exact name. Either can
    # land on a source — a stale state file, or a source renamed to the
    # target's name after it was read — so the resolved ID is checked too.
    hit = next((m for m in meta if m["id"] == pid), None)
    if hit:
        raise SystemExit(f"The Fresh playlist resolved to the source "
                         f"{hit['name']!r} ({pid}). Refusing to write it.")

    run_date = date.today().isoformat()
    write_archive(con, [{
        "run_date": run_date, "kind": "fresh_pre_replace_snapshot",
        "gap_tag": "fresh", "playlist_id": pid, "position": i, "slot": None,
        "artist_name": old["artist_name"], "track_name": old["track_name"],
        "spotify_track_uri": old["uri"], "source": "spotify",
    } for i, old in enumerate(playlist_items(sp, pid))])

    uris = [t["spotify_track_uri"] for t in tracks]
    if len(uris) > SP_PUT_MAX_URIS:
        raise SystemExit(f"{len(uris)} tracks will not fit one PUT; refusing.")
    # PUT replaces the whole playlist in one call; /items, not /tracks.
    resp = sp.put(f"/playlists/{pid}/items", json={"uris": uris})
    if not isinstance(resp, dict) or "_status" in resp:
        raise SystemExit(f"Could not write {name!r}: {resp}\n" + (
            FORBIDDEN_NOTE if isinstance(resp, dict)
            and resp.get("_status") == 403 else ""))

    # `public: false` does not stick; the description must not claim privacy.
    hours = sum(t["duration_ms"] or 0 for t in tracks) / 3_600_000
    sp.put(f"/playlists/{pid}", json={
        "name": name,
        "description": config.FRESH_PLAYLIST_DESCRIPTION_TEMPLATE.format(
            n=len(tracks), hours=hours, date=run_date),
    })

    write_archive(con, [{
        "run_date": run_date, "kind": "fresh_selection", "gap_tag": "fresh",
        "playlist_id": pid, "position": t["position"], "slot": None,
        "artist_name": _artists(t["track_artists"]),
        "track_name": t["track_name"],
        "spotify_track_uri": t["spotify_track_uri"], "source": t["source"],
    } for t in tracks])
    state["fresh"] = {"id": pid, "name": name}
    # Kept so poll.py --status can label a play from the source by name.
    state["sources"] = [{k: m[k] for k in ("kind", "id", "name")} for m in meta]
    save_state(state)
    return pid


def source_check(sp, meta: list[dict], rows: list[dict]) -> dict[str, tuple]:
    """Re-read every source after a write: name -> (before, after, same).

    The code never writes a source and a test holds it to that; this is the
    check against the live account. It compares URIs AND added_at, since a
    write that touched the source would most likely restamp dates rather than
    change the count. A save made while this ran would also show as a change.
    """
    out = {}
    for m in meta:
        before = sorted((r["spotify_track_uri"], r["added_at"] or "")
                        for r in rows if r["source"] == m["source"])
        again = _rows(dict(m), _read_raw(sp, m))
        after = sorted((r["spotify_track_uri"], r["added_at"] or "") for r in again)
        out[m["name"]] = (len(before), len(after), before == after)
    return out


# --------------------------------------------------------------------------
# Run and report
# --------------------------------------------------------------------------


def run(sp, con: duckdb.DuckDBPyConnection, sources: list[str], size: int,
        write: bool) -> dict:
    """Read, snapshot, select, and with `write` publish. main() minus auth."""
    metas, rows = [], []
    for s in sources:
        meta, got = read_source(sp, s)
        metas.append(meta)
        rows.extend(got)
    guard_target(metas, load_state())

    write_capture(con, rows)
    fresh = select_fresh(con, size)
    out = {"metas": metas, "fresh": fresh, "playlist_id": None,
           "sources_unchanged": None, "source_check": {}}
    # An empty selection is a failed or empty read, never an instruction to
    # blank the playlist.
    if write and fresh:
        out["playlist_id"] = publish(sp, con, metas, fresh)
        out["source_check"] = source_check(sp, metas, rows)
        out["sources_unchanged"] = all(v[2] for v in out["source_check"].values())

    if config.PLAYS_PARQUET.exists():
        con.execute(f"CREATE OR REPLACE VIEW plays AS SELECT * FROM "
                    f"'{config.PLAYS_PARQUET}'")
    report(con, out, write)
    return out


def _day(ts) -> str:
    return ts.strftime("%Y-%m-%d") if ts else "—"


def report(con: duckdb.DuckDBPyConnection, out: dict, write: bool) -> None:
    fresh, name = out["fresh"], config.FRESH_PLAYLIST_NAME
    print("\n" + "=" * 72)
    print("STAGE 11 — CAPTURE" + ("" if write else "  (dry run)"))
    print("=" * 72)

    print("\nsources (read only — never written)")
    for m in out["metas"]:
        kept = con.execute("SELECT count(*) FROM capture WHERE source = ?",
                           [m["source"]]).fetchone()[0]
        lo, hi = con.execute("SELECT min(added_at), max(added_at) FROM capture_raw "
                             "WHERE source = ?", [m["source"]]).fetchone()
        print(f"  {m['kind']:<8} {m['name']!r}: {m['n_read']} read, "
              f"{m['n_skipped']} non-track skipped, {kept} kept after dedupe")
        # The largest same-day stamp is where dates stop meaning history: a
        # bulk paste, or a backfill that stamped every save with its own day.
        bulk = con.execute("""
            SELECT CAST(added_at AS DATE) AS d, count(*) AS n FROM capture_raw
            WHERE source = ? AND added_at IS NOT NULL
            GROUP BY 1 ORDER BY n DESC, d DESC LIMIT 1
        """, [m["source"]]).fetchone()
        print(f"           added {_day(lo)} → {_day(hi)}"
              + (f"; largest same-day stamp {bulk[0]} × {bulk[1]}"
                 if bulk and bulk[1] > 1 else ""))

    total, raw = (con.execute("SELECT count(*) FROM capture").fetchone()[0],
                  con.execute("SELECT count(*) FROM capture_raw").fetchone()[0])
    lo, hi = con.execute("SELECT min(added_at), max(added_at) FROM capture").fetchone()
    print(f"\n{config.CAPTURE_PARQUET.name}: {total} tracks "
          f"({raw - total} duplicate(s) across sources folded), "
          f"added {_day(lo)} → {_day(hi)}")
    print("adds per month, last 6:")
    for month, n in con.execute("""
        SELECT strftime(m, '%Y-%m'), count(c.spotify_track_uri)
        FROM range(date_trunc('month', current_date) - INTERVAL 5 MONTH,
                   date_trunc('month', current_date) + INTERVAL 1 MONTH,
                   INTERVAL 1 MONTH) r(m)
        LEFT JOIN capture c ON date_trunc('month', c.added_at) = r.m
        GROUP BY 1 ORDER BY 1
    """).fetchall():
        print(f"   {month}  {n:>4}")

    if not fresh:
        print(f"\n⚠ nothing to render — {name!r} left untouched")
        print("=" * 72)
        return
    hours = sum(t["duration_ms"] or 0 for t in fresh) / 3_600_000
    print(f"\n{name} — {len(fresh)} tracks, {hours:.1f} h, newest "
          f"{_day(fresh[0]['added_at'])}, oldest {_day(fresh[-1]['added_at'])}")
    has_plays = con.execute("SELECT count(*) FROM information_schema.tables "
                            "WHERE table_name = 'plays'").fetchone()[0]
    if has_plays:
        k = count_export_played(con, [t["spotify_track_uri"] for t in fresh])
        print(f"  {k} of {len(fresh)} have at least one export play")
    else:
        print(f"  ({config.PLAYS_PARQUET.name} absent — export-play count skipped)")
    for t in fresh:
        artists = _artists(t["track_artists"])
        print(f"  {t['position']:>3}  {_day(t['added_at'])}  "
              f"{(t['duration_ms'] or 0) / 60000:4.1f}m  "
              f"{artists[:26]:<26} {(t['track_name'] or '')[:36]}")

    if not write:
        print("\nDRY RUN — nothing written to Spotify, no state or archive rows. "
              "Re-run with --write to refresh it.")
    elif out["playlist_id"]:
        print(f"\nwrote {len(fresh)} tracks to {name!r} ({out['playlist_id']})")
        for src, (before, after, same) in out["source_check"].items():
            print(f"  source {src!r}: {before} tracks before, {after} after, "
                  + ("added_at unchanged" if same else
                     "CHANGED — Stage 11 sent it no write; a save made while "
                     "this ran would explain it"))
        print(f"Archive -> {config.PLAYLISTS_PARQUET}")
        print(f"State   -> {config.CAPTURE_STATE_JSON}")
    print("=" * 72)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", action="append", default=[], metavar="NAME",
                    required=True,
                    help="the playlist you save into, by EXACT name, or "
                         f"'{LIKED}' for Liked Songs. Repeatable; a track in "
                         "several keeps its newest added_at.")
    ap.add_argument("--size", type=int, default=config.FRESH_SIZE,
                    help=f"tracks to render (default {config.FRESH_SIZE}, "
                         f"at most {SP_PUT_MAX_URIS})")
    ap.add_argument("--write", action="store_true",
                    help=f"refresh {config.FRESH_PLAYLIST_NAME!r}; without it "
                         "nothing is written to Spotify")
    args = ap.parse_args(argv)

    # Every refusal comes before the token, so a refused run never prompts.
    sources = check_sources(args.source)
    size = check_size(args.size)

    client_id = os.getenv("SPOTIFY_CLIENT_ID", "")
    if not client_id:
        sys.exit("SPOTIFY_CLIENT_ID is not set; see .env.example.")
    config.ensure_dirs()

    sp = Spotify(gentle_token(client_id, scopes_for(sources, args.write)))
    run(sp, duckdb.connect(), sources, size, args.write)


if __name__ == "__main__":
    main()
