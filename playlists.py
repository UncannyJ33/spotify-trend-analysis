"""Stage 8 — render the genre-gap analysis into Spotify playlists.

    .venv/bin/python playlists.py --dry-run     # selections only, no Spotify writes
    .venv/bin/python playlists.py               # build/refresh the playlists

One playlist per under-explored rising genre (Stage 7's gap analysis), capped
at N_PLAYLISTS. Each mixes ANCHOR_TRACKS familiar tracks — the listener's own
recent plays by library artists serving that genre — with discovery tracks
from Stage 5's candidate artists.

Two sources split the judgment. Spotify's search relevance ORDERS an artist's
tracks — with ListenBrainz's popularity dataset disabled it is the only
popularity signal still standing, and it carries the URI for free. MusicBrainz
says WHICH of that artist's recordings actually carry the gap genre
(`arid AND tag:`), and those are preferred within the relevance order. So the
artist's on-genre work outranks their bigger off-genre hit, without demos and
5.1 remixes outranking everything — which is what MusicBrainz alone would give.

The playlist is a rendering: every run archives its selections locally, and a
snapshot of anything it overwrites, so Spotify never holds the only copy of
anything.

Identity is the locally stored playlist ID (data/playlist_state.json), with an
exact-name fallback on first run. The title carries a visible marker
(PLAYLIST_NAME_TEMPLATE) so pipeline-managed playlists are recognisable in the
library. This stage never deletes or unfollows a playlist.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import date

import duckdb
import requests

import config
from enrich import MB_MIN_INTERVAL, Throttled, normalise
from recommend import append_jsonl, load_jsonl
from report import pretty

SP_API = "https://api.spotify.com/v1"
SP_SCOPES = "playlist-modify-private playlist-read-private"
SP_MIN_INTERVAL = 0.25
# One page of relevance; TRACKS_PER_ARTIST picks from it. Ten, not the
# documented maximum of fifty: this app answers "Invalid limit" (400) to
# anything above 10, so a larger value fails every search rather than
# returning more. Verified by probe, 2026-07-31.
SP_SEARCH_LIMIT = 10

# ListenBrainz popularity is server-side disabled — the by-artist endpoints 500
# and the batch route answers 200 with null listen counts for every recording.
# Genre truth comes from MusicBrainz recording search instead; see the Task 1
# probe record in docs/superpowers/plans/2026-07-31-stage8-gap-playlists.md.
MB_RECORDING_URL = "https://musicbrainz.org/ws/2/recording"
MB_RECORDING_LIMIT = 100    # one page is plenty; this is a filter, not a ranking

GENRE_RECORDINGS_CACHE = config.CACHE_DIR / "genre_recordings.jsonl"
ARTIST_TRACKS_CACHE = config.CACHE_DIR / "spotify_artist_tracks.jsonl"

# Probed 2026-07-31: this Spotify app is refused every playlist-CONTENTS call
# while everything else it needs works. Scope is not the cause — the 403 stands
# with modify-private, modify-public, read-private and read-collaborative all
# granted — so the message points at the app rather than sending the reader
# back round the consent loop for nothing.
QUOTA_NOTE = """
  Spotify refused a playlist-contents call with 403.

  This app can search, read tracks, list your playlists and rename them, but is
  refused every call that reads or writes a playlist's TRACK LIST:
      POST /users/{id}/playlists      create
      PUT  /playlists/{id}/tracks     replace
      POST /playlists/{id}/tracks     add
      GET  /playlists/{id}/tracks     read contents
  It is not a scope problem: the same 403 stands with every playlist scope
  granted. The app is quota-restricted — its search limit caps at 10 instead of
  50 and track objects arrive with no popularity field, the same pattern that
  already 403s related-artists and top-tracks for this project.

  Fix it on Spotify's side (developer.spotify.com/dashboard — confirm Web API is
  enabled, then request Extended Quota Mode). No code change is needed here.

  Meanwhile `playlists.py --dry-run` does everything except the write, and
  prints exactly what each playlist would contain.
