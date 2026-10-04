"""Stage 8 — render the genre-gap analysis into Spotify playlists.

    .venv/bin/python playlists.py --dry-run     # selections only, no Spotify writes
    .venv/bin/python playlists.py               # build/refresh the playlists

One playlist per under-explored rising genre (Stage 7's gap analysis), capped
at N_PLAYLISTS. Each mixes ANCHOR_TRACKS familiar tracks — the listener's own
recent plays that serve that genre, judged on the remixer where the title
names one — with discovery tracks from Stage 5's candidate artists: records a
candidate leads or remixed, never one they only feature on (candidate_led).
Anchors and strangers clear the same bar: the genre's share of the artist's
whole tag weight (serving_sql), not the mere presence of a tag.

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
import csv
import json
import re
import time
import unicodedata
from datetime import date

import duckdb
import requests

import config
from credits import remix_credit
from enrich import (MB_MIN_INTERVAL, MBID_RE, Throttled, load_cache,
                    load_genre_vocabulary, load_overrides, normalise,
                    resolve_via_musicbrainz)
from recommend import (CANDIDATE_TAG_CACHE, SIMILAR_CACHE, append_jsonl,
                       fetch_candidate_tags, fetch_similar, load_jsonl)
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
# Seed-name resolutions this stage asked MusicBrainz for, the way Stage 9 keeps
# its own (consolidate.CONSOLIDATE_CACHE): Stage 2's cache is READ first, free,
# but never written from here — a seed is not a library artist and must not
# appear in artist_tags. Answers only; an "error" is never written.
PLAYLIST_SEED_CACHE = config.CACHE_DIR / "playlist_seed_resolution.jsonl"

# Discovery searches with WHO IS ON each track, shared by Stages 8 and 10: every
# hit keeps Spotify's own credit list ({name, id}, in Spotify's order) and the
# duration search carries for free. Stage 8's first cache,
# spotify_artist_tracks.jsonl, kept a name and a URI per hit and every hit was
# labelled as the candidate — so Tion Wayne's rap single went out as MJ Cole
# garage, and ILLENIUM's "Free Fall" as RUNN's indie. That file is retired, not
# migrated: nothing reads or writes it any more, and an append-only cache is
# never rebuilt. An artist only it answered costs one search here, once.
SP_TRACKS_CREDITED_CACHE = config.CACHE_DIR / "spotify_artist_tracks_credited.jsonl"

# Spotify renamed the playlist endpoints on 2026-02-11 and the old paths now
# answer 403 "Forbidden" — not 404, which is why this reads as a permissions
# problem and is not one. Verified against the live API 2026-07-31:
#
#     GET/PUT/POST /playlists/{id}/tracks   403   <- gone
#     GET/PUT/POST /playlists/{id}/items    200   <- current
#     POST /users/{uid}/playlists           403   <- gone (per-user endpoints
#                                                  were removed outright)
#     POST /me/playlists                    201   <- current
#
# The response nesting moved with them: a playlist's `tracks` object is now
# `items`, and each row's `track` is now `item`. Do not "restore" the old paths.
FORBIDDEN_NOTE = """
  Spotify refused a playlist call with 403.

  Note that the pre-February-2026 paths (/playlists/{id}/tracks,
  POST /users/{id}/playlists) also answer 403 rather than 404, so a 403 here
  may mean a stale endpoint rather than a missing permission. This module uses
  the current paths; check those first if you are debugging.

  If the paths are right, the likely causes are an expired consent (delete
  .cache/spotify_token.json and re-run) or a Development Mode limit —
  developer.spotify.com/dashboard.
