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

Outputs: two playlists, data/running_state.json, rows in data/playlists.parquet
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import date

import duckdb

import config
from consolidate import gentle_token
from enrich import MB_MIN_INTERVAL, Throttled, load_genre_vocabulary, normalise
from playlists import (
    ARTIST_TRACKS_CACHE,
    FORBIDDEN_NOTE,
    GENRE_RECORDINGS_CACHE,
    SP_API,
    Spotify,
    _title_key,
    choose_tracks,
    ensure_playlist,
    mb_genre_recordings,
    playlist_items,
    select_candidates,
    sp_artist_tracks,
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

# The two clusters. `key` names the weight column; `label` names the playlist.
CLUSTERS = (
    {"key": "garage", "label": "speed garage", "tags": config.RUN_GARAGE_TAGS},
    {"key": "bass", "label": "dubstep", "tags": config.RUN_BASS_TAGS},
)


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
    """Tracks from the listener's own recent history, per cluster.

    The join is on track_credits, NOT on plays.artist_name, so a track counts
    for every artist actually on it — album artist, feature and remixer alike.
    That is the whole reason Stage 1b learned to parse `- X Remix`: without it
    a Halsey track remixed by Ian Asher is judged on Halsey's tags.

    Duration comes from the longest COMPLETED play. A play that ended in
    'trackdone' ran the track to its end, so its ms_played is the duration —
    exact, free, and it saves a request per track against an endpoint whose
    batch form answers 403.

    The per-artist cap is keyed on the ALBUM artist. A remixer who also has
    their own releases is capped on those separately, which is why Blair Muir
    can bring REHAB and Disturbia while his Luude remix counts against Luude.
    """
    con.execute(
        f"""
        CREATE OR REPLACE TABLE known_pool AS
        WITH recent AS (
            SELECT
                p.spotify_track_uri,
                any_value(p.track_name)  AS track_name,
                any_value(p.artist_name) AS album_artist,
                count(*)                 AS n_plays,
                sum(p.played_seconds) / 3600.0 AS hours,
                avg(CASE WHEN p.reason_end = 'trackdone' THEN 1.0 ELSE 0 END)
                    AS done_rate,
                -- MEDIAN, not max. A completed play's ms_played is the track's
                -- duration, but the odd play reports far more than the track
                -- runs (a paused stream that kept counting). max() took
                -- SLANDER's "Wish I Could Forget" to 9.6 minutes and let one
                -- track eat a tenth of the playlist.
                median(CASE WHEN p.reason_end = 'trackdone' THEN p.ms_played END)
                    AS duration_ms
            FROM plays p
            WHERE p.spotify_track_uri IS NOT NULL
              AND p.track_name IS NOT NULL
              AND p.month >= (SELECT max(month) FROM plays)
                             - INTERVAL {config.RUN_WINDOW_MONTHS} MONTH
            GROUP BY 1
        ),
        credited AS (
            SELECT r.*, ac.cluster,
                   -- One row per (track, cluster): a track crediting three
                   -- qualifying artists is one track, not three.
                   row_number() OVER (
                       PARTITION BY r.spotify_track_uri, ac.cluster
                       ORDER BY ac.garage_w + ac.bass_w DESC, c.artist_name
                   ) AS rn
            FROM recent r
            JOIN track_credits c USING (spotify_track_uri)
            JOIN artist_clusters ac ON ac.artist_name = c.artist_name
            WHERE ac.cluster IS NOT NULL
        )
        SELECT
            spotify_track_uri, track_name, album_artist, cluster,
            n_plays, hours, done_rate, duration_ms,
            -- Laplace-smoothed completion. A track played once and finished is
            -- not evidence of the same strength as one finished forty times,
            -- and (done+1)/(n+2) says so without discarding the single play.
            (done_rate * n_plays + 1) / (n_plays + 2) AS done_smoothed,
            hours * ((done_rate * n_plays + 1) / (n_plays + 2)) AS score
        FROM credited
        WHERE rn = 1
          -- A track skipped repeatedly is not a track that carries a run.
          -- Applied only where there is enough evidence to mean anything.
          AND NOT (n_plays >= 3 AND done_rate < {config.RUN_MIN_TRACKDONE_RATE})
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


def cluster_candidates(con, http, label: str, tag_cache: dict,
                       sim_cache: dict, vocab: set[str]) -> list[dict]:
    """Discovery candidates seeded on THIS cluster's own top artists.

    Stage 5's recommendations.parquet is seeded across the whole library, so its
    electronic candidates are whatever sits near a taste that also contains
    hip-hop and rock — canonical rather than current. It offered Basement Jaxx,
    Busy P and Mr. Oizo as speed garage, and The Prodigy as dubstep. Asking
    ListenBrainz "who is like Blair Muir, like NOTION, like Ian Asher" returns
    the right neighbourhood instead.

    Both lookups are the cached, append-only kind the rest of the project uses,
    so a re-run inside the same quarter spends nothing.
    """
    seeds = con.execute(
        """
        SELECT t.artist_name, any_value(t.mbid) AS mbid, sum(k.score) AS score
        FROM known_pool k
        JOIN track_credits c USING (spotify_track_uri)
        JOIN artist_tags t ON t.artist_name = c.artist_name
        JOIN artist_clusters ac ON ac.artist_name = t.artist_name
        WHERE k.cluster = ? AND ac.cluster = ? AND t.mbid IS NOT NULL
        GROUP BY t.artist_name
        ORDER BY score DESC
        LIMIT ?
        """,
        [label, label, config.RUN_DISCOVERY_SEEDS],
    ).fetchall()
    if not seeds:
        return []
    print(f"  seeding discovery on {len(seeds)} of your own artists: "
          f"{', '.join(s[0] for s in seeds[:5])}...")

    known_names = {normalise(r[0]) for r in con.execute(
        "SELECT DISTINCT artist_name FROM plays WHERE artist_name IS NOT NULL"
    ).fetchall()}

    pooled: dict[str, dict] = {}
    for name, mbid, seed_score in seeds:
        for sim in fetch_similar(http, mbid, sim_cache):
            if normalise(sim["name"]) in known_names or not sim["mbid"]:
                continue
            row = pooled.setdefault(sim["mbid"], {
                "artist_name": sim["name"], "mbid": sim["mbid"], "score": 0.0})
            # Similar to several of your cluster artists beats similar to one.
            row["score"] += float(sim.get("score") or 0)

    ranked = sorted(pooled.values(), key=lambda r: -r["score"])
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
# Duration — fill to time, not to a track count
# --------------------------------------------------------------------------


def track_duration(sp, uri: str, cache: dict) -> int | None:
    """Duration for a track with no listening history. One request each:
    the batch /tracks form answers 403 like every other batch endpoint."""
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
    needed = {
        "plays": config.PLAYS_PARQUET,
        "artist_tags": config.ARTIST_TAGS_PARQUET,
        "track_credits": config.DATA_DIR / "track_credits.parquet",
        "recommendations": config.RECOMMENDATIONS_PARQUET,
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
    artist_tracks_cache = load_jsonl(ARTIST_TRACKS_CACHE, "key")
    duration_cache = load_jsonl(DURATION_CACHE, "key")
    sim_cache = load_jsonl(SIMILAR_CACHE, "seed_mbid")
    vocab = load_genre_vocabulary(http)

    pins, vetoes = load_overrides()
    target_ms = config.RUN_TARGET_MINUTES * 60_000
    known_ms = int(target_ms * config.RUN_KNOWN_FRACTION)

    # Shared across BOTH playlists. These are meant to be compared on real
    # runs, so a track — or an artist — appearing in both makes the comparison
    # say less. The first dry run put The Prodigy and Breathe Carolina in each.
    used_uris: set[str] = set()
    used_songs: set[str] = set()
    used_discovery_artists: set[str] = set()

    def fresh(row: dict) -> bool:
        """Not already placed, in this playlist or the other one.

        Song identity is the FOLDED title, not the URI: Spotify presses the
        album cut, the single and the extended mix as distinct URIs, and the
        first dry run duly listed jigitz's 'tell you straight' twice in one
        playlist.
        """
        uri = row.get("spotify_track_uri")
        song = (normalise(row.get("artist_name") or ""),
                _title_key(row.get("track_name") or ""))
        return uri not in used_uris and song not in used_songs

    def place(row: dict) -> dict:
        used_uris.add(row.get("spotify_track_uri"))
        used_songs.add((normalise(row.get("artist_name") or ""),
                        _title_key(row.get("track_name") or "")))
        return row

    out = []
    for cluster in CLUSTERS:
        label, tags = cluster["label"], list(cluster["tags"])
        print(f"\n{pretty(label)} run")

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
                used_uris.discard(r["spotify_track_uri"])
                used_songs.discard((normalise(r.get("artist_name") or ""),
                                    _title_key(r.get("track_name") or "")))
        print(f"  {len(known)} known tracks "
              f"({sum(1 for k in known if k.get('pinned'))} pinned), "
              f"{sum(k['duration_ms'] for k in known)/60000:.0f} min")

        # Cluster-seeded candidates first; Stage 5's library-wide list is the
        # top-up when ListenBrainz is thin. Both must clear the same intensity
        # bar a library artist clears.
        candidates = cluster_candidates(con, http, label, tag_cache,
                                        sim_cache, vocab)
        seeded = len(candidates)
        have = {c["mbid"] for c in candidates}
        for c in select_candidates(con, tags, tag_cache):
            if c["mbid"] in have:
                continue
            cl, share = classify_tags(
                (tag_cache.get(c["mbid"]) or {}).get("tags", []),
                min_weight=config.RUN_MIN_CANDIDATE_CLUSTER_WEIGHT,
                min_share=config.RUN_MIN_CANDIDATE_SHARE)
            if cl == label:
                candidates.append(dict(c, share=share))
        print(f"  {len(candidates)} candidates clear the "
              f"{config.RUN_MIN_CANDIDATE_SHARE:.2f} stranger bar "
              f"({seeded} from your own artists' neighbours)")

        discovery: list[dict] = []
        budget_ms = target_ms - sum(k["duration_ms"] for k in known)
        got_ms = 0
        for cand in candidates:
            if got_ms >= budget_ms:
                break
            if normalise(cand["artist_name"]) in used_discovery_artists:
                continue
            tracks = sp_artist_tracks(sp, cand["artist_name"], artist_tracks_cache)
            if not tracks:
                continue
            on_genre = mb_genre_recordings(http, cand["mbid"], tags, genre_rec_cache)
            for chosen in choose_tracks(tracks, on_genre,
                                        config.RUN_DISCOVERY_TRACKS_PER_ARTIST):
                if (not fresh(chosen) or vetoed(chosen, vetoes)
                        or is_live(chosen.get("track_name", ""))):
                    continue
                dur = track_duration(sp, chosen["spotify_track_uri"], duration_cache)
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
          AND p.month >= (SELECT max(month) FROM plays)
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