"""


# --------------------------------------------------------------------------
# Selection — pure data logic, no network
# --------------------------------------------------------------------------


def select_gaps(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """Top gap genres, hard-capped at N_PLAYLISTS by agreement."""
    cols = ["tag", "gap_score", "hours", "n_artists", "rel_change_per_year"]
    rows = con.execute(
        f"""
        SELECT {', '.join(cols)} FROM genre_gaps
        ORDER BY gap_score DESC
        LIMIT {config.N_PLAYLISTS}
        """
    ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def select_anchor_tracks(con: duckdb.DuckDBPyConnection, tag: str) -> list[dict]:
    """The listener's own recent favourites by artists serving this genre.

    Anchors are chosen at artist level (the artist carries the gap tag) and at
    track level by the listener's own recent play time — their URIs come
    straight from the export, so no search is ever needed for anchors.
    Recording-level genre matching is NOT attempted here: library tracks have
    no recording MBIDs, and resolving them is the per-track explosion this
    project has twice declined.
    """
    cols = ["artist_name", "track_name", "spotify_track_uri", "hours"]
    rows = con.execute(
        f"""
        WITH recent AS (
            SELECT p.artist_name, p.track_name, p.spotify_track_uri,
                   sum(p.played_seconds) / 3600.0 AS hours
            FROM plays p
            JOIN artist_tags t
              ON t.artist_name = p.artist_name AND t.is_genre AND t.tag = ?
            WHERE p.spotify_track_uri IS NOT NULL
              AND p.month >= (SELECT max(month) FROM plays)
                             - INTERVAL {config.ANCHOR_WINDOW_MONTHS} MONTH
            GROUP BY 1, 2, 3
        )
        SELECT artist_name, track_name, spotify_track_uri, hours
        FROM recent
        QUALIFY row_number() OVER (
            PARTITION BY artist_name ORDER BY hours DESC, spotify_track_uri
        ) <= {config.TRACKS_PER_ARTIST}
        ORDER BY hours DESC, spotify_track_uri
        LIMIT {config.ANCHOR_TRACKS}
        """,
        [tag],
    ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def select_candidates(con: duckdb.DuckDBPyConnection, tag: str,
                      tag_cache: dict) -> list[dict]:
    """Stage 5 candidates whose tag vector serves this gap, best score first.

    Library artists are excluded by normalised name: the discovery slots are
    for strangers, and the familiar ones already have the anchor slots.
    """
    known = {normalise(r[0]) for r in con.execute(
        "SELECT DISTINCT artist_name FROM plays WHERE artist_name IS NOT NULL"
    ).fetchall()}
    rows = con.execute(
        "SELECT artist_name, mbid, score FROM recommendations ORDER BY score DESC"
    ).fetchall()
    out = []
    for name, mbid, score in rows:
        if normalise(name) in known:
            continue
        tags = {t["tag"] for t in (tag_cache.get(mbid) or {}).get("tags", [])}
        if tag in tags:
            out.append({"artist_name": name, "mbid": mbid, "score": score})
    return out


def assemble(anchors: list[dict], discovery: list[dict], size: int) -> list[dict]:
    """Interleave: anchors at evenly spaced positions, discovery in between.

    A block of five familiar songs then twenty strangers reads as two playlists
    stapled together; spreading the anchors keeps a foothold always within a
    song or two. Pools that run dry shorten the playlist rather than padding
    it — a repeat is worse than a gap.
    """
    anchors = list(anchors)[:size]
    step = max(1, size // len(anchors)) if anchors else size
    anchor_pos = {i * step for i in range(len(anchors)) if i * step < size}

    out: list[dict] = []
    seen: set[str] = set()
    ai = di = 0

    def take(pool: list[dict], idx: int) -> int:
        """Advance past anything already placed; returns the usable index."""
        while idx < len(pool) and pool[idx]["spotify_track_uri"] in seen:
            idx += 1
        return idx

    for pos in range(size):
        want_anchor = ai < len(anchors) and pos in anchor_pos
        if want_anchor:
            ai = take(anchors, ai)
            want_anchor = ai < len(anchors)
        if want_anchor:
            pool, idx = anchors, ai
        else:
            di = take(discovery, di)
            if di >= len(discovery):
                break
            pool, idx = discovery, di

        row = dict(pool[idx])
        row["slot"] = "anchor" if pool is anchors else "discovery"
        row["position"] = len(out)
        seen.add(row["spotify_track_uri"])
        out.append(row)
        if pool is anchors:
            ai += 1
        else:
            di += 1
    return out


# --------------------------------------------------------------------------
# MusicBrainz — which of this artist's recordings actually serve the genre
# --------------------------------------------------------------------------


def _title_key(name: str) -> str:
    """Fold a track title for comparison across two catalogues.

    Spotify and MusicBrainz disagree constantly about the tail of a title —
    '- 2006 Remaster', '(Radio Edit)', '[VIP]'. Everything from the first such
    separator on is dropped before the usual normalise(), so the two catalogues
    are compared on the song rather than on the pressing. A title that is
    nothing BUT a suffix falls back to the whole string: folding it to "" would
    hand back a key that matches every untitled thing in the set.
    """
    head = re.split(r"\s+[-–—(\[]", name or "", maxsplit=1)[0]
    return normalise(head) or normalise(name or "")


def mb_genre_recordings(http: Throttled, artist_mbid: str, tag: str,
                        cache: dict) -> set[str]:
    """Folded titles of this artist's recordings tagged with `tag`.

    One request answers "which of their work is dubstep". What it cannot answer
    is which of it is any good — MusicBrainz orders by Lucene relevance, so
    demos and 5.1 remixes rank alongside the hits (Aphex Twin + techno leads
    with a SAW:II fragment). That is why this is a PREFERENCE SET applied over
    Spotify's relevance order, never an ordering in its own right.

    An empty ANSWER is cached like any other: "nobody tagged this artist's
    recordings dubstep" is a fact, not something to re-ask every run. A failed
    REQUEST is not cached — that is a missing answer, and caching it would
    freeze a transient 503 into a permanent "this artist has nothing".
    """
    key = f"{artist_mbid}::{tag}"
    if key in cache:
        return set(cache[key]["titles"])
    r = http.get(MB_RECORDING_URL, params={
        "query": f'arid:{artist_mbid} AND tag:"{tag}"',
        "fmt": "json", "limit": MB_RECORDING_LIMIT,
    })
    if r is None or r.status_code != 200:
        print(f"    ! MusicBrainz gave no answer for {tag} recordings "
              f"({'no response' if r is None else r.status_code}); "
              f"treating as unknown, not as empty")
        return set()
    titles = {k for k in (_title_key(rec.get("title", ""))
                          for rec in r.json().get("recordings", [])) if k}
    rec = {"key": key, "artist_mbid": artist_mbid, "tag": tag,
           "titles": sorted(titles)}
    append_jsonl(GENRE_RECORDINGS_CACHE, rec)
    cache[key] = rec
    return titles


def choose_tracks(tracks: list[dict], on_genre: set[str], k: int) -> list[dict]:
    """Prefer the artist's on-genre work; Spotify relevance does the rest.

    `tracks` arrives in Spotify's relevance order, which is the popularity
    proxy. The sort is STABLE and keyed only on the genre flag, so relevance
    survives inside each group: the artist's on-genre work rises above their
    bigger off-genre hit, but between two on-genre tracks the better-known one
    still leads. With no on-genre data the sort is a no-op and this degrades
    cleanly to plain relevance order.
    """
    flagged = [dict(t, genre_matched=_title_key(t.get("track_name", "")) in on_genre)
               for t in tracks]
    flagged.sort(key=lambda t: not t["genre_matched"])
    return flagged[:k]


# --------------------------------------------------------------------------
# Spotify — the ordering, the URIs, and later the shelf
# --------------------------------------------------------------------------


class Spotify:
    """Thin bearer-token client.

    429s are honoured — Spotify's Retry-After is real, unlike MusicBrainz's —
    but capped, so a bad header cannot park a run for hours. There is
    deliberately NO delete verb: this stage must never be able to remove a
    playlist, and the cheapest way to guarantee that is to not implement it.
    """

    def __init__(self, token: str):
        self.h = {"Authorization": f"Bearer {token}"}
        self._last = 0.0

    def _wait(self):
        gap = SP_MIN_INTERVAL - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()

    def _req(self, method: str, path: str, **kw):
        for attempt in (1, 2):
            self._wait()
            try:
                r = requests.request(method, f"{SP_API}{path}", headers=self.h,
                                     timeout=30, **kw)
            except requests.RequestException:
                return None
            if r.status_code == 429 and attempt == 1:
                try:
                    hinted = int(r.headers.get("Retry-After", "1") or 1)
                except ValueError:
                    hinted = 1
                time.sleep(min(hinted, 30))
                continue
            if r.status_code >= 400:
                return {"_status": r.status_code, "_body": r.text[:200]}
            return r.json() if r.text else {}
        return None

    def get(self, path: str, params: dict | None = None):
        return self._req("GET", path, params=params)

    def post(self, path: str, json: dict):
        return self._req("POST", path, json=json)

    def put(self, path: str, json: dict):
        return self._req("PUT", path, json=json)


def _artist_match(item: dict, artist: str) -> bool:
    """Accept a hit only when the artist we asked for is really credited on it.

    Searching `artist:"Virtual Riot"` is a relevance query, not a filter:
    karaoke acts, tribute covers and "in the style of" uploads all come back.
    Folding is enrich.normalise, the same one Stage 2 resolves names with, so
    'A$AP Rocky' and 'ASAP Rocky' are one artist. A featured credit counts —
    the artist is genuinely on the track.
    """
    want = normalise(artist)
    return want in {normalise(a.get("name", "")) for a in item.get("artists", [])}


def sp_artist_tracks(sp, artist: str, cache: dict) -> list[dict]:
    """This artist's tracks in Spotify's relevance order, validated.

    Relevance order is the whole point: with ListenBrainz popularity down it is
    the only popularity signal left, so the list comes back in exactly the
    order Spotify gave it and nothing here re-sorts it. `artist_name` is set to
    the name we asked for rather than the credit string on the result, so the
    per-artist cap downstream stays keyed on one spelling.

    An empty answer caches like any other: an artist Spotify does not carry is
    asked once, not once per run.
    """
    key = normalise(artist)
    if key in cache:
        return [dict(t, artist_name=artist) for t in cache[key]["tracks"]]
    resp = sp.get("/search", params={
        "q": f'artist:"{artist.replace(chr(34), "")}"',
        "type": "track", "limit": SP_SEARCH_LIMIT,
    })
    items = (resp.get("tracks", {}).get("items", [])
             if isinstance(resp, dict) and "_status" not in resp else [])
    tracks = [{"track_name": it.get("name"), "spotify_track_uri": it.get("uri")}
              for it in items
              if it.get("uri") and _artist_match(it, artist)]
    rec = {"key": key, "artist": artist, "tracks": tracks}
    append_jsonl(ARTIST_TRACKS_CACHE, rec)
    cache[key] = rec
    return [dict(t, artist_name=artist) for t in tracks]


# --------------------------------------------------------------------------
# Playlist lifecycle — ID first, exact name second, create third. Never delete.
# --------------------------------------------------------------------------


def load_state() -> dict:
    if not config.PLAYLIST_STATE_JSON.exists():
        return {}
    try:
        return json.loads(config.PLAYLIST_STATE_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(d: dict) -> None:
    config.PLAYLIST_STATE_JSON.write_text(
        json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")


def _alive(resp) -> bool:
    return bool(isinstance(resp, dict) and "_status" not in resp and resp.get("id"))


def ensure_playlist(sp, uid: str, tag: str, name: str, state: dict) -> str:
    """Resolve the playlist this stage owns for `tag`, creating if needed.

    Identity is the stored ID — immune to the user renaming things. The name
    match is an exact-template fallback for first runs and lost state; it is
    EXACT on purpose, case and all. A near-miss is somebody's hand-made
    playlist, and adopting it would mean this stage overwrites something a
    person built. Creating a duplicate is the cheap mistake; overwriting is
    the expensive one.
    """
    entry = state.get(tag) or {}
    if entry.get("id"):
        if _alive(sp.get(f"/playlists/{entry['id']}", params={"fields": "id,name"})):
            return entry["id"]

    page = sp.get("/me/playlists", params={"limit": 50})
    while isinstance(page, dict) and "_status" not in page:
        for item in page.get("items", []):
            if item.get("name") == name:
                return item["id"]
        nxt = page.get("next")
        if not nxt:
            break
        page = sp.get(nxt.removeprefix(SP_API), params=None)

    created = sp.post(f"/users/{uid}/playlists", json={
        "name": name, "public": False,
        "description": "created by spotify-trend-analysis",
    })
    if not _alive(created):
        raise SystemExit(
            f"Could not create playlist '{name}': {created}\n" + (
                QUOTA_NOTE if isinstance(created, dict)
                and created.get("_status") == 403 else ""))
    return created["id"]


def playlist_items(sp, pid: str) -> list[dict]:
    """Current contents, for the pre-replace snapshot. Nothing we overwrite
    goes unrecorded — hand-added tracks included."""
    out: list[dict] = []
    path = (f"/playlists/{pid}/tracks"
            "?fields=items(track(uri,name,artists(name))),next&limit=100")
    resp = sp.get(path, params=None)
    while isinstance(resp, dict) and "_status" not in resp:
        for it in resp.get("items", []):
            t = it.get("track") or {}
            out.append({"uri": t.get("uri"), "track_name": t.get("name"),
                        "artist_name": ", ".join(a.get("name", "")
                                                 for a in t.get("artists", []))})
        nxt = resp.get("next")
        if not nxt:
            break
        resp = sp.get(nxt.removeprefix(SP_API), params=None)
    return out


# --------------------------------------------------------------------------
# Archive — the playlist is a rendering; this Parquet is the record
# --------------------------------------------------------------------------


ARCHIVE_COLS = ["run_date", "kind", "gap_tag", "playlist_id", "position",
                "slot", "artist_name", "track_name", "spotify_track_uri", "source"]


def write_archive(con: duckdb.DuckDBPyConnection, rows: list[dict]) -> None:
    """Append this run to the archive, rewritten in total order.

    ORDER BY ALL for the same reason every other write in this project uses it:
    a partial sort key leaves ties for DuckDB's parallel sort to break however
    it likes, and byte-identical re-runs quietly stop holding.
    """
    if not rows:
        return
    con.execute(f"""CREATE OR REPLACE TABLE _new ({', '.join(
        c + (' INTEGER' if c == 'position' else ' VARCHAR') for c in ARCHIVE_COLS)})""")
    con.executemany(
        f"INSERT INTO _new VALUES ({', '.join('?' for _ in ARCHIVE_COLS)})",
        [[r.get(c) for c in ARCHIVE_COLS] for r in rows])
    if config.PLAYLISTS_PARQUET.exists():
        con.execute(f"""CREATE OR REPLACE TABLE _all AS
            SELECT * FROM '{config.PLAYLISTS_PARQUET}' UNION ALL SELECT * FROM _new""")
    else:
        con.execute("CREATE OR REPLACE TABLE _all AS SELECT * FROM _new")
    con.execute(f"COPY (SELECT * FROM _all ORDER BY ALL) TO "
                f"'{config.PLAYLISTS_PARQUET}' (FORMAT PARQUET, COMPRESSION ZSTD)")


# --------------------------------------------------------------------------


def register_sources(con: duckdb.DuckDBPyConnection) -> None:
    needed = {
        "genre_gaps": config.DATA_DIR / "genre_gaps.parquet",
        "plays": config.PLAYS_PARQUET,
        "artist_tags": config.ARTIST_TAGS_PARQUET,
        "recommendations": config.RECOMMENDATIONS_PARQUET,
    }
    for name, path in needed.items():
        if not path.exists():
            raise SystemExit(f"{path} not found — run the earlier stages first.")
        con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM '{path}'")


def build_selections(con, http, sp) -> list[dict]:
    """Everything up to (but excluding) the Spotify writes; shared by both
    modes so --dry-run previews exactly what a live run would do.

    Per candidate artist this spends one Spotify search (relevance order, the
    surviving popularity signal) and one MusicBrainz search (which of their
    recordings carry the gap genre). Both are cached append-only, so a re-run
    inside the same quarter spends nothing.
    """
    tag_cache = load_jsonl(config.CACHE_DIR / "candidate_tags.jsonl", "mbid")
    genre_rec_cache = load_jsonl(GENRE_RECORDINGS_CACHE, "key")
    artist_tracks_cache = load_jsonl(ARTIST_TRACKS_CACHE, "key")

    out = []
    for gap in select_gaps(con):
        tag = gap["tag"]
        print(f"\n{pretty(tag)} — {gap['n_artists']} artists, {gap['hours']:.0f} h")
        anchors = select_anchor_tracks(con, tag)
        print(f"  {len(anchors)} anchors from your own listening")

        seen_uris = {a["spotify_track_uri"] for a in anchors}
        candidates = select_candidates(con, tag, tag_cache)
        print(f"  {len(candidates)} candidate artists carry this genre")

        discovery: list[dict] = []
        for cand in candidates:
            if len(discovery) >= config.PLAYLIST_SIZE:   # enough material
                break
            tracks = sp_artist_tracks(sp, cand["artist_name"], artist_tracks_cache)
            if not tracks:
                continue
            on_genre = mb_genre_recordings(http, cand["mbid"], tag, genre_rec_cache)
            for chosen in choose_tracks(tracks, on_genre, config.TRACKS_PER_ARTIST):
                if chosen["spotify_track_uri"] in seen_uris:
                    continue
                seen_uris.add(chosen["spotify_track_uri"])
                discovery.append(chosen)

        matched = sum(1 for d in discovery if d.get("genre_matched"))
        print(f"  {len(discovery)} discovery tracks "
              f"({matched} matched on recording-level tags)")
        out.append({"gap": gap,
                    "tracks": assemble(anchors, discovery, config.PLAYLIST_SIZE)})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Stage 8 gap playlists")
    ap.add_argument("--dry-run", action="store_true",
                    help="select and resolve, print the result, write nothing")
    args = ap.parse_args()

    import os

    from dotenv import load_dotenv
    load_dotenv()
    from poll import access_token

    config.ensure_dirs()
    con = duckdb.connect()
    register_sources(con)

    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    if not client_id:
        raise SystemExit(
            "SPOTIFY_CLIENT_ID missing from .env — Stage 8 shares the Stage 6 "
            "Spotify app (redirect URI exactly http://127.0.0.1:3000).")
    sp = Spotify(access_token(client_id, SP_SCOPES))
    http = Throttled(MB_MIN_INTERVAL)

    selections = build_selections(con, http, sp)
    today = date.today().isoformat()

    if args.dry_run:
        report(selections, dry=True)
        return

    me = sp.get("/me", params=None)
    if not _alive(me):
        raise SystemExit(f"Could not read the current user: {me}")
    uid = me["id"]

    state = load_state()
    archive_rows = []
    for sel in selections:
        tag = sel["gap"]["tag"]
        name = config.PLAYLIST_NAME_TEMPLATE.format(genre=pretty(tag))
        if not sel["tracks"]:
            print(f"  ⚠ nothing selected for '{name}' — leaving it untouched")
            continue
        pid = ensure_playlist(sp, uid, tag, name, state)

        # Snapshot BEFORE the replace, so nothing we overwrite goes unrecorded.
        for i, old in enumerate(playlist_items(sp, pid)):
            archive_rows.append({
                "run_date": today, "kind": "pre_replace_snapshot", "gap_tag": tag,
                "playlist_id": pid, "position": i, "slot": None,
                "artist_name": old["artist_name"], "track_name": old["track_name"],
                "spotify_track_uri": old["uri"], "source": "spotify",
            })

        uris = [t["spotify_track_uri"] for t in sel["tracks"]]
        resp = sp.put(f"/playlists/{pid}/tracks", json={"uris": uris})
        if not isinstance(resp, dict) or "_status" in resp:
            print(f"  ⚠ replace failed for '{name}': {resp} — skipping")
            if isinstance(resp, dict) and resp.get("_status") == 403:
                raise SystemExit(QUOTA_NOTE)
            continue
        sp.put(f"/playlists/{pid}", json={
            "name": name,
            "description": config.PLAYLIST_DESCRIPTION_TEMPLATE.format(
                genre=pretty(tag), date=today),
        })
        state[tag] = {"id": pid, "name": name}
        sel["playlist_id"] = pid
        for t in sel["tracks"]:
            archive_rows.append({
                "run_date": today, "kind": "selection", "gap_tag": tag,
                "playlist_id": pid, "position": t["position"], "slot": t["slot"],
                "artist_name": t["artist_name"], "track_name": t["track_name"],
                "spotify_track_uri": t["spotify_track_uri"],
                "source": "plays" if t["slot"] == "anchor" else "spotify-search",
            })

    save_state(state)
    write_archive(con, archive_rows)
    report(selections, dry=False)


def report(selections: list[dict], dry: bool) -> None:
    print()
    print("=" * 74)
    print(f"STAGE 8 — GAP PLAYLISTS {'(dry run — nothing written)' if dry else ''}")
    print("=" * 74)
    for sel in selections:
        g = sel["gap"]
        n_anchor = sum(1 for t in sel["tracks"] if t["slot"] == "anchor")
        n_disc = len(sel["tracks"]) - n_anchor
        n_matched = sum(1 for t in sel["tracks"] if t.get("genre_matched"))
        print(f"\n{config.PLAYLIST_NAME_TEMPLATE.format(genre=pretty(g['tag']))}")
        print(f"  gap: {g['n_artists']} artists, {g['hours']:.0f} h, "
              f"{100 * g['rel_change_per_year']:+.0f}%/yr")
        print(f"  {len(sel['tracks'])} tracks — {n_anchor} anchors, {n_disc} "
              f"discovery ({n_matched} matched on recording-level tags)")
        if sel.get("playlist_id"):
            print(f"  playlist: {sel['playlist_id']}")
        for t in sel["tracks"][:8]:
            mark = "⚓" if t["slot"] == "anchor" else " "
            print(f"   {mark} {t['artist_name'][:28]:<28} {t['track_name'][:38]}")
        if len(sel["tracks"]) > 8:
            print(f"     ... {len(sel['tracks']) - 8} more")
    if not dry:
        print(f"\nArchive -> {config.PLAYLISTS_PARQUET}")
        print(f"State   -> {config.PLAYLIST_STATE_JSON}")
    print("=" * 74)


if __name__ == "__main__":
    main()