"""


# --------------------------------------------------------------------------
# Selection — pure data logic, no network
# --------------------------------------------------------------------------


def select_gaps(con: duckdb.DuckDBPyConnection, limit: int | None = -1) -> list[dict]:
    """Top gap genres, hard-capped at N_PLAYLISTS by agreement.

    `limit=None` returns the whole table, which the override loader uses to
    look up trend numbers for a label it did not choose.
    """
    n = config.N_PLAYLISTS if limit == -1 else limit
    cols = ["tag", "gap_score", "hours", "n_artists", "rel_change_per_year"]
    rows = con.execute(
        f"""
        SELECT {', '.join(cols)} FROM genre_gaps
        ORDER BY gap_score DESC
        {'' if n is None else f'LIMIT {n}'}
        """
    ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def serving_sql(tags_rel: str, key: str, n_tags: int) -> str:
    """The `key`s in `tags_rel` whose genre weight leans on a spec's tags.

    Carrying a tag is not serving a genre. Halsey carries indie pop(3) beside
    pop(6), electropop(4) and alternative pop(4); Ellie Goulding carries indie
    pop(3) and indie folk(2) beside electropop(7) and dance-pop(5). A set test
    ("any spec tag") admitted both, they are heavily played, and they took the
    top anchor slots of an indie playlist. So an artist serves the spec only
    when the spec's tags hold at least ANCHOR_MIN_TAG_SHARE of their whole
    genre weight — Halsey's is 0.09, Ellie Goulding's 0.16 — AND one of those
    tags clears MIN_TAG_COUNT_FOR_ANCHOR, the floor that says somebody stands
    behind it. The greatest(tag_count, 0) clamps are load-bearing, not a
    repeat: artist_tags keeps MusicBrainz's negative counts (163 genre rows,
    down to -6; only Stage 3 and the candidate cache clamp their own copies).
    Unclamped, a downvote elsewhere shrinks the denominator — spec 1 beside
    5 and -2 reads 0.25 instead of 0.17 — and a downvoted spec tag cancels a
    real one. Do not delete them as redundant.

    ONE definition for both sides — library anchors over artist_tags and
    strangers over the candidate tag cache — so a stranger can never clear
    less than a library artist does. `tags_rel` must expose (key, tag,
    tag_count) restricted to genres; bind the spec's tags TWICE, in order.
    """
    ph = ", ".join("?" for _ in range(n_tags))
    return f"""
        SELECT {key}
        FROM {tags_rel}
        GROUP BY {key}
        HAVING sum(CASE WHEN tag IN ({ph}) THEN greatest(tag_count, 0) ELSE 0 END)
               / nullif(sum(greatest(tag_count, 0)), 0)
                   >= {config.ANCHOR_MIN_TAG_SHARE}
           AND max(CASE WHEN tag IN ({ph}) THEN tag_count END)
                   >= {config.MIN_TAG_COUNT_FOR_ANCHOR}
    """


def select_anchor_tracks(con: duckdb.DuckDBPyConnection, tags: list[str],
                         taken: set[tuple[str, str]] | None = None,
                         exclude=()) -> list[dict]:
    """The listener's own recent favourites that serve these genres.

    `tags` is a list because a playlist may span several related genres — an
    indie playlist wants indie pop and indie folk together, and a bass one
    wants dubstep alongside its neighbours. The spec's tags are weighed
    together, per artist, so an artist carrying three of them is still one
    artist rather than three copies of its listening time.

    WHO JUDGES A TRACK. The remixer its title names (credits.remix_credit,
    Stage 1b's own rule), else the album artist. The export bills a remix to
    the ORIGINAL artist, so "Colors - Ian Asher Remix" and "Without Me -
    ILLENIUM Remix" were judged as Halsey tracks and anchored indie, and "I Was
    Made For Lovin' You - Disco Lines Remix" anchored heavy metal as KISS. It
    is read from the TITLE, not from track_credits.credit_type: credits.py
    types every non-first poller artist as `featured`, so the polled pressing
    of "Black Out Days - Subtronics Remix" has Subtronics as a feature. A
    remixer with no genre tags leaves the track unable to anchor at all — the
    original artist's genre is exactly the wrong answer for a remix.

    WHAT QUALIFIES. serving_sql: the judge's weight on the spec's tags as a
    share of their whole genre weight, plus the MIN_TAG_COUNT_FOR_ANCHOR floor.

    ONE SONG, ONE ANCHOR. Spotify presses the album cut, the single and the
    remaster as distinct URIs, and the dry run anchored Solo Kei's "cowboy
    killers" twice in one playlist. Within a playlist the key is (album
    artist, song key), so an original and its remix do not both take anchor
    slots. `taken` holds the (judge, song key) pairs earlier playlists in the
    same run placed (select_run_anchors) — the same song used to anchor indie
    AND indie rock — and the first playlist to claim one keeps it. That key
    keeps a remix apart from its original, so a remix anchoring garage does
    not keep the original out of the playlist it belongs in; Stage 10's
    Placements draws the same line. Taken songs go before dedupe and dedupe
    before the per-artist cap, so a freed slot passes to a different song
    instead of being lost.

    EXCLUDE. A spec's `exclude` names artists kept out of this playlist. Both
    the judge and the album artist are tested, so a remix judged as an
    excluded remixer goes too. Filtering happens BEFORE the per-artist cap and
    LIMIT, so an excluded artist's slots pass to someone else rather than
    shrinking the playlist.

    Tracks rank by the listener's own recent play time — their URIs come
    straight from the export, so no search is ever needed for anchors.
    Recording-level genre matching is NOT attempted here: library tracks have
    no recording MBIDs, and resolving them is the per-track explosion this
    project has twice declined. The per-artist cap stays on the ALBUM artist,
    as in Stage 10.
    """
    if not tags:
        return []
    register_song_key(con)
    register_remix_credit(con)
    register_norm_name(con)
    excl = sorted({normalise(n) for n in exclude})
    cols = ["artist_name", "judge", "track_name", "spotify_track_uri", "hours",
            "song_key"]
    rows = con.execute(
        f"""
        WITH recent AS (
            SELECT p.artist_name, p.track_name, p.spotify_track_uri,
                   sum(p.played_seconds) / 3600.0 AS hours
            FROM plays p
            WHERE p.spotify_track_uri IS NOT NULL
              -- The export horizon, not the latest play: a polled row past the
              -- export's coverage would slide the window forward.
              AND p.month >= {config.ANALYSIS_HORIZON_SQL}
                             - INTERVAL {config.ANCHOR_WINDOW_MONTHS} MONTH
            GROUP BY 1, 2, 3
        ),
        judged AS (
            SELECT *,
                   coalesce(remix_credit(track_name, artist_name), artist_name)
                       AS judge,
                   song_key(track_name) AS song_key
            FROM recent
        ),
        serving AS (
            {serving_sql("(SELECT artist_name, tag, tag_count FROM artist_tags "
                         "WHERE is_genre)", "artist_name", len(tags))}
        ),
        fresh AS (
            SELECT j.*
            FROM judged j
            JOIN serving s ON s.artist_name = j.judge
            WHERE NOT list_contains(?::VARCHAR[][], [j.judge, j.song_key])
              AND NOT list_contains(?::VARCHAR[], norm_name(j.judge))
              AND NOT list_contains(?::VARCHAR[], coalesce(norm_name(j.artist_name), ''))
        ),
        one_per_song AS (
            SELECT * FROM fresh
            QUALIFY row_number() OVER (
                PARTITION BY artist_name, song_key
                ORDER BY hours DESC, spotify_track_uri
            ) = 1
        )
        SELECT {', '.join(cols)}
        FROM one_per_song
        QUALIFY row_number() OVER (
            PARTITION BY artist_name ORDER BY hours DESC, spotify_track_uri
        ) <= {config.TRACKS_PER_ARTIST}
        ORDER BY hours DESC, spotify_track_uri
        LIMIT {config.ANCHOR_TRACKS}
        """,
        [*tags, *tags, sorted([list(k) for k in taken or ()]), excl, excl],
    ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def select_run_anchors(con: duckdb.DuckDBPyConnection,
                       specs: list[dict]) -> list[list[dict]]:
    """Anchors for every playlist in build order, each song anchoring once.

    Playlists that share a genre neighbourhood share artists — Solo Kei
    carries indie pop and indie rock at a third each — and each playlist
    picking alone anchored "cowboy killers" in both. `taken` carries (judge,
    song key) forward, so the first playlist to claim a song keeps it and the
    next moves on down its own list.
    """
    taken: set[tuple[str, str]] = set()
    out = []
    for spec in specs:
        anchors = select_anchor_tracks(con, spec["tags"], taken,
                                      spec.get("exclude", ()))
        taken |= {(a["judge"], a["song_key"]) for a in anchors}
        out.append(anchors)
    return out


def _serving_mbids(con: duckdb.DuckDBPyConnection, mbids: list[str],
                   tags: list[str], tag_cache: dict) -> set[str]:
    """Which of `mbids` serve `tags`, judged by serving_sql over the candidate
    tag cache — the one bar every stranger clears, Stage 5's and seeded alike."""
    con.execute("CREATE OR REPLACE TEMP TABLE _candidate_tags "
                "(mbid VARCHAR, tag VARCHAR, tag_count INTEGER)")
    vectors = {(m, t.get("tag"), t.get("count"))
               for m in mbids for t in (tag_cache.get(m) or {}).get("tags", [])}
    if vectors:
        con.executemany("INSERT INTO _candidate_tags VALUES (?, ?, ?)", list(vectors))
    return {r[0] for r in con.execute(
        serving_sql("_candidate_tags", "mbid", len(tags)), [*tags, *tags]
    ).fetchall()}


