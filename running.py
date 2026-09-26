"""Stage 10 — two running playlists, built for sustained intensity.

    .venv/bin/python running.py --dry-run   # selections only, no Spotify writes
    .venv/bin/python running.py --write     # build/refresh both playlists

Stage 8 asks "where is this listening heading". This stage asks something
narrower and more practical: what keeps a run going. The library splits cleanly
into two high-intensity clusters that suit different runs, so it builds one of
each — steady 4x4 garage for distance, drop-driven bass for intervals.

THERE IS NO BPM HERE, AND THERE CANNOT BE.
    Spotify's /audio-features and /audio-analysis both answer 403 since the
    February 2026 rename; /tracks still answers 200, so this is a removal, not
    a permissions problem. Deezer's ISRC lookup matches this library perfectly
    (40/40) but carries a real BPM on 12% of it. And tempo would be the wrong
    signal even if it were free: Zomboy's "Nuclear (Hands Up)" measures 87 BPM
    because its drums are half-time, and it is exactly the sort of track that
    carries a run. What is modelled here is sustained intensity, not tempo.

WHY A SHARE AND NOT A TAG LIST.
    An include-list alone admits ILLENIUM — he carries dubstep(3) and
    trap edm(3) — while melodic dubstep(3) and future bass(2) are precisely why
    a workout playlist sags. Cluster membership is therefore
    cluster_weight / (cluster_weight + drag_weight), the same weighted-share
    shape Stage 9 uses to separate rap from electronic, and for the same reason:
    the naive rules both fail on real artists in this library.

WHAT MAKES A TRACK RANK.
    Tags are per ARTIST — this project has twice declined per-track MBID
    resolution and is not reopening it. So genre says which artists belong, and
    the listener's own behaviour ranks their tracks: hours in the recent window
    times the rate at which the track is played to the end. Luude's "Pachamama"
    has more plays than his Blair Muir remix and less than half the completion,
    and that difference is invisible to genre data.

    This is NOT an attempt to infer which plays happened during a run. The
    export records no activity type and CLAUDE.md rules that inference out;
    trackdone rate only says whether a track gets skipped.

Stage 1b's remixer credits are what make this work at all. Spotify bills a
remix to the ORIGINAL artist, so an Ian Asher speed-garage rework of a Halsey
song reads as pop and would be filtered out as drag. Credits are joined here
across album artist, feature AND remixer for exactly that reason.

DISCOVERY IS JUDGED PER TRACK.
    A stranger clearing the share bar says the ARTIST belongs, not that a given
    record by them does. Spotify's relevance page for MJ Cole led with Tion
    Wayne's rap single, and every hit used to be relabelled as the candidate,
    so nothing could tell. Each pick is now read against Spotify's own credit
    list first — gate_discovery has the rules and the cases behind each.

Outputs: two playlists, data/running_state.json, rows in data/playlists.parquet
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import unicodedata
from datetime import date

import duckdb

import config
from consolidate import gentle_token
from credits import REMIX_CREDIT_RE, REMIX_FORMAT_STOPLIST, REMIX_NON_NAME_RE
from enrich import MB_MIN_INTERVAL, Throttled, load_genre_vocabulary, normalise
from playlists import (
    FORBIDDEN_NOTE,
    GENRE_RECORDINGS_CACHE,
    SP_API,
    SP_SEARCH_LIMIT,
    Spotify,
    _artist_match,
    _title_key,
    choose_tracks,
    ensure_playlist,
    mb_genre_recordings,
    playlist_items,
    write_archive,
)
from recommend import (
    SIMILAR_CACHE,
    append_jsonl,
    fetch_candidate_tags,
    fetch_similar,
    load_jsonl,
)
from report import pretty

SCOPES_READ = "playlist-read-private playlist-read-collaborative"
SCOPES_WRITE = SCOPES_READ + " playlist-modify-private playlist-modify-public"

# Durations for tracks that are NOT in the listening history. Known tracks get
# theirs from plays for free (a completed play's ms_played IS the duration);
# only discovery tracks cost a request, and batch /tracks answers 403 so they
# cost one each.
DURATION_CACHE = config.CACHE_DIR / "track_durations.jsonl"

# Discovery searches, with WHO IS ON each track. Stage 8's
# spotify_artist_tracks.jsonl keeps a name and a URI per hit, and every hit was
# then labelled as the candidate — so Tion Wayne's rap single went out as MJ
# Cole garage, and Skrillex appeared seven times in the dubstep run under other
# artists' names. This keeps Spotify's own credit list (name AND id) and the
# duration search already carries. A new file, not a rewrite: the old one is
# Stage 8's, and an append-only cache is never rebuilt.
SP_TRACKS_CREDITED_CACHE = config.CACHE_DIR / "spotify_artist_tracks_credited.jsonl"

# The two clusters. `key` names the weight column; `label` names the playlist.
CLUSTERS = (
    {"key": "garage", "label": "speed garage", "tags": config.RUN_GARAGE_TAGS},
    {"key": "bass", "label": "dubstep", "tags": config.RUN_BASS_TAGS},
)


# --------------------------------------------------------------------------
# Song identity — a SONG and a VERSION are different keys
# --------------------------------------------------------------------------

# What a version key throws away once pressing notes are gone: ASCII
# punctuation and space, plus the typographic marks Spotify titles carry.
# Spelled as code points both engines read alike; \W would not do, since
# Python's is Unicode-aware and RE2's is ASCII-only. Non-ASCII letters stay,
# so a title in another script does not fold to "".
_VERSION_FOLD_RE = r"[\x00-\x2f\x3a-\x40\x5b-\x60\x7b-\x7f\xa0‘’“”–—…]+"


def version_key(title: str) -> str:
    """The RECORDING a title names: pressing notes dropped, remixes kept.

    config.RUN_PRESSING_NOTE_RE says what a pressing note is. The song key,
    playlists._title_key, folds harder and drops every suffix; the two
    together let a playlist hold one version of a song while the other
    playlist holds a different one. version_key_sql is the same key in DuckDB,
    and a test holds the two to the same answers.
    """
    t = title or ""
    head = re.sub(config.RUN_PRESSING_NOTE_RE, "", t)
    # A title that is only a note ("(Remastered)") keeps its full form rather
    # than folding to "" and matching every other such title.
    return (re.sub(_VERSION_FOLD_RE, "", head.lower())
            or re.sub(_VERSION_FOLD_RE, "", t.lower()))


def version_key_sql(col: str) -> str:
    """version_key as a DuckDB expression over the column `col`."""
    note = config.RUN_PRESSING_NOTE_RE.replace("'", "''")
    fold = _VERSION_FOLD_RE.replace("'", "''")
    return (f"coalesce(nullif(regexp_replace(lower(regexp_replace("
            f"{col}, '{note}', '', 'g')), '{fold}', '', 'g'), ''), "
            f"regexp_replace(lower({col}), '{fold}', '', 'g'))")


def register_song_key(con: duckdb.DuckDBPyConnection) -> None:
    """song_key(title) in SQL — playlists._title_key itself, not a copy.

    Its normalise() folds accents through NFKD and drops "the" and "and",
    which DuckDB cannot reproduce, and a second definition is exactly how two
    keys for one idea drift apart.
    """
    if not con.execute("SELECT count(*) FROM duckdb_functions() "
                       "WHERE function_name = 'song_key'").fetchone()[0]:
        con.create_function("song_key", _title_key, ["VARCHAR"], "VARCHAR")


class Placements:
    """What is already placed, in this playlist or the other one.

    Three keys, because Spotify presses one recording as several URIs and one
    song as several recordings:
    - the URI;
    - (artist, version_key), across BOTH playlists. The album cut, the single
      and the remaster are one record, and the first dry run duly listed
      jigitz's 'tell you straight' twice in one playlist;
    - (artist, song_key), within the CURRENT playlist only. One version of a
      song per playlist — but an original placed in garage no longer keeps its
      own remix out of dubstep, which the folded title alone used to do.
    """

    def __init__(self) -> None:
        self.uris: set[str] = set()
        self.versions: set[tuple] = set()
        self.songs: set[tuple] = set()

    def new_playlist(self) -> None:
        self.songs = set()

    @staticmethod
    def _keys(row: dict) -> tuple[tuple, tuple]:
        artist = normalise(row.get("artist_name") or "")
        title = row.get("track_name") or ""
        return (artist, version_key(title)), (artist, _title_key(title))

    def fresh(self, row: dict) -> bool:
        version, song = self._keys(row)
        return (row.get("spotify_track_uri") not in self.uris
                and version not in self.versions and song not in self.songs)

    def place(self, row: dict) -> dict:
        version, song = self._keys(row)
        self.uris.add(row.get("spotify_track_uri"))
        self.versions.add(version)
        self.songs.add(song)
        return row

    def release(self, row: dict) -> None:
        """Un-claim a row the duration fill turned away, so the other
        playlist can still be offered it."""
        version, song = self._keys(row)
        self.uris.discard(row.get("spotify_track_uri"))
        self.versions.discard(version)
        self.songs.discard(song)


# --------------------------------------------------------------------------
# Classification — which artists belong to which cluster
# --------------------------------------------------------------------------


def build_artist_clusters(con: duckdb.DuckDBPyConnection) -> None:
    """Per-artist weights over the two clusters and the drag family.

    Weights are MusicBrainz vote counts, clamped at 0 upstream by Stage 2. A
    zero-count tag therefore contributes nothing, which is the same guard
    MIN_TAG_COUNT_FOR_ANCHOR gives Stage 8: REAPER carries `heavy metal` at 0
    because the community downvoted it, and nothing should be classified on the
    strength of a tag nobody stands behind.

    Both shares are computed against the SAME drag weight, so an artist can
    qualify for both clusters; the heavier weight then wins. Knock2 carries
    bass house(4) and hybrid trap(2) and belongs in the garage list on that
    basis rather than by coin toss.
    """
    def in_list(items) -> str:
        return "(" + ", ".join("?" for _ in items) + ")"

    # Parameter order must follow the order the placeholders appear in the SQL.
    params = (list(config.RUN_TAG_BLOCKLIST) + list(config.RUN_GARAGE_TAGS)
              + list(config.RUN_BASS_TAGS) + list(config.RUN_DRAG_TAGS))

    con.execute(
        f"""
        CREATE OR REPLACE TABLE artist_clusters AS
        WITH scoped AS (
            -- `garage rock` is not UK garage and `hardcore punk` is not happy
            -- hardcore. Same job as CONSOLIDATE_EDM_BLOCKLIST.
            SELECT artist_name, tag, greatest(tag_count, 0) AS w
            FROM artist_tags
            WHERE is_genre
              AND tag NOT IN {in_list(config.RUN_TAG_BLOCKLIST)}
        ),
        agg AS (
            SELECT
                artist_name,
                sum(CASE WHEN tag IN {in_list(config.RUN_GARAGE_TAGS)}
                         THEN w ELSE 0 END) AS garage_w,
                sum(CASE WHEN tag IN {in_list(config.RUN_BASS_TAGS)}
                         THEN w ELSE 0 END) AS bass_w,
                sum(CASE WHEN tag IN {in_list(config.RUN_DRAG_TAGS)}
                         THEN w ELSE 0 END) AS drag_w
            FROM scoped
            GROUP BY 1
        ),
        shared AS (
            SELECT *,
                   garage_w / nullif(garage_w + drag_w, 0) AS garage_share,
                   bass_w   / nullif(bass_w   + drag_w, 0) AS bass_share
            FROM agg
        )
        SELECT *,
            CASE
                WHEN garage_share >= {config.RUN_MIN_INTENSITY_SHARE}
                 AND bass_share   >= {config.RUN_MIN_INTENSITY_SHARE}
                    THEN CASE WHEN garage_w >= bass_w
                              THEN 'speed garage' ELSE 'dubstep' END
                WHEN garage_share >= {config.RUN_MIN_INTENSITY_SHARE}
                    THEN 'speed garage'
                WHEN bass_share   >= {config.RUN_MIN_INTENSITY_SHARE}
                    THEN 'dubstep'
                ELSE NULL
            END AS cluster
        FROM shared
        """,
        params,
    )


def classify_tags(tags: list[dict], min_weight: int = 0,
                  min_share: float = config.RUN_MIN_INTENSITY_SHARE,
                  ) -> tuple[str | None, float]:
    """The same weighted-share test as build_artist_clusters, for tag vectors
    that are not in artist_tags — i.e. discovery candidates, whose genres live
    in the candidate_tags cache.

    A discovery artist must clear at least the bar a library artist clears.
    Without this, a candidate sharing ONE tag with the cluster is admitted, and
    a dry run duly offered Röyksopp, Basement Jaxx and 90 seconds of Aphex Twin
    ambient as running music.

    `min_share` defaults to the library line so the SQL and this agree; callers
    judging a stranger pass config.RUN_MIN_CANDIDATE_SHARE. At 0.60 Netsky
    passed on drum and bass 10 against liquid funk 3 (0.77) — fine for an
    artist the listener already plays, not for one nobody has vouched for.
    """
    broad = {b.casefold() for b in config.RUN_BROAD_TAGS}
    garage = bass = drag = 0
    narrow_garage = narrow_bass = False
    for t in tags:
        tag = (t.get("tag") or "").casefold()
        if tag in {b.casefold() for b in config.RUN_TAG_BLOCKLIST}:
            continue
        w = max(int(t.get("count") or 0), 0)
        if tag in {g.casefold() for g in config.RUN_GARAGE_TAGS}:
            garage += w
            narrow_garage = narrow_garage or (tag not in broad and w > 0)
        elif tag in {b.casefold() for b in config.RUN_BASS_TAGS}:
            bass += w
            narrow_bass = narrow_bass or (tag not in broad and w > 0)
        elif tag in {d.casefold() for d in config.RUN_DRAG_TAGS}:
            drag += w

    g_share = garage / (garage + drag) if (garage + drag) else 0.0
    b_share = bass / (bass + drag) if (bass + drag) else 0.0
    # A ratio is meaningless on a sparse vector: one cluster tag at count 1 and
    # no drag tags gives 1/1. `min_weight` is the absolute floor beneath it —
    # waived when a NARROW tag carries the artist, since `speed garage(1)` says
    # far more than `tech house(1)`.
    if garage < min_weight and not narrow_garage:
        garage, g_share = 0, 0.0
    if bass < min_weight and not narrow_bass:
        bass, b_share = 0, 0.0
    thr = min_share
    if g_share >= thr and b_share >= thr:
        return ("speed garage", g_share) if garage >= bass else ("dubstep", b_share)
    if g_share >= thr:
        return "speed garage", g_share
    if b_share >= thr:
        return "dubstep", b_share
    return None, max(g_share, b_share)


def build_known_pool(con: duckdb.DuckDBPyConnection) -> None:
    """Versions from the listener's own recent history, per cluster.

    The join is on track_credits, NOT on plays.artist_name, so a track counts
    for the artists actually on it. That is the whole reason Stage 1b learned
    to parse `- X Remix`: without it a Halsey track remixed by Ian Asher is
    judged on Halsey's tags.

    ONE ROW PER VERSION, not per URI. Spotify presses the album cut, the single
    and the remaster as distinct URIs, and per URI "tell you straight" was two
    rows on 70 and 44 plays, each judged on part of its evidence. Pressings are
    grouped on (album artist, version_key): plays, hours and completions are
    summed, credits are the union over every pressing (the poller may have
    seen one and not another), and the row carries the most-played URI, which
    is what the playlist will play. `uris` keeps the rest. A remix is its own
    version, so it is judged on its own plays.

    POLLED PLAYS COUNT AS TIME, NOT AS EVIDENCE. A polled row carries an
    estimated ms_played and a NULL reason_end, so read as completion every one
    is a skip — the 09-25 re-run pulled Rain off its 0.846 and cut "Tough -
    Gravagerz Remix" outright. Completion, the play count behind the Laplace
    term and the skip floor come from export rows only; hours count all, since
    the listening did happen. A track only the poller has heard sits at the
    0.5 prior rather than vanishing.

    THE WINDOW ENDS AT THE EXPORT HORIZON (config.ANALYSIS_HORIZON_SQL), not at
    the latest play. Anchored on the poller, 36 months slid forward past the
    export's coverage and lost 1,801 plays (82.6 h) off the far end. There is no
    upper bound, so polled plays past the horizon are still counted.

    Duration is the representative pressing's MEDIAN completed export play;
    failing that, a polled ms_played, which the poller records as the whole
    track's length; failing that, the median over its other pressings.

    WHICH CREDITS MAY PLACE A TRACK. The album artist, a remixer, or a feature
    the poller saw. A feature from the export is Stage 1b's title regex — a
    guess — and never admits on its own: Todd Edwards' 0.54 h in this window is
    two Daft Punk edits he is guessed onto, and that guess is how he reached
    the garage seeds at all. A blanket ban on features would be wrong the other
    way, eroding every remixer keep as polling grows, because credits.py types
    every non-first poller artist as `featured`, remixers included.

    A remix belongs to its REMIXER's run. Habstrakt's bass house put "The One -
    NGHTMRE Remix" in garage; the record is NGHTMRE's. So when the title's
    remix credit names a credited cluster artist, the track goes to that
    artist's cluster only. It is read from the title, not from credit_type, for
    the poller reason above.

    A featured credit with no cluster (Inéz, hand-tagged house and melodic
    dubstep) neither admits nor refuses. There is no drag test on the known
    side: a hand tag on a singer must never move a track the listener plays.

    ONE VERSION PER SONG PER CLUSTER. Where an album artist's original and its
    remix both qualify, the one listened through more often takes the slot, as
    long as it has RUN_MIN_VERSION_PLAYS plays behind it: "Drugs I Like (AVELLO
    Remix)" over the original it outlasts, but not a remix finished twice.

    The per-artist cap is keyed on the ALBUM artist. A remixer who also has
    their own releases is capped on those separately, which is why Blair Muir
    can bring REHAB and Disturbia while his Luude remix counts against Luude.
    """
    register_song_key(con)
    con.execute(
        f"""
        CREATE OR REPLACE TABLE known_pool AS
        WITH per_uri AS (
            SELECT
                p.spotify_track_uri,
                any_value(p.track_name)  AS track_name,
                any_value(p.artist_name) AS album_artist,
                -- Completion evidence: export plays only.
                count(*) FILTER (WHERE NOT p.ms_played_estimated) AS n_plays,
                count(*)                                          AS n_plays_all,
                count(*) FILTER (WHERE NOT p.ms_played_estimated
                                   AND p.reason_end = 'trackdone') AS n_done,
                -- Polled estimates count as time.
                sum(p.played_seconds) / 3600.0 AS hours,
                -- MEDIAN, not max. A completed play's ms_played is the track's
                -- duration, but the odd play reports far more than the track
                -- runs (a paused stream that kept counting). max() took
                -- SLANDER's "Wish I Could Forget" to 9.6 minutes and let one
                -- track eat a tenth of the playlist.
                coalesce(
                    median(CASE WHEN p.reason_end = 'trackdone'
                                THEN p.ms_played END)
                        FILTER (WHERE NOT p.ms_played_estimated),
                    median(p.ms_played) FILTER (WHERE p.ms_played_estimated)
                ) AS duration_ms
            FROM plays p
            WHERE p.spotify_track_uri IS NOT NULL
              AND p.track_name IS NOT NULL
              AND p.month >= {config.ANALYSIS_HORIZON_SQL}
                             - INTERVAL {config.RUN_WINDOW_MONTHS} MONTH
            GROUP BY 1
        ),
        members AS (
            -- Every pressing points at its version's representative: the
            -- most-played URI, which is the one the playlist carries.
            SELECT *,
                   first_value(spotify_track_uri) OVER (
                       PARTITION BY coalesce(lower(album_artist), ''),
                                    {version_key_sql('track_name')}
                       ORDER BY n_plays_all DESC, hours DESC, spotify_track_uri
                   ) AS rep_uri
            FROM per_uri
        ),
        grouped AS (
            SELECT rep_uri AS spotify_track_uri,
                   list(spotify_track_uri ORDER BY spotify_track_uri) AS uris,
                   sum(n_plays)::BIGINT     AS n_plays,
                   sum(n_plays_all)::BIGINT AS n_plays_all,
                   sum(n_done)::BIGINT      AS n_done,
                   sum(hours)               AS hours,
                   median(duration_ms)      AS any_duration_ms
            FROM members
            GROUP BY 1
        ),
        recent AS (
            SELECT g.spotify_track_uri, p.track_name, p.album_artist, g.uris,
                   g.n_plays, g.n_plays_all, g.hours,
                   coalesce(g.n_done / nullif(g.n_plays, 0), 0) AS done_rate,
                   coalesce(p.duration_ms, g.any_duration_ms)   AS duration_ms
            FROM grouped g
            JOIN per_uri p USING (spotify_track_uri)
        ),
        admitting AS (
            -- The credits allowed to place a version in a cluster, over every
            -- pressing's credits.
            SELECT DISTINCT m.rep_uri AS spotify_track_uri, c.artist_name,
                   ac.cluster, ac.garage_w + ac.bass_w AS w
            FROM members m
            JOIN track_credits c ON c.spotify_track_uri = m.spotify_track_uri
            JOIN artist_clusters ac ON ac.artist_name = c.artist_name
            WHERE ac.cluster IS NOT NULL
              AND (c.credit_type IN ('album_artist', 'remixer')
                   OR (c.credit_type = 'featured' AND c.credit_source = 'poller'))
        ),
        routed AS (
            -- The remixer a pressing's TITLE names, when they are an admitting
            -- cluster artist on the record. regexp_extract gives '' on no
            -- match, and no credited name is that short.
            SELECT a.spotify_track_uri,
                   first(a.cluster ORDER BY a.w DESC, a.cluster) AS remix_cluster
            FROM admitting a
            JOIN members m ON m.rep_uri = a.spotify_track_uri
            WHERE lower(trim(a.artist_name)) = lower(trim(
                      regexp_extract(m.track_name, '{REMIX_CREDIT_RE}', 1, 'i')))
            GROUP BY 1
        ),
        credited AS (
            SELECT r.*, a.cluster,
                   -- One row per (version, cluster): a track crediting three
                   -- qualifying artists is one track, not three.
                   row_number() OVER (
                       PARTITION BY r.spotify_track_uri, a.cluster
                       ORDER BY a.w DESC, a.artist_name
                   ) AS rn
            FROM recent r
            JOIN admitting a USING (spotify_track_uri)
            LEFT JOIN routed x USING (spotify_track_uri)
            WHERE x.remix_cluster IS NULL OR a.cluster = x.remix_cluster
        ),
        scored AS (
            SELECT
                spotify_track_uri, track_name, album_artist, cluster, uris,
                n_plays, n_plays_all, hours, done_rate, duration_ms,
                -- Laplace-smoothed completion. A track played once and
                -- finished is not evidence of the same strength as one
                -- finished forty times, and (done+1)/(n+2) says so without
                -- discarding the single play. n is the EXPORT count, so a
                -- polled-only track sits at 0.5.
                (done_rate * n_plays + 1) / (n_plays + 2) AS done_smoothed,
                hours * ((done_rate * n_plays + 1) / (n_plays + 2)) AS score
            FROM credited
            WHERE rn = 1
              -- A track skipped repeatedly is not a track that carries a run.
              -- Applied only where there is enough evidence to mean anything,
              -- and polled plays are not evidence.
              AND NOT (n_plays >= 3
                       AND done_rate < {config.RUN_MIN_TRACKDONE_RATE})
        )
        SELECT * FROM scored
        -- One version per song per cluster, chosen after the skip floor so a
        -- floored original leaves the slot to its remix.
        QUALIFY row_number() OVER (
            PARTITION BY cluster, coalesce(lower(album_artist), ''),
                         song_key(track_name)
            ORDER BY n_plays >= {config.RUN_MIN_VERSION_PLAYS} DESC,
                     done_smoothed DESC, score DESC, spotify_track_uri
        ) = 1
        ORDER BY ALL
        """
    )


def select_known(con: duckdb.DuckDBPyConnection, label: str,
                 limit: int) -> list[dict]:
    """Top-scoring known tracks for one cluster, capped per album artist."""
    cols = ["spotify_track_uri", "track_name", "artist_name", "duration_ms",
            "hours", "done_rate", "n_plays", "score"]
    rows = con.execute(
        f"""
        SELECT spotify_track_uri, track_name, album_artist AS artist_name,
               duration_ms, hours, done_rate, n_plays, score
        FROM known_pool
        WHERE cluster = ?
        QUALIFY row_number() OVER (
            PARTITION BY album_artist ORDER BY score DESC, spotify_track_uri
        ) <= {config.RUN_TRACKS_PER_ARTIST}
        ORDER BY score DESC, spotify_track_uri
        LIMIT {limit}
        """,
        [label],
    ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


# --------------------------------------------------------------------------
# Overrides — a hand answer outranks a computed score
# --------------------------------------------------------------------------


def cluster_seed_artists(con: duckdb.DuckDBPyConnection, label: str,
                         vetoes: set[tuple]) -> list[dict]:
    """This cluster's own artists, ranked by what their tracks earn in the known
    pool — ONE row per MusicBrainz artist.

    Joined to a one-row-per-name view of artist_tags, never to artist_tags
    itself: that table has a row per TAG, and the old join counted an artist
    once per tag. Todd Edwards carries eighteen, which lifted him from rank 31
    to garage seed #17, and his ListenBrainz tail then supplied 13 of the 21
    garage discovery tracks. Grouped on the MBID, so Skrillex and SKRILLEX
    pool into one seed rather than counting him twice.

    An artist-wide veto drops a seed under ANY name sharing its MBID. SLANDER
    was dubstep seed #6 after being vetoed off the playlist itself; seeding on a
    rejected artist asks ListenBrainz for more of exactly what was rejected.

    Task 8's library-artist discovery reads this list too, so the ranking is
    total — ties broken on mbid — and unlimited; callers take what they need.
    """
    rows = con.execute(
        f"""
        WITH a AS (
            SELECT artist_name, any_value(mbid) AS mbid
            FROM artist_tags WHERE mbid IS NOT NULL
            GROUP BY 1
        ),
        per_name AS (
            SELECT c.artist_name, a.mbid,
                   sum(k.score) AS score, sum(k.hours) AS hours
            FROM known_pool k
            JOIN track_credits c USING (spotify_track_uri)
            JOIN a ON a.artist_name = c.artist_name
            JOIN artist_clusters ac ON ac.artist_name = c.artist_name
            WHERE k.cluster = ? AND ac.cluster = ?
            GROUP BY 1, 2
        ),
        seeds AS (
            SELECT mbid,
                   -- The spelling carrying more of the score names the seed;
                   -- a tie falls to the name, so the choice is repeatable.
                   first(artist_name ORDER BY score DESC, artist_name)
                       AS artist_name,
                   sum(score) AS score, sum(hours) AS hours
            FROM per_name
            GROUP BY mbid
            HAVING sum(hours) >= {config.RUN_MIN_SEED_HOURS}
        )
        SELECT s.mbid, s.artist_name, s.score, s.hours,
               (SELECT list(a.artist_name ORDER BY a.artist_name)
                FROM a WHERE a.mbid = s.mbid) AS names
        FROM seeds s
        ORDER BY s.score DESC, s.mbid
        """,
        [label, label],
    ).fetchall()
    out = []
    for mbid, name, score, hours, names in rows:
        if any(vetoed({"artist_name": n, "track_name": ""}, vetoes)
               for n in names or [name]):
            continue
        out.append({"mbid": mbid, "artist_name": name,
                    "score": float(score), "hours": float(hours)})
    return out


def cluster_candidates(con, http, label: str, tag_cache: dict,
                       sim_cache: dict, vocab: set[str],
                       vetoes: set[tuple]) -> list[dict]:
    """Discovery candidates seeded on THIS cluster's own top artists.

    Stage 5's recommendations.parquet is seeded across the whole library, so its
    electronic candidates are whatever sits near a taste that also contains
    hip-hop and rock — canonical rather than current. It offered Basement Jaxx,
    Busy P and Mr. Oizo as speed garage, and The Prodigy as dubstep. Asking
    ListenBrainz "who is like Blair Muir, like NOTION, like Ian Asher" returns
    the right neighbourhood instead.

    Similarity is normalised PER SEED before it is pooled. ListenBrainz scores
    are not on a common scale: Skrillex's list tops out at 3955 and REAPER's at
    181, so a raw sum handed the pool to whichever seed is a hub. All twelve
    dubstep strangers carried a Skrillex contribution while Eptic, Sullivan King
    and Space Laces went unused. Each neighbour now earns
    (seed score / total used seed score) x (similarity / that seed's best), so
    a bigger artist of yours still counts for more, and a hub does not.

    Both lookups are the cached, append-only kind the rest of the project uses,
    so a re-run inside the same quarter spends nothing.
    """
    used: list[tuple[dict, list[dict]]] = []
    for seed in cluster_seed_artists(con, label, vetoes):
        if len(used) >= config.RUN_DISCOVERY_SEEDS:
            break
        similar = fetch_similar(http, seed["mbid"], sim_cache)
        # ListenBrainz knowing nothing about an artist is no reason to seed on
        # one fewer: skip it and let the next artist down take the slot.
        if similar:
            used.append((seed, similar))
    if not used:
        return []
    print(f"  seeding discovery on {len(used)} of your own artists: "
          f"{', '.join(s['artist_name'] for s, _ in used[:5])}...")

    known_names = {normalise(r[0]) for r in con.execute(
        "SELECT DISTINCT artist_name FROM plays WHERE artist_name IS NOT NULL"
    ).fetchall()}

    total = sum(s["score"] for s, _ in used) or 1.0
    pooled: dict[str, dict] = {}
    for seed, similar in used:
        best = max(float(x.get("score") or 0) for x in similar) or 1.0
        weight = seed["score"] / total
        for sim in similar:
            if normalise(sim["name"]) in known_names or not sim["mbid"]:
                continue
            row = pooled.setdefault(sim["mbid"], {
                "artist_name": sim["name"], "mbid": sim["mbid"], "score": 0.0})
            # Similar to several of your cluster artists beats similar to one.
            row["score"] += weight * float(sim.get("score") or 0) / best

    # mbid breaks ties, so the tagging cutoff below never depends on the order
    # the seeds happened to be walked in.
    ranked = sorted(pooled.values(), key=lambda r: (-r["score"], r["mbid"]))
    out = []
    for cand in ranked[:config.RUN_MAX_CANDIDATES_TO_TAG]:
        tags = fetch_candidate_tags(http, cand["mbid"], vocab, tag_cache)
        cl, share = classify_tags(
            tags, min_weight=config.RUN_MIN_CANDIDATE_CLUSTER_WEIGHT,
            min_share=config.RUN_MIN_CANDIDATE_SHARE)
        if cl == label:
            out.append(dict(cand, share=share))
    return out


def load_overrides() -> tuple[list[dict], set[tuple]]:
    """(pins, vetoes) from running_overrides.csv.

    Pins carry the FULL track title, not the folded one. `_title_key` drops
    everything from the first ' - ', and this library contains both Insania's
    'iloveitiloveitiloveit - Garage' (23 plays) and Bella Kay's
    'iloveitiloveitiloveit' (4 plays). Folding the pin would let the wrong one
    take the slot.

    A blank `playlist` applies to both.
    """
    pins: list[dict] = []
    vetoes: set[tuple] = set()
    if not config.RUNNING_OVERRIDES_CSV.exists():
        return pins, vetoes

    with config.RUNNING_OVERRIDES_CSV.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            decision = (row.get("decision") or "").strip().lower()
            artist = (row.get("artist_name") or "").strip()
            if not artist or artist.startswith("#") or decision not in {"keep", "drop"}:
                continue
            entry = {
                "playlist": (row.get("playlist") or "").strip(),
                "artist_name": artist,
                "track_name": (row.get("track_name") or "").strip(),
                "note": (row.get("note") or "").strip(),
            }
            if decision == "keep":
                pins.append(entry)
            else:
                vetoes.add((normalise(artist), normalise(entry["track_name"])))
    return pins, vetoes


def is_live(track_name: str) -> bool:
    """A live recording, which is refused however on-genre it is.

    Crowd noise, a spoken intro and whatever tempo the drummer picked on the
    night all break a run, and none of that is visible to a genre tag: the
    track is correctly classified and still wrong.

    Matched structurally rather than as a substring. A bare /live/ would take
    Zeds Dead's "Alive" and Dustycloud's "Alive" — both in these playlists,
    both wanted.
    """
    return bool(re.search(config.RUN_LIVE_TITLE_RE, track_name or ""))


def vetoed(row: dict, vetoes: set[tuple]) -> bool:
    """A veto naming only an artist removes everything by them."""
    a = normalise(row.get("artist_name", ""))
    t = normalise(row.get("track_name", ""))
    return (a, t) in vetoes or (a, "") in vetoes


def resolve_pins(con: duckdb.DuckDBPyConnection, pins: list[dict],
                 label: str) -> list[dict]:
    """Turn pinned (artist, track) pairs into real URIs from the history.

    Matched on the full title against plays, so the pin means the pressing the
    listener actually played rather than whichever one Spotify search happens
    to rank first.
    """
    out = []
    for pin in pins:
        if pin["playlist"] and pin["playlist"] != label:
            continue
        rows = con.execute(
            """
            SELECT spotify_track_uri, any_value(track_name), any_value(artist_name),
                   max(CASE WHEN reason_end = 'trackdone' THEN ms_played END),
                   sum(played_seconds) / 3600.0 AS hours
            FROM plays
            WHERE lower(artist_name) = lower(?)
              AND lower(track_name)  = lower(?)
              AND spotify_track_uri IS NOT NULL
            GROUP BY spotify_track_uri
            ORDER BY hours DESC
            LIMIT 1
            """,
            [pin["artist_name"], pin["track_name"]],
        ).fetchall()
        if not rows:
            print(f"  ! pinned track not found in your history: "
                  f"{pin['artist_name']} — {pin['track_name']!r}")
            continue
        uri, tname, aname, dur, hours = rows[0]
        out.append({"spotify_track_uri": uri, "track_name": tname,
                    "artist_name": aname, "duration_ms": dur, "hours": hours,
                    "done_rate": 1.0, "n_plays": 0, "score": float("inf"),
                    "pinned": True})
    return out


# --------------------------------------------------------------------------
# Discovery tracks — judged per TRACK, not per artist
# --------------------------------------------------------------------------


def sp_artist_tracks_credited(sp, artist: str, cache: dict) -> list[dict]:
    """This artist's tracks in Spotify's relevance order, with who is on each.

    The same /search Stage 8 makes — one page of SP_SEARCH_LIMIT, hits kept only
    where the artist is really credited — keeping what Stage 8 throws away:
    every credited artist as {name, id}, in Spotify's order, and `duration_ms`,
    which search returns for free. So the length cap and the time budget cost
    no /tracks request unless a hit arrives without one.

    Only an ANSWER is cached. A 200 with no usable hit is a fact ("Spotify does
    not carry them") and is asked once. A 429, an error envelope or no response
    at all is a missing answer, and caching it would freeze a transient failure
    into "this artist has no tracks" for good.

    Keyed on normalise(artist), like Stage 8's, so 'DEM2' and 'Dem 2' share a
    key. The record holds every credited id, and pin_artist_id separates the
    two on read; a namesake is a read-time filter, not a cache repair.
    """
    key = normalise(artist)
    if key in cache:
        return [dict(t, artist_name=artist) for t in cache[key]["tracks"]]
    resp = sp.get("/search", params={
        "q": f'artist:"{artist.replace(chr(34), "")}"',
        "type": "track", "limit": SP_SEARCH_LIMIT,
    })
    if not isinstance(resp, dict) or "_status" in resp:
        return []
    tracks = [
        {"track_name": it.get("name"), "spotify_track_uri": it.get("uri"),
         "duration_ms": it.get("duration_ms"),
         # A LIST of credits, never a joined string: every printable separator
         # eventually collides with a real name ("Tyler, The Creator").
         "artists": [{"name": a.get("name"), "id": a.get("id")}
                     for a in it.get("artists", [])]}
        for it in (resp.get("tracks") or {}).get("items", [])
        if it.get("uri") and _artist_match(it, artist)
    ]
    rec = {"key": key, "artist": artist, "status": 200, "tracks": tracks}
    append_jsonl(SP_TRACKS_CREDITED_CACHE, rec)
    cache[key] = rec
    return [dict(t, artist_name=artist) for t in tracks]


def pin_artist_id(tracks: list[dict],
                  artist: str) -> tuple[str | None, list[dict]]:
    """The candidate's own Spotify artist id, and only the tracks crediting it.

    normalise() is a comparison key, not an identity. 'DEM2' and 'Dem 2' both
    fold to `dem2`, and the Dem 2 search duly returned DEM2's "Discoteca" — a
    different act — which went out as Dem 2 discovery.

    Among the ids whose name folds to the candidate, the one whose Spotify name
    IS the candidate's (after NFKD, case and all) wins; failing that, the id on
    the most tracks; failing that, whichever relevance put first. Exact name
    first is what makes it deterministic: "most tracks" alone would hand a
    search for one act to its namesake whenever the namesake had the bigger page.
    """
    want_key = normalise(artist)
    want_exact = unicodedata.normalize("NFKD", artist)
    exact: dict[str, bool] = {}
    on_tracks: dict[str, set[int]] = {}
    first_seen: dict[str, int] = {}
    for i, t in enumerate(tracks):
        for a in t.get("artists") or []:
            aid, name = a.get("id"), a.get("name") or ""
            if not aid or normalise(name) != want_key:
                continue
            first_seen.setdefault(aid, i)
            on_tracks.setdefault(aid, set()).add(i)
            exact[aid] = (exact.get(aid, False)
                          or unicodedata.normalize("NFKD", name) == want_exact)
    if not first_seen:
        return None, []
    pinned = min(first_seen, key=lambda aid: (
        not exact[aid], -len(on_tracks[aid]), first_seen[aid]))
    return pinned, [t for t in tracks
                    if any(a.get("id") == pinned for a in t.get("artists") or [])]


def drag_artists(con: duckdb.DuckDBPyConnection) -> set[str]:
    """Normalised names of library artists whose drag weight OUTWEIGHS both
    cluster weights — the pop, rap and melodic acts a discovery track must not
    be led or remixed by. Level is not enough: an artist the library cannot
    call either way is not evidence against a track."""
    return {normalise(r[0]) for r in con.execute(
        "SELECT artist_name FROM artist_clusters "
        "WHERE drag_w > greatest(garage_w, bass_w)").fetchall()}


def remix_credit(title: str) -> str | None:
    """The remixer a title names, by Stage 1b's own rule.

    credits.REMIX_CREDIT_RE and its two guards are imported, not copied, so a
    title parses the same way here as it does in track_credits: "Bounce - Radio
    Edit" names a format rather than a person, and "Song - 2019 Remix" names a
    year. The pattern sits inside what both RE2 and Python accept.
    """
    m = re.search(REMIX_CREDIT_RE, title or "", re.IGNORECASE)
    if not m:
        return None
    name = m.group(1).strip()
    if (not name or name.lower() in REMIX_FORMAT_STOPLIST
            or re.match(REMIX_NON_NAME_RE, name) or " - " in name):
        return None
    return name


def gate_discovery(tracks: list[dict], cand: dict, pinned_id: str | None,
                   on_genre: set[str], drag: set[str], vetoes: set[tuple],
                   is_fresh, k: int = config.RUN_DISCOVERY_TRACKS_PER_ARTIST,
                   ) -> list[dict]:
    """Every discovery pick passes as a TRACK before choose_tracks sees it, so
    each of the k picks it returns is usable rather than a slot wasted on a
    track the loop then refuses.

    The artist-level gate (classify_tags) says a candidate belongs; it cannot
    say a given record by them does. Spotify's relevance page for MJ Cole led
    with Tion Wayne's rap single "Crazy Love" — MJ Cole is its third credit, and
    a title collision with his 2000 record flagged it as garage.

    1. Live, or longer than RUN_MAX_DISCOVERY_MS: refused.
    2. A vetoed credit — ANY credit, features included, artist-wide or as
       (name, title) — or a track already placed: refused.
    3. A drag LEAD, or a drag remixer Spotify credits on the record: refused.
       Featured credits are not drag-tested. Every drag co-credit the
       evaluation found was the lead (Tion Wayne, Bieber on the Wideboys mix,
       Rihanna), while Inéz carries melodic dubstep by hand and sings on three
       records SJ keeps. Library-artist discovery passes through here too, and
       an unplayed Subtronics track must not be refused for its singer.
    4. genre_matched needs the title in `on_genre` AND the record to be the
       candidate's own: led by the pinned id with no other remixer named, or
       naming the candidate as remixer. "Everyday - Netsky Remix" sat in
       Rusko's list and inherited his `everyday` recording.
    5. With recording tags, only matched tracks survive. Without any (Y U QT,
       Dustycloud: keeps with nothing tagged in MusicBrainz), relevance stands.
       Deliberately NOT added: "the lead must be the candidate or a cluster
       artist" — Spotify bills a stranger's remix to the pop original first,
       so that rule would refuse nearly every remix.
    6. choose_tracks, for its stable sort and same-song dedupe. Its own flag
       agrees with step 4 by construction: after step 5, either every row is
       matched and in `on_genre`, or `on_genre` is empty and none is.
    """
    name = cand["artist_name"]
    want = normalise(name)
    eligible: list[dict] = []
    for t in tracks:
        title = t.get("track_name") or ""
        artists = t.get("artists") or []
        credited = [a.get("name") for a in artists if a.get("name")]
        dur = t.get("duration_ms")
        if is_live(title) or (dur and dur > config.RUN_MAX_DISCOVERY_MS):
            continue

        row = dict(t, artist_name=name, credited=credited)
        if (any(vetoed({"artist_name": n, "track_name": title}, vetoes)
                for n in [name, *credited]) or not is_fresh(row)):
            continue

        rc = remix_credit(title)
        credited_keys = {normalise(n) for n in credited}
        remixer = normalise(rc) if rc and normalise(rc) in credited_keys else None
        lead = artists[0] if artists else {}
        if normalise(lead.get("name") or "") in drag or (remixer and remixer in drag):
            continue

        names_cand = rc is not None and normalise(rc) == want
        own_lead = pinned_id is not None and lead.get("id") == pinned_id
        row["genre_matched"] = (_title_key(title) in on_genre
                                and (own_lead or names_cand)
                                and (rc is None or names_cand))
        eligible.append(row)

    if on_genre:
        eligible = [r for r in eligible if r["genre_matched"]]
    return choose_tracks(eligible, on_genre, k)


# --------------------------------------------------------------------------
# Duration — fill to time, not to a track count
# --------------------------------------------------------------------------


def track_duration(sp, uri: str, cache: dict) -> int | None:
    """Duration for a discovery track whose search hit carried none — search
    usually does, so this is the fallback. One request each: the batch /tracks
    form answers 403 like every other batch endpoint."""
    tid = (uri or "").rsplit(":", 1)[-1]
    if not tid:
        return None
    if tid in cache:
        return cache[tid]["duration_ms"]
    resp = sp.get(f"/tracks/{tid}", params={})
    dur = resp.get("duration_ms") if isinstance(resp, dict) and "_status" not in resp else None
    rec = {"key": tid, "duration_ms": dur}
    append_jsonl(DURATION_CACHE, rec)
    cache[tid] = rec
    return dur


def fill_to_target(rows: list[dict], target_ms: int) -> list[dict]:
    """Take tracks in order until the next one would overshoot the target.

    Stops rather than truncating mid-list, and skips a single overlong track
    instead of ending the playlist on it — a 9-minute mix should not cost the
    three good tracks behind it. Tracks with no known duration are charged the
    library median so a missing value cannot silently blow the budget.
    """
    median_ms = 3.15 * 60_000
    out, used = [], 0
    for r in rows:
        dur = r.get("duration_ms") or median_ms
        if used + dur > target_ms:
            continue
        out.append(dict(r, duration_ms=dur))
        used += dur
    return out


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def interleave(known: list[dict], discovery: list[dict]) -> list[dict]:
    """Mix the two pools in proportion, whichever is larger.

    NOT playlists.assemble, and the difference is load-bearing here. That one
    spaces anchors every `size // len(anchors)` slots, which is right for Stage
    8 (six anchors in twenty-five) but collapses to a step of 1 the moment
    anchors are the majority — every anchor at the front, every discovery track
    stapled to the end.

    At 33 tracks nobody notices. At four hours, on a 30-45 minute run, it means
    the discovery half is never reached: the listener hears the same familiar
    opening every time, which is the exact complaint this length exists to fix.

    So take from whichever pool has consumed less of itself. 45 known against 30
    discovery comes out roughly 3:2 the whole way down, and it degrades to a
    plain copy when either pool is empty.
    """
    k, d = list(known), list(discovery)
    out: list[dict] = []
    ki = di = 0
    for _ in range(len(k) + len(d)):
        k_used = ki / len(k) if k else 1.0
        d_used = di / len(d) if d else 1.0
        take_known = ki < len(k) and (k_used <= d_used or di >= len(d))
        if take_known:
            row = dict(k[ki], slot="anchor")
            ki += 1
        else:
            row = dict(d[di], slot="discovery")
            di += 1
        row["position"] = len(out)
        out.append(row)
    return out


def register_sources(con: duckdb.DuckDBPyConnection) -> None:
    # No recommendations.parquet: Stage 10 does not read Stage 5. Discovery is
    # seeded on each cluster's own artists (cluster_candidates), and the old
    # Stage 5 top-up is gone — see build_selections.
    needed = {
        "plays": config.PLAYS_PARQUET,
        "artist_tags": config.ARTIST_TAGS_PARQUET,
        "track_credits": config.DATA_DIR / "track_credits.parquet",
    }
    for name, path in needed.items():
        if not path.exists():
            raise SystemExit(f"{path} not found — run the earlier stages first.")
        con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM '{path}'")


def build_selections(con, http, sp) -> list[dict]:
    """Everything up to (but excluding) the Spotify writes, so --dry-run
    previews exactly what a live run would do."""
    tag_cache = load_jsonl(config.CACHE_DIR / "candidate_tags.jsonl", "mbid")
    genre_rec_cache = load_jsonl(GENRE_RECORDINGS_CACHE, "key")
    credited_cache = load_jsonl(SP_TRACKS_CREDITED_CACHE, "key")
    duration_cache = load_jsonl(DURATION_CACHE, "key")
    sim_cache = load_jsonl(SIMILAR_CACHE, "seed_mbid")
    vocab = load_genre_vocabulary(http)
    drag = drag_artists(con)

    pins, vetoes = load_overrides()
    target_ms = config.RUN_TARGET_MINUTES * 60_000
    known_ms = int(target_ms * config.RUN_KNOWN_FRACTION)

    # Shared across BOTH playlists. These are meant to be compared on real
    # runs, so a track — or an artist — appearing in both makes the comparison
    # say less. The first dry run put The Prodigy and Breathe Carolina in each.
    # Placements says what "the same track" means; see its docstring.
    placed = Placements()
    fresh, place = placed.fresh, placed.place
    used_discovery_artists: set[str] = set()

    out = []
    for cluster in CLUSTERS:
        label, tags = cluster["label"], list(cluster["tags"])
        print(f"\n{pretty(label)} run")
        placed.new_playlist()

        # Pins are placed first and are exempt from folding: the suffix IS the
        # record, and folding would let a different pressing take the slot.
        pinned = [place(p) for p in resolve_pins(con, pins, label)]
        known_rows = [r for r in select_known(con, label, limit=600)
                      if not vetoed(r, vetoes) and not is_live(r["track_name"])
                      and fresh(r)]
        picked: list[dict] = []
        for r in known_rows:
            if fresh(r):
                picked.append(place(r))
        known = fill_to_target(pinned + picked, known_ms)
        # Anything the duration fill rejected must not stay claimed, or it
        # cannot be offered to the other playlist.
        kept = {k["spotify_track_uri"] for k in known}
        for r in pinned + picked:
            if r["spotify_track_uri"] not in kept:
                placed.release(r)
        print(f"  {len(known)} known tracks "
              f"({sum(1 for k in known if k.get('pinned'))} pinned), "
              f"{sum(k['duration_ms'] for k in known)/60000:.0f} min")

        # Strangers come only from this cluster's own neighbourhood. There is
        # deliberately no Stage 5 top-up any more: once the seed and share fixes
        # landed, its only garage contribution was Basement Jaxx — the very
        # miss cluster seeding was built to fix — and every dubstep act it
        # passed was already seeded. A thin cluster is answered by known
        # tracks below, not by a library-wide list judged on one shared tag.
        candidates = cluster_candidates(con, http, label, tag_cache,
                                        sim_cache, vocab, vetoes)
        print(f"  {len(candidates)} candidates clear the "
              f"{config.RUN_MIN_CANDIDATE_SHARE:.2f} stranger bar")

        discovery: list[dict] = []
        budget_ms = target_ms - sum(k["duration_ms"] for k in known)
        got_ms = 0
        for cand in candidates:
            if got_ms >= budget_ms:
                break
            if normalise(cand["artist_name"]) in used_discovery_artists:
                continue
            # Only the candidate's own Spotify id survives, so a namesake's
            # records never reach the gate under their name.
            pinned_id, tracks = pin_artist_id(
                sp_artist_tracks_credited(sp, cand["artist_name"], credited_cache),
                cand["artist_name"])
            if not tracks:
                continue
            on_genre = mb_genre_recordings(http, cand["mbid"], tags, genre_rec_cache)
            for chosen in gate_discovery(tracks, cand, pinned_id, on_genre,
                                         drag, vetoes, fresh):
                # Search carries the length; /tracks is only the fallback for a
                # hit without one, and what it returns gets the same cap the
                # gate applied to everything else.
                dur = (chosen.get("duration_ms")
                       or track_duration(sp, chosen["spotify_track_uri"],
                                         duration_cache))
                if dur and dur > config.RUN_MAX_DISCOVERY_MS:
                    continue
                if got_ms + (dur or 0) > budget_ms:
                    continue
                place(chosen)
                used_discovery_artists.add(normalise(cand["artist_name"]))
                discovery.append(dict(chosen, duration_ms=dur))
                got_ms += dur or 0

        matched = sum(1 for d in discovery if d.get("genre_matched"))
        print(f"  {len(discovery)} discovery tracks "
              f"({matched} matched on recording-level tags), {got_ms/60000:.0f} min")

        # Discovery that cannot fill its third hands the time back rather than
        # shipping a short playlist. MusicBrainz barely tags current speed
        # garage, so that cluster's candidate pool is thin through no fault of
        # the listener's — and a known track they already like beats a gap.
        spare_ms = target_ms - sum(k["duration_ms"] for k in known) - got_ms
        if spare_ms > 0:
            topup = []
            for r in picked:
                if r["spotify_track_uri"] in kept:
                    continue
                dur = r.get("duration_ms") or 0
                if dur and dur <= spare_ms:
                    topup.append(place(dict(r, duration_ms=dur)))
                    spare_ms -= dur
            if topup:
                print(f"  + {len(topup)} more known tracks to fill the gap "
                      f"discovery left, "
                      f"{sum(t['duration_ms'] for t in topup)/60000:.0f} min")
                known = known + topup

        tracks = interleave(known, discovery)
        out.append({"label": label, "tags": tags, "tracks": tracks,
                    "n_known": len(known), "n_new": len(discovery)})
    return out


# --------------------------------------------------------------------------
# Spotify writes — every Stage 8 safety rule, unchanged
# --------------------------------------------------------------------------


def load_state() -> dict:
    if not config.RUNNING_STATE_JSON.exists():
        return {}
    try:
        return json.loads(config.RUNNING_STATE_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(d: dict) -> None:
    config.RUNNING_STATE_JSON.write_text(
        json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")


def publish(sp, con, selections: list[dict]) -> list[dict]:
    """Create or refresh both playlists. Never deletes, never unfollows.

    Contents are snapshotted into data/playlists.parquet BEFORE any replace, so
    nothing this overwrites goes unrecorded — hand-added tracks included.
    """
    state = load_state()
    run_date = date.today().isoformat()
    archive: list[dict] = []

    for sel in selections:
        label = sel["label"]
        name = config.RUN_PLAYLIST_NAME_TEMPLATE.format(label=label)
        pid = ensure_playlist(sp, label, name, state)
        state[label] = {"id": pid, "name": name}

        for row in playlist_items(sp, pid):
            archive.append({
                "run_date": run_date, "kind": "run_pre_replace_snapshot",
                "gap_tag": label, "playlist_id": pid, "position": None,
                "slot": None, "artist_name": row["artist_name"],
                "track_name": row["track_name"],
                "spotify_track_uri": row["uri"], "source": "spotify",
            })

        uris = [t["spotify_track_uri"] for t in sel["tracks"]]
        # PUT replaces the whole playlist in one call; /items, not /tracks.
        resp = sp.put(f"/playlists/{pid}/items", json={"uris": uris[:100]})
        if isinstance(resp, dict) and "_status" in resp:
            raise SystemExit(f"Could not write '{name}': {resp}\n" + (
                FORBIDDEN_NOTE if resp.get("_status") == 403 else ""))
        for chunk_start in range(100, len(uris), 100):
            sp.post(f"/playlists/{pid}/items",
                    json={"uris": uris[chunk_start:chunk_start + 100]})

        # `public: false` is accepted on create and then reported as true, and a
        # later PUT does not change it either. These stay off the public profile
        # page but ARE readable by direct link; the description must not claim
        # otherwise.
        sp.put(f"/playlists/{pid}", json={
            "description": config.RUN_PLAYLIST_DESCRIPTION_TEMPLATE.format(
                label=label, known=sel["n_known"], new=sel["n_new"],
                date=run_date),
        })

        for t in sel["tracks"]:
            archive.append({
                "run_date": run_date, "kind": "run_selection", "gap_tag": label,
                "playlist_id": pid, "position": t.get("position"),
                "slot": t.get("slot"), "artist_name": t.get("artist_name"),
                "track_name": t.get("track_name"),
                "spotify_track_uri": t.get("spotify_track_uri"),
                "source": "library" if t.get("slot") == "anchor" else "discovery",
            })
        print(f"  wrote {len(uris)} tracks to {name!r}")

    save_state(state)
    write_archive(con, archive)
    return archive


# --------------------------------------------------------------------------
# Report — the verification surface, as with every other stage
# --------------------------------------------------------------------------


def report(con, selections: list[dict], dry: bool) -> None:
    print()
    print("=" * 74)
    print("STAGE 10 — RUNNING PLAYLISTS" + ("  (dry run)" if dry else ""))
    print("=" * 74)

    n_cluster = con.execute(
        "SELECT cluster, count(*) FROM artist_clusters "
        "WHERE cluster IS NOT NULL GROUP BY 1 ORDER BY 1").fetchall()
    print("\nartists classified:")
    for c, n in n_cluster:
        print(f"   {c:<16} {n:>5}")

    lo = config.RUN_MIN_INTENSITY_SHARE - config.RUN_BORDERLINE_BAND
    hi = config.RUN_MIN_INTENSITY_SHARE + config.RUN_BORDERLINE_BAND
    border = con.execute(
        f"""
        SELECT artist_name,
               round(greatest(coalesce(garage_share, 0),
                              coalesce(bass_share, 0)), 2) AS best,
               cluster
        FROM artist_clusters
        WHERE greatest(coalesce(garage_share, 0), coalesce(bass_share, 0))
              BETWEEN {lo} AND {hi}
        ORDER BY best DESC LIMIT 15
        """
    ).fetchall()
    if border:
        print(f"\nborderline artists (share {lo:.2f}–{hi:.2f}, line is "
              f"{config.RUN_MIN_INTENSITY_SHARE:.2f}) — the marginal calls:")
        for a, s, c in border:
            print(f"   {a[:34]:<34} {s:>5.2f}  {c or 'excluded'}")

    untagged = con.execute(
        f"""
        SELECT p.artist_name, sum(p.played_seconds)/3600.0 AS h
        FROM plays p
        LEFT JOIN artist_tags t ON t.artist_name = p.artist_name
        WHERE t.artist_name IS NULL AND p.artist_name IS NOT NULL
          AND p.month >= {config.ANALYSIS_HORIZON_SQL}
                         - INTERVAL {config.RUN_WINDOW_MONTHS} MONTH
        GROUP BY 1 ORDER BY h DESC LIMIT 12
        """
    ).fetchall()
    if untagged:
        print("\nrecent artists with NO tags at all — invisible to this stage.")
        print("Answer them in artist_overrides.csv (name -> MBID, or hand tags):")
        for a, h in untagged:
            print(f"   {a[:34]:<34} {h:>6.1f} h")

    for sel in selections:
        mins = sum((t.get("duration_ms") or 0) for t in sel["tracks"]) / 60000
        print(f"\n--- {config.RUN_PLAYLIST_NAME_TEMPLATE.format(label=sel['label'])}")
        print(f"    {len(sel['tracks'])} tracks, {mins:.0f} min "
              f"({sel['n_known']} known + {sel['n_new']} new)")
        for t in sel["tracks"]:
            flag = "*" if t.get("pinned") else (
                "+" if t.get("slot") == "discovery" else " ")
            dur = (t.get("duration_ms") or 0) / 60000
            print(f"    {t.get('position', 0):>2}{flag} {dur:4.1f}m  "
                  f"{(t.get('artist_name') or '')[:26]:<26} "
                  f"{(t.get('track_name') or '')[:40]}")
    print("\n    * pinned by hand    + discovery (not in your library)")
    if dry:
        print("\nNothing was written. Re-run with --write to build them.")
    print("=" * 74)


def main() -> None:
    ap = argparse.ArgumentParser(description="Stage 10 running playlists")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="select and resolve, print the result, write nothing "
                         "(the default)")
    ap.add_argument("--write", dest="dry_run", action="store_false",
                    help="actually create/refresh the playlists on Spotify")
    args = ap.parse_args()

    config.ensure_dirs()
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "")
    if not client_id:
        sys.exit("SPOTIFY_CLIENT_ID is not set; see .env.example.")

    con = duckdb.connect()
    register_sources(con)
    build_artist_clusters(con)
    build_known_pool(con)

    http = Throttled(MB_MIN_INTERVAL)
    sp = Spotify(gentle_token(client_id,
                              SCOPES_READ if args.dry_run else SCOPES_WRITE))

    selections = build_selections(con, http, sp)
    if not args.dry_run:
        publish(sp, con, selections)
    report(con, selections, args.dry_run)


if __name__ == "__main__":
    main()
