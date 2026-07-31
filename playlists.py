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
SP_SEARCH_LIMIT = 20        # one page of relevance; TRACKS_PER_ARTIST picks from it

# ListenBrainz popularity is server-side disabled — the by-artist endpoints 500
# and the batch route answers 200 with null listen counts for every recording.
# Genre truth comes from MusicBrainz recording search instead; see the Task 1
# probe record in docs/superpowers/plans/2026-07-31-stage8-gap-playlists.md.
MB_RECORDING_URL = "https://musicbrainz.org/ws/2/recording"
MB_RECORDING_LIMIT = 100    # one page is plenty; this is a filter, not a ranking

GENRE_RECORDINGS_CACHE = config.CACHE_DIR / "genre_recordings.jsonl"
ARTIST_TRACKS_CACHE = config.CACHE_DIR / "spotify_artist_tracks.jsonl"


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