def select_candidates(con: duckdb.DuckDBPyConnection, tags: list[str],
                      tag_cache: dict, exclude=()) -> list[dict]:
    """Stage 5 candidates whose tag vector serves these genres.

    Library artists are excluded by normalised name: the discovery slots are
    for strangers, and the familiar ones already have the anchor slots.

    A stranger must clear the bar a library anchor clears — serving_sql, the
    same share and the same floor — and never less. Nobody has vouched for a
    stranger, which is why Stage 10 gates them harder still. Under the old
    "any spec tag" rule the heavy metal playlist filled with Papa Roach
    (heavy metal 1 of 26), Deftones (1 of 60) and Rage Against the Machine
    (1 of 39), and garage with Röyksopp (1 of 35) and Pendulum (1 of 52).
    """
    if not tags:
        return []
    excl = {normalise(n) for n in exclude}
    known = {normalise(r[0]) for r in con.execute(
        "SELECT DISTINCT artist_name FROM plays WHERE artist_name IS NOT NULL"
    ).fetchall()}
    rows = con.execute(
        "SELECT artist_name, mbid, score FROM recommendations ORDER BY score DESC"
    ).fetchall()
    serving = _serving_mbids(con, [m for _, m, _ in rows], tags, tag_cache)
    return [{"artist_name": name, "mbid": mbid, "score": score}
            for name, mbid, score in rows
            if normalise(name) not in known and normalise(name) not in excl
            and mbid in serving]


def _pipe_list(raw: str | None) -> list[str]:
    """A pipe-separated override cell as a list, trimmed, blanks dropped."""
    return [t.strip() for t in (raw or "").split("|") if t.strip()]


