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