def load_playlist_specs(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """What playlists to build: the override file if present, else the gaps.

    The override exists because the gap ranking answers "where is your
    listening heading", weighted by seconds — and seconds are dominated by
    whatever plays during a workout, where music is a metronome rather than a
    choice. The export records no activity, so it cannot tell functional
    listening from attentive listening, and no amount of analysis recovers it.
    The file is where a human supplies what the data does not contain, exactly
    as artist_overrides.csv answers Stage 2's review list.

    A label that IS a gap genre keeps its trend numbers and its description;
    one that is not is marked pinned, and says so rather than claiming to be
    rising when the history says it is falling.

    `seeds` names artists whose ListenBrainz neighbours feed this playlist's
    discovery (seed_candidates); `exclude` keeps artists out of this playlist
    only — anchors, candidates and any discovery track crediting them.
    """
    gaps = {g["tag"]: g for g in select_gaps(con, limit=None)}
    if not config.PLAYLIST_OVERRIDES_CSV.exists():
        return [{"label": g["tag"], "tags": [g["tag"]], "seeds": [], "exclude": [],
                 "pinned": False, "gap": g}
                for g in select_gaps(con)]

    specs = []
    with config.PLAYLIST_OVERRIDES_CSV.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            label = (row.get("label") or "").strip()
            if not label or label.startswith("#"):
                continue
            tags = _pipe_list(row.get("tags")) or [label]
            specs.append({"label": label, "tags": tags,
                          "seeds": _pipe_list(row.get("seeds")),
                          "exclude": _pipe_list(row.get("exclude")),
                          "pinned": label not in gaps, "gap": gaps.get(label)})
    if len(specs) > config.N_PLAYLISTS:
        print(f"  ! {config.PLAYLIST_OVERRIDES_CSV.name} lists {len(specs)} "
              f"playlists; N_PLAYLISTS caps at {config.N_PLAYLISTS} — using the "
              f"first {config.N_PLAYLISTS}")
    return specs[:config.N_PLAYLISTS]


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
        if not want_anchor:
            di = take(discovery, di)
            if di >= len(discovery):
                # Discovery is spent. Fall back to whatever anchors are left
                # rather than ending here: the anchors sit at spaced
                # positions, so stopping at the first unfilled gap throws away
                # every anchor after it. A genre with six qualifying library
                # artists and no candidates produced a ONE-track playlist.
                ai = take(anchors, ai)
                if ai >= len(anchors):
                    break
                want_anchor = True
        pool, idx = (anchors, ai) if want_anchor else (discovery, di)

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


def _register_udf(con: duckdb.DuckDBPyConnection, name: str, fn,
                  null_handling: str = "default", n_args: int = 1) -> None:
    if not con.execute("SELECT count(*) FROM duckdb_functions() "
                       "WHERE function_name = ?", [name]).fetchone()[0]:
        con.create_function(name, fn, ["VARCHAR"] * n_args, "VARCHAR",
                            null_handling=null_handling)


def register_song_key(con: duckdb.DuckDBPyConnection) -> None:
    """song_key(title) in SQL — _title_key itself, not a copy.

    Its normalise() folds accents through NFKD and drops "the" and "and",
    which DuckDB cannot reproduce, and a second definition is exactly how two
    keys for one idea drift apart. Stage 10 imports this one.
    """
    _register_udf(con, "song_key", _title_key)


def register_norm_name(con: duckdb.DuckDBPyConnection) -> None:
    """norm_name(name) in SQL — enrich.normalise itself, so an exclude row
    matches an artist exactly as every other name comparison here does."""
    _register_udf(con, "norm_name", normalise)


def register_remix_credit(con: duckdb.DuckDBPyConnection) -> None:
    """remix_credit(title, album_artist) in SQL — credits.remix_credit itself,
    so the anchor judge parses a title exactly as Stage 1b does. The album
    artist is what keeps "(Taylor's Version)" from naming "Taylor". NULL when
    the title names no remixer, hence null_handling='special'."""
    _register_udf(con, "remix_credit", remix_credit, null_handling="special",
                  n_args=2)


def lead_artist(track: dict) -> str | None:
    """A Spotify track's first-credited artist: the album artist remix_credit
    needs to tell a self-version from a remix on a search result."""
    artists = track.get("artists") or []
    return (artists[0] or {}).get("name") if artists else None


def mb_genre_recordings(http: Throttled, artist_mbid: str, tags: list[str],
                        cache: dict) -> set[str]:
    """Folded titles of this artist's recordings tagged with any of `tags`.

    One request answers "which of their work is dubstep". What it cannot answer
    is which of it is any good — MusicBrainz orders by Lucene relevance, so
    demos and 5.1 remixes rank alongside the hits (Aphex Twin + techno leads
    with a SAW:II fragment). That is why this is a PREFERENCE SET applied over
    Spotify's relevance order, never an ordering in its own right.

    Several tags go in one request as a Lucene OR, so a blended playlist costs
    the same single lookup per artist as a single-genre one.

    An empty ANSWER is cached like any other: "nobody tagged this artist's
    recordings dubstep" is a fact, not something to re-ask every run. A failed
    REQUEST is not cached — that is a missing answer, and caching it would
    freeze a transient 503 into a permanent "this artist has nothing".
    """
    tags = sorted(set(tags))
    if not tags:
        return set()
    key = f"{artist_mbid}::{'|'.join(tags)}"
    if key in cache:
        return set(cache[key]["titles"])
    clause = " OR ".join(f'tag:"{t}"' for t in tags)
    r = http.get(MB_RECORDING_URL, params={
        "query": f"arid:{artist_mbid} AND ({clause})",
        "fmt": "json", "limit": MB_RECORDING_LIMIT,
    })
    if r is None or r.status_code != 200:
        print(f"    ! MusicBrainz gave no answer for {'/'.join(tags)} recordings "
              f"({'no response' if r is None else r.status_code}); "
              f"treating as unknown, not as empty")
        return set()
    titles = {k for k in (_title_key(rec.get("title", ""))
                          for rec in r.json().get("recordings", [])) if k}
    rec = {"key": key, "artist_mbid": artist_mbid, "tags": tags,
           "titles": sorted(titles)}
    append_jsonl(GENRE_RECORDINGS_CACHE, rec)
    cache[key] = rec
    return titles


def is_live(track_name: str) -> bool:
    """A live recording, refused as a pick however on-genre it is.

    Crowd noise, a spoken intro and whatever tempo the band picked on the night
    are invisible to a genre tag: the track is correctly classified and still
    wrong. Stage 10 found it first (Katy B's iTunes Festival "Go Away"); Stage 8
    offered Motörhead's "Bomber - Lemmy's 50th Birthday, Live at The Whisky" as
    heavy metal discovery. One definition, shared by both.

    Matched structurally rather than as a substring (config.RUN_LIVE_TITLE_RE,
    named for the stage that introduced it). A bare /live/ would take Zeds
    Dead's "Alive" and Dustycloud's "Alive" — both wanted.
    """
    return bool(re.search(config.RUN_LIVE_TITLE_RE, track_name or ""))


def candidate_led(tracks: list[dict], artist: str,
                  pinned_id: str | None) -> list[dict]:
    """Only the candidate's own records: led by their pinned Spotify id, or a
    remix whose title names them.

    A search for an artist returns every record they are credited on, and a
    FEATURE IS NOT THEIR RECORD. RUNN's results carried "Free Fall" — an
    ILLENIUM record featuring RUNN, which Spotify credits ILLENIUM then RUNN —
    and it went out in the indie frontier as RUNN's work, putting ILLENIUM in
    the playlist through a featured singer. Discovery offers a track on the
    strength of the candidate's genre, so the candidate must have made it: their
    pinned id (pin_artist_id, so a namesake never leads for them) is the FIRST
    credit, or credits.remix_credit names them. The second clause is what keeps
    their remixes — Spotify bills a remix to the original artist first, and
    "Song - RUNN Remix" is RUNN's record however it is billed. A title's
    "feat." is not read: Spotify's lead is the credit, and it already lists the
    featured act second.

    Relevance order is kept. Runs BEFORE discovery_eligible, which then drops
    live recordings and remixes a candidate leads but someone else made
    ("Alive - Trivecta Remix"). Stage 10 draws its own line in
    gate_discovery — it tests the lead only for drag, and asks for the
    candidate's own record only for a genre match — so do not unify the two.
    """
    if not pinned_id:
        return []
    me = normalise(artist or "")
    out = []
    for t in tracks:
        ids = [a.get("id") for a in t.get("artists") or []]
        rc = remix_credit(t.get("track_name") or "", lead_artist(t))
        # The remixer clause still wants the pinned id ON the record: the
        # title's name folds like a namesake's ("Big Tune - DEM2 Remix" is not
        # Dem 2's), and only the id tells them apart.
        if (ids[:1] == [pinned_id]
                or (rc is not None and normalise(rc) == me and pinned_id in ids)):
            out.append(t)
    return out


def drop_excluded(tracks: list[dict], exclude) -> list[dict]:
    """Drop any track whose Spotify credit list names an excluded artist.

    The whole credit list, not just the lead: an exclude row says "not in this
    playlist", and a Drake feature on someone else's record is still Drake.
    """
    excl = {normalise(n) for n in exclude}
    if not excl:
        return list(tracks)
    return [t for t in tracks
            if not any(normalise(a.get("name") or "") in excl
                       for a in t.get("artists") or [])]


def discovery_eligible(tracks: list[dict], artist: str) -> list[dict]:
    """A candidate's search results minus what is not really theirs to offer.

    Two refusals, in relevance order so choose_tracks still sees the popularity
    proxy intact:
    - live recordings (is_live);
    - a remix whose title names someone OTHER than the candidate. Spotify files
      a remix under the original artist, so RUNN's results include "Alive -
      Trivecta Remix" — a melodic dubstep record by Trivecta — and it reached
      the indie frontier as RUNN's work. Anchors are judged by the remixer
      (select_anchor_tracks); discovery has no tags for an arbitrary remixer, so
      the honest move is not to offer the track at all. The candidate's OWN
      remix of someone else stays.
    """
    me = normalise(artist or "")
    out = []
    for t in tracks:
        title = t.get("track_name") or ""
        if is_live(title):
            continue
        rc = remix_credit(title, lead_artist(t))
        if rc and normalise(rc) != me:
            continue
        out.append(t)
    return out


def choose_tracks(tracks: list[dict], on_genre: set[str], k: int) -> list[dict]:
    """Prefer the artist's on-genre work; Spotify relevance does the rest.

    `tracks` arrives in Spotify's relevance order, which is the popularity
    proxy. The sort is STABLE and keyed only on the genre flag, so relevance
    survives inside each group: the artist's on-genre work rises above their
    bigger off-genre hit, but between two on-genre tracks the better-known one
    still leads. With no on-genre data the sort is a no-op and this degrades
    cleanly to plain relevance order.

    One song is then allowed one slot. Spotify returns the album cut, the
    single, the remaster and the deluxe edition as distinct URIs, so deduping
    on URI alone hands an artist's two slots to the same song twice — the first
    dry run produced 'Papa Roach - Last Resort' back to back. Dedupe runs after
    the sort and before the cap, so the best-ranked pressing survives and the
    freed slot goes to a different song rather than being lost.
    """
    flagged = [dict(t, genre_matched=_title_key(t.get("track_name", "")) in on_genre)
               for t in tracks]
    flagged.sort(key=lambda t: not t["genre_matched"])

    out, seen = [], set()
    for t in flagged:
        song = _title_key(t.get("track_name", ""))
        if song in seen:
            continue
        seen.add(song)
        out.append(t)
        if len(out) >= k:
            break
    return out


# --------------------------------------------------------------------------
# Spotify — the ordering, the URIs, and later the shelf
# --------------------------------------------------------------------------


class Spotify:
    """Thin bearer-token client.

    429s are honoured — Spotify's Retry-After is real, unlike MusicBrainz's —
    but capped, so a bad header cannot park a run for hours. A 429 that is
    still a 429 after that one wait sets `rate_limited`: the app is locked out,
    not briefly throttled, and every further request only extends the lockout.
    build_selections stops searching on it. There is deliberately NO delete
    verb: this stage must never be able to remove a playlist, and the cheapest
    way to guarantee that is to not implement it.
    """

    def __init__(self, token: str):
        self.h = {"Authorization": f"Bearer {token}"}
        self._last = 0.0
        self.rate_limited = False

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
            if r.status_code == 429:
                self.rate_limited = True
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
    'A$AP Rocky' and 'ASAP Rocky' are one artist. A featured credit counts
    HERE — the artist is genuinely on the track, and the cache records the
    fact. Whether a feature may be OFFERED is each stage's call: Stage 8's
    candidate_led refuses it, Stage 10's gate_discovery has its own rule.
    """
    want = normalise(artist)
    return want in {normalise(a.get("name", "")) for a in item.get("artists", [])}


def sp_artist_tracks_credited(sp, artist: str, cache: dict) -> list[dict]:
    """This artist's tracks in Spotify's relevance order, with who is on each.

    One /search, one page of SP_SEARCH_LIMIT, hits kept only where the artist
    is really credited (_artist_match). Relevance order is the whole point:
    with ListenBrainz popularity down it is the only popularity signal left,
    so nothing here re-sorts it. Each hit keeps every credited artist as
    {name, id}, in Spotify's order — the lead is artists[0], which is how
    Stage 8's candidate_led tells a candidate's record from a feature — and
    `duration_ms`, which search returns for free, so Stage 10's length cap and
    time budget cost no /tracks request unless a hit arrives without one.
    `artist_name` is the name asked for, not the credit string, so the
    per-artist cap downstream stays keyed on one spelling.

    Only an ANSWER is cached. A 200 with no usable hit is a fact ("Spotify does
    not carry them") and is asked once. A 429, an error envelope or no response
    at all is a missing answer, and caching it would freeze a transient failure
    into "this artist has no tracks" for good.

    Keyed on normalise(artist), so 'DEM2' and 'Dem 2' share a key. The record
    holds every credited id, and pin_artist_id separates the two on read; a
    namesake is a read-time filter, not a cache repair. Stages 8 and 10 share
    the file, so an artist either stage has searched costs the other nothing.
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


def _confirmed_gone(resp) -> bool:
    """True only when Spotify has said, in terms, that this id no longer
    exists (404) — never for a 5xx, a 429, an error envelope carrying some
    other status, or no response at all (a network failure, `sp.get` -> None).

    That distinction is the whole fix for the 2026-09-29 outage: a 502 on the
    stored-ID check used to read exactly like a 404 (`_alive` is False either
    way), so an outage fell through to name-adopt and then POSTed a fresh
    playlist — a duplicate, had Spotify accepted the create. A Spotify FAILURE
    is never evidence a playlist is gone; only Spotify affirmatively saying so
    is.
    """
    return isinstance(resp, dict) and resp.get("_status") == 404


def ensure_playlist(sp, tag: str, name: str, state: dict,
                    aliases: tuple[str, ...] = ()) -> str:
    """Resolve the playlist this stage owns for `tag`, creating if needed.

    Identity is the stored ID — immune to the user renaming things. The name
    match is an exact-template fallback for first runs and lost state; it is
    EXACT on purpose, case and all. A near-miss is somebody's hand-made
    playlist, and adopting it would mean this stage overwrites something a
    person built. Creating a duplicate is the cheap mistake; overwriting is
    the expensive one — but so is a duplicate born of a Spotify outage, so
    both checks below refuse to create unless they have POSITIVE evidence
    there is nothing to adopt, not merely a failure to find it.

    `aliases` are names the playlist carried before a rename (Stage 10's
    garage run was "speed garage run · Claude"), tried in order after `name`
    and just as exactly. The current name wins wherever it sits in the
    listing; an alias is adopted only when nothing carries the current name,
    so lost state after a rename finds the old playlist instead of creating
    a second. Stage 8 passes none.

    THE STORED ID falls through to name-adopt ONLY on a confirmed 404. Any
    other failure — 5xx, 429, an error envelope, no response — raises
    immediately: the id may well still be alive, and guessing otherwise is how
    a 502 outage on 2026-09-29 nearly duplicated a run playlist.

    THE LISTING must be read WHOLE — every page answered — before "nothing
    matched" is trusted enough to create. A page failing partway used to just
    stop the loop and fall out the bottom as if the rest of the library held
    no match; now any page failing (including the very first request) raises,
    because an unread page could hold the exact name this call is looking for.
    """
    entry = state.get(tag) or {}
    if entry.get("id"):
        resp = sp.get(f"/playlists/{entry['id']}", params={"fields": "id,name"})
        if _alive(resp):
            return entry["id"]
        if not _confirmed_gone(resp):
            raise SystemExit(
                f"Spotify is failing on the stored playlist id for {tag!r} "
                f"({entry['id']}): {resp}. Not creating a playlist that may "
                "already exist — a Spotify failure is never evidence a "
                "playlist is gone.")
        # Confirmed 404: genuinely gone. Fall through to the name/alias search.

    under_alias: dict[str, str] = {}
    page = sp.get("/me/playlists", params={"limit": 50})
    listed_whole = False
    while isinstance(page, dict) and "_status" not in page:
        for item in page.get("items", []):
            if item.get("name") == name:
                return item["id"]
            if item.get("name") in aliases:
                under_alias.setdefault(item["name"], item["id"])
        nxt = page.get("next")
        if not nxt:
            listed_whole = True
            break
        page = sp.get(nxt.removeprefix(SP_API), params=None)
    if not listed_whole:
        raise SystemExit(
            f"Spotify is failing while listing playlists (looking for "
            f"{name!r}): {page}. Not creating a playlist that may already "
            "exist — a Spotify failure is never evidence a playlist is gone.")
    for alias in aliases:
        if alias in under_alias:
            return under_alias[alias]

    # POST /me/playlists, not /users/{uid}/playlists — the per-user endpoints
    # were removed in Feb 2026 and the old path now 403s.
    created = sp.post("/me/playlists", json={
        "name": name, "public": False,
        "description": "created by spotify-trend-analysis",
    })
    if not _alive(created):
        raise SystemExit(
            f"Could not create playlist '{name}': {created}\n" + (
                FORBIDDEN_NOTE if isinstance(created, dict)
                and created.get("_status") == 403 else ""))
    return created["id"]


def playlist_items(sp, pid: str) -> list[dict]:
    """Current contents, for the pre-replace snapshot. Nothing we overwrite
    goes unrecorded — hand-added tracks included.

    `/items` and the `item` key, not `/tracks` and `track`: renamed Feb 2026.
    """
    out: list[dict] = []
    path = (f"/playlists/{pid}/items"
            "?fields=items(item(uri,name,artists(name))),next&limit=100")
    resp = sp.get(path, params=None)
    while isinstance(resp, dict) and "_status" not in resp:
        for it in resp.get("items", []):
            t = it.get("item") or {}
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


def resolve_seed(con: duckdb.DuckDBPyConnection, http, seed: str,
                 stage2: dict, mine: dict,
                 overrides: dict | None = None) -> dict | None:
    """A seed cell -> {"artist_name", "mbid"}, or None. Never guesses.

    `Name=MBID` is used as given. Otherwise, in order: artist_overrides.csv
    (an MBID row is the answer; IGNORE/NONE is a refusal), the library
    (artist_tags, matched through normalise), Stage 2's cache, this stage's
    cache, then MusicBrainz. The override file comes before every cache for
    the reason consolidate.resolve_missing gives: Stage 2's raw cache still
    holds whatever the search matched before a NONE row was written — PLAT.
    sat on the vaporwave ＰＬＡＴ — and the MBID is what seeds ListenBrainz. A
    tags-only override row leaves resolution alone, as it does in Stage 2.
    Only status "resolved" with an MBID counts — the Henrik lesson: a wrong
    artist invents listening in genres never played, which is worse than none.
    A library name with no MBID carries hand tags under NONE and is not
    searched again.
    """
    name, _, pinned = (p.strip() for p in seed.partition("="))
    if pinned:
        if MBID_RE.fullmatch(pinned.lower()):
            return {"artist_name": name, "mbid": pinned.lower()}
        print(f"  ⚠ seed {seed!r}: {pinned!r} is not an MBID — skipped")
        return None
    ov = (overrides or {}).get(normalise(name))
    if ov is not None:
        if ov.get("mbid"):
            return {"artist_name": name, "mbid": ov["mbid"]}
        if ov.get("ignore") or ov.get("none"):
            print(f"  ⚠ seed {name!r} is answered "
                  f"{'IGNORE' if ov.get('ignore') else 'NONE'} in "
                  f"{config.ARTIST_OVERRIDES_CSV.name} — skipped. Give it as "
                  "Name=MBID if MusicBrainz has since gained an entry.")
            return None
    register_norm_name(con)
    row = con.execute(
        "SELECT artist_name, max(mbid) FROM artist_tags "
        "WHERE norm_name(artist_name) = ? GROUP BY 1 ORDER BY 1 LIMIT 1",
        [normalise(name)]).fetchone()
    if row is not None:
        if row[1]:
            return {"artist_name": row[0], "mbid": row[1]}
        print(f"  ⚠ seed {name!r} is a library artist with no MusicBrainz id "
              "(answered NONE in artist_overrides.csv) — give it as Name=MBID")
        return None
    rec = stage2.get(name) or mine.get(name)
    if rec is None or rec.get("status") == "error":
        rec = resolve_via_musicbrainz(http, name)
        if rec.get("status") != "error":
            append_jsonl(PLAYLIST_SEED_CACHE, rec)
            mine[name] = rec
    if rec.get("status") == "resolved" and rec.get("mbid"):
        return {"artist_name": name, "mbid": rec["mbid"]}
    print(f"  ⚠ seed {name!r} did not resolve exactly on MusicBrainz "
          f"({rec.get('status')}) — skipped. Answer it as Name=MBID.")
    return None


def seed_candidates(con: duckdb.DuckDBPyConnection, http, spec: dict, *,
                    sim_cache: dict, tag_cache: dict, stage2: dict, mine: dict,
                    vocab: set[str], known: set[str],
                    overrides: dict | None = None) -> list[dict]:
    """Strangers near the artists a person named for this playlist.

    Stage 5 seeds on the whole taste vector, dominated by workout listening, so
    a genre the listener WANTS but barely plays (vaporwave: ~0 h) gets no
    supply from it. A seed is the human's statement of where the playlist
    should go; ListenBrainz supplies who is near it.

    Similarity is normalised per seed before pooling — ListenBrainz scores share
    no scale (Skrillex tops out at 3955, REAPER at 181), the rule Stage 10's
    discovery_candidates learned. An unheard seed is itself a candidate,
    ranked above every neighbour; a library seed only feeds neighbours (library
    artists are anchors, never discovery). Excluded names never enter the pool.
    Everything then clears _serving_mbids — the same bar as every stranger.
    """
    excl = {normalise(n) for n in spec.get("exclude", [])}
    seeds = [r for s in spec.get("seeds", [])
             if (r := resolve_seed(con, http, s, stage2, mine, overrides))]
    if not seeds:
        return []
    top = float(len(seeds)) + 1.0     # above any pooled sum (each seed adds <= 1)
    pooled: dict[str, dict] = {}
    for seed in seeds:
        key = normalise(seed["artist_name"])
        if key not in known and key not in excl:
            pooled[seed["mbid"]] = {"artist_name": seed["artist_name"],
                                    "mbid": seed["mbid"], "score": top}
    for seed in seeds:
        similar = fetch_similar(http, seed["mbid"], sim_cache)
        if not similar:
            print(f"  ⚠ ListenBrainz has no neighbours for seed "
                  f"{seed['artist_name']!r} — it adds nothing this run")
            continue
        best = max(float(x.get("score") or 0) for x in similar) or 1.0
        for sim in similar:
            key = normalise(sim.get("name") or "")
            if not sim.get("mbid") or key in known or key in excl:
                continue
            row = pooled.setdefault(sim["mbid"], {
                "artist_name": sim["name"], "mbid": sim["mbid"], "score": 0.0})
            row["score"] += float(sim.get("score") or 0) / best
    ranked = sorted(pooled.values(),
                    key=lambda r: (-r["score"], r["mbid"]))[:config.PLAYLIST_SEED_CANDIDATES]
    for cand in ranked:
        fetch_candidate_tags(http, cand["mbid"], vocab, tag_cache)
    serving = _serving_mbids(con, [c["mbid"] for c in ranked], spec["tags"], tag_cache)
    refused = [s["artist_name"] for s in seeds
               if s["mbid"] in pooled and s["mbid"] not in serving]
    if refused:
        print(f"  ⚠ seed(s) {', '.join(refused)} do not serve "
              f"{' + '.join(spec['tags'])} by MusicBrainz's tags — not offered")
    return [c for c in ranked if c["mbid"] in serving]


def build_selections(con, http, sp) -> list[dict]:
    """Everything up to (but excluding) the Spotify writes; shared by both
    modes so --dry-run previews exactly what a live run would do.

    Per candidate artist this spends one Spotify search (relevance order, the
    surviving popularity signal, and who is credited on each hit) and, only if
    one of the candidate's own records survives, one MusicBrainz search (which
    of their recordings carry the gap genre). Both are cached append-only, and
    the Spotify cache is shared with Stage 10, so a re-run inside the same
    quarter spends nothing.

    Per candidate: pin their Spotify id (a namesake is not them), keep what
    they lead or remixed (candidate_led — a feature is not their record), drop
    live recordings and other people's remixes (discovery_eligible), then
    choose_tracks. Every filter keeps relevance order.

    A quota lockout stops the SEARCHING, not the run. A failed search is never
    cached, so without a stop every uncached candidate was asked again — two
    requests and a sleep each, in every playlist — and each ask pushed the
    lockout further out. Once the client reports a 429 that outlived its retry
    (Spotify.rate_limited), only candidates the shared cache already answers
    are considered. A single 5xx or network error does not stop anything: that
    is one request failing, not the app being locked out.

    Seeded candidates (seed_candidates) go first — they are the human's
    statement — then Stage 5's, deduped on MBID.
    """
    tag_cache = load_jsonl(CANDIDATE_TAG_CACHE, "mbid")
    genre_rec_cache = load_jsonl(GENRE_RECORDINGS_CACHE, "key")
    credited_cache = load_jsonl(SP_TRACKS_CREDITED_CACHE, "key")

    out = []
    specs = load_playlist_specs(con)
    sim_cache = load_jsonl(SIMILAR_CACHE, "seed_mbid")
    seed_cache = load_jsonl(PLAYLIST_SEED_CACHE, "artist_name")
    stage2 = load_cache()
    # The genre vocabulary and artist_overrides.csv are read only when a seed
    # needs them: the vocabulary is one cached file (fetched once), and the
    # override file is personal data that a seedless run — and every existing
    # test, which never points SPOTIFY_ARTIST_OVERRIDES at a scratch dir — has
    # no business opening.
    seeded_any = any(s.get("seeds") for s in specs)
    vocab = load_genre_vocabulary(http) if seeded_any else set()
    overrides = load_overrides() if seeded_any else {}
    known = {normalise(r[0]) for r in con.execute(
        "SELECT DISTINCT artist_name FROM plays WHERE artist_name IS NOT NULL"
    ).fetchall()}
    for spec, anchors in zip(specs, select_run_anchors(con, specs)):
        tags = spec["tags"]
        blend = "" if tags == [spec["label"]] else f"  [{' + '.join(tags)}]"
        print(f"\n{pretty(spec['label'])}"
              f"{' (pinned)' if spec['pinned'] else ''}{blend}")
        print(f"  {len(anchors)} anchors from your own listening")

        seen_uris = {a["spotify_track_uri"] for a in anchors}
        seeded = seed_candidates(con, http, spec, sim_cache=sim_cache,
                                 tag_cache=tag_cache, stage2=stage2,
                                 mine=seed_cache, vocab=vocab, known=known,
                                 overrides=overrides)
        have = {c["mbid"] for c in seeded}
        candidates = seeded + [c for c in select_candidates(
            con, tags, tag_cache, spec.get("exclude", ())) if c["mbid"] not in have]
        print(f"  {len(candidates)} candidate artists carry these genres "
              f"({len(seeded)} from your seeds)")

        discovery: list[dict] = []
        for cand in candidates:
            if len(discovery) >= config.PLAYLIST_SIZE:   # enough material
                break
            name = cand["artist_name"]
            locked = getattr(sp, "rate_limited", False)
            if locked and normalise(name) not in credited_cache:
                continue                       # a cached answer is still free
            hits = sp_artist_tracks_credited(sp, name, credited_cache)
            if not locked and getattr(sp, "rate_limited", False):
                print("  ⚠ Spotify still answers 429 after a retry — no more "
                      "searches this run, cached artists only. Re-run once "
                      "the quota has recovered.")
            pinned_id, tracks = pin_artist_id(hits, name)
            tracks = drop_excluded(
                discovery_eligible(candidate_led(tracks, name, pinned_id), name),
                spec.get("exclude", ()))
            if not tracks:
                continue
            on_genre = mb_genre_recordings(http, cand["mbid"], tags, genre_rec_cache)
            for chosen in choose_tracks(tracks, on_genre, config.TRACKS_PER_ARTIST):
                if chosen["spotify_track_uri"] in seen_uris:
                    continue
                seen_uris.add(chosen["spotify_track_uri"])
                discovery.append(chosen)

        matched = sum(1 for d in discovery if d.get("genre_matched"))
        print(f"  {len(discovery)} discovery tracks "
              f"({matched} matched on recording-level tags)")
        out.append({"spec": spec,
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

    state = load_state()
    archive_rows = []
    for sel in selections:
        spec = sel["spec"]
        tag = spec["label"]
        name = config.PLAYLIST_NAME_TEMPLATE.format(genre=pretty(tag))
        if not sel["tracks"]:
            print(f"  ⚠ nothing selected for '{name}' — leaving it untouched")
            continue
        pid = ensure_playlist(sp, tag, name, state)

        # Snapshot BEFORE the replace, so nothing we overwrite goes unrecorded.
        for i, old in enumerate(playlist_items(sp, pid)):
            archive_rows.append({
                "run_date": today, "kind": "pre_replace_snapshot", "gap_tag": tag,
                "playlist_id": pid, "position": i, "slot": None,
                "artist_name": old["artist_name"], "track_name": old["track_name"],
                "spotify_track_uri": old["uri"], "source": "spotify",
            })

        uris = [t["spotify_track_uri"] for t in sel["tracks"]]
        resp = sp.put(f"/playlists/{pid}/items", json={"uris": uris})
        if not isinstance(resp, dict) or "_status" in resp:
            print(f"  ⚠ replace failed for '{name}': {resp} — skipping")
            if isinstance(resp, dict) and resp.get("_status") == 403:
                raise SystemExit(FORBIDDEN_NOTE)
            continue
        template = (config.PLAYLIST_DESCRIPTION_PINNED_TEMPLATE if spec["pinned"]
                    else config.PLAYLIST_DESCRIPTION_TEMPLATE)
        sp.put(f"/playlists/{pid}", json={
            "name": name,
            "description": template.format(genre=pretty(tag), date=today),
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
        spec = sel["spec"]
        g = spec.get("gap")
        n_anchor = sum(1 for t in sel["tracks"] if t["slot"] == "anchor")
        n_disc = len(sel["tracks"]) - n_anchor
        n_matched = sum(1 for t in sel["tracks"] if t.get("genre_matched"))
        print(f"\n{config.PLAYLIST_NAME_TEMPLATE.format(genre=pretty(spec['label']))}")
        if spec["tags"] != [spec["label"]]:
            print(f"  blend: {' + '.join(spec['tags'])}")
        if g:
            print(f"  gap: {g['n_artists']} artists, {g['hours']:.0f} h, "
                  f"{100 * g['rel_change_per_year']:+.0f}%/yr")
        else:
            print("  pinned: your choice, not a trend finding")
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
