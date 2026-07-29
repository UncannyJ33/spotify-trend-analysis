"""Stage 5 — recommend artists based on where taste is HEADING, not where it has been.

    .venv/bin/python recommend.py
    .venv/bin/python recommend.py --lambda 0   # score against current taste only
    .venv/bin/python recommend.py --report     # re-rank from cache, no network

An ordinary recommender scores candidates against your listening history, which
means it recommends the past back to you. This history is 30% hip hop and that
share is falling hard — matching it would push precisely the direction the
listening is leaving.

So candidates are scored against a *trajectory-weighted* taste vector:

    weight(genre) = current_share x (1 + LAMBDA x relative_annual_change)

A genre climbing 90% a year is worth roughly twice its current share; one in
free-fall is discounted toward zero. `--lambda 0` collapses this back to plain
current-taste scoring, which is the honest baseline to compare against.

Pipeline:
  1. Seed on artists from the last SEED_WINDOW_MONTHS, weighted by listening time.
  2. Ask ListenBrainz for similar artists per seed (collaborative filtering over
     real listening sessions, no auth). Spotify's equivalent endpoints are gone —
     /v1/recommendations now 404s and related-artists 403s.
  3. Drop anyone already in the history, by MBID and by normalised name.
  4. Tag the strongest survivors via MusicBrainz.
  5. Score = similarity^ALPHA x trajectory_fit^BETA, and explain each result.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict

import duckdb
import pandas as pd

import config
# Reuse the throttle, name folding and genre vocabulary rather than growing a
# second copy of each.
from enrich import (
    MB_MIN_INTERVAL,
    Throttled,
    load_genre_vocabulary,
    normalise,
)

LB_SIMILAR_URL = "https://labs.api.listenbrainz.org/similar-artists/json"
LB_ALGORITHM = (
    "session_based_days_9000_session_300_contribution_5_threshold_15_limit_50_skip_30"
)
MB_ARTIST_URL = "https://musicbrainz.org/ws/2/artist"

SIMILAR_CACHE = config.CACHE_DIR / "similar_artists.jsonl"
CANDIDATE_TAG_CACHE = config.CACHE_DIR / "candidate_tags.jsonl"


# --------------------------------------------------------------------------
# Cache helpers (append-only JSONL, last write wins, same shape as Stage 2)
# --------------------------------------------------------------------------


def load_jsonl(path, key: str) -> dict:
    if not path.exists():
        return {}
    out = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            out[rec[key]] = rec
    return out


def append_jsonl(path, rec: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


# --------------------------------------------------------------------------
# The taste vector
# --------------------------------------------------------------------------


def build_taste_vector(con: duckdb.DuckDBPyConnection, lam: float) -> dict[str, float]:
    """Genre -> desirability, tilted toward what is climbing.

    Built from the trailing month of `tag_trends`, so it reflects current
    listening rather than a seven-year average.
    """
    lo, hi = config.TRAJECTORY_CLAMP
    rows = con.execute(
        f"""
        SELECT tag, smoothed_share, coalesce(rel_change_per_year, 0) AS rel
        FROM tag_trends
        WHERE variant = '{config.DEFAULT_VARIANT}'
          AND month = (SELECT max(month) FROM tag_trends)
          AND smoothed_share > 0
        """
    ).fetchall()

    vec: dict[str, float] = {}
    for tag, share, rel in rows:
        rel = max(lo, min(hi, float(rel or 0.0)))
        w = float(share) * (1.0 + lam * rel)
        if w > 0:
            vec[tag] = w
    return normalise_vector(vec)


def normalise_vector(v: dict[str, float]) -> dict[str, float]:
    """Unit-length, so cosine similarity is a plain dot product."""
    norm = math.sqrt(sum(x * x for x in v.values()))
    return {k: x / norm for k, x in v.items()} if norm else {}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(w * b.get(k, 0.0) for k, w in a.items())


# --------------------------------------------------------------------------
# Seeds and candidates
# --------------------------------------------------------------------------


def build_seeds(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str, float]]:
    """(artist_name, mbid, weight) for recent, resolved, well-listened artists."""
    rows = con.execute(
        f"""
        WITH recent AS (
            SELECT c.artist_name, sum(c.played_seconds) AS secs
            FROM track_credits c
            JOIN plays p USING (spotify_track_uri)
            WHERE p.month >= (
                SELECT max(month) - INTERVAL '{config.SEED_WINDOW_MONTHS} months'
                FROM plays)
            GROUP BY 1
        )
        SELECT r.artist_name, a.mbid, r.secs
        FROM recent r
        JOIN artist_resolution a USING (artist_name)
        WHERE a.mbid IS NOT NULL AND a.status = 'resolved'
        ORDER BY r.secs DESC
        LIMIT {config.N_SEEDS}
        """
    ).fetchall()
    total = sum(r[2] for r in rows) or 1.0
    return [(name, mbid, secs / total) for name, mbid, secs in rows]


def fetch_similar(http: Throttled, mbid: str, cache: dict) -> list[dict]:
    """Similar artists for one seed, cached so re-runs cost nothing."""
    if mbid in cache:
        return cache[mbid]["similar"]
    r = http.get(
        LB_SIMILAR_URL, params={"artist_mbids": mbid, "algorithm": LB_ALGORITHM}
    )
    similar = []
    if r is not None and r.status_code == 200:
        try:
            payload = r.json()
        except ValueError:
            payload = []
        rows = payload if isinstance(payload, list) else payload.get("data", [])
        similar = [
            {"mbid": x["artist_mbid"], "name": x.get("name") or "",
             "score": float(x.get("score") or 0),
             "comment": x.get("comment") or ""}
            for x in rows if x.get("artist_mbid")
        ]
    rec = {"seed_mbid": mbid, "similar": similar}
    append_jsonl(SIMILAR_CACHE, rec)
    cache[mbid] = rec
    return similar


def fetch_candidate_tags(http: Throttled, mbid: str, vocab: set[str],
                         cache: dict) -> list[dict]:
    """Genre vector for a candidate.

    `inc=genres` returns MusicBrainz's curated genre list directly; raw tags are
    the fallback, filtered against the same vocabulary Stage 2 uses so both
    sides of the comparison live in one namespace.
    """
    if mbid in cache:
        return cache[mbid]["tags"]
    r = http.get(f"{MB_ARTIST_URL}/{mbid}",
                 params={"inc": "tags+genres", "fmt": "json"})
    tags: list[dict] = []
    if r is not None and r.status_code == 200:
        d = r.json()
        genres = d.get("genres") or []
        if genres:
            tags = [{"tag": g["name"].casefold(), "count": max(g.get("count") or 0, 0)}
                    for g in genres if g.get("name")]
        else:
            tags = [{"tag": t["name"].casefold(), "count": max(t.get("count") or 0, 0)}
                    for t in (d.get("tags") or [])
                    if t.get("name") and (not vocab or t["name"].casefold() in vocab)]
    rec = {"mbid": mbid, "tags": tags}
    append_jsonl(CANDIDATE_TAG_CACHE, rec)
    cache[mbid] = rec
    return tags


def tags_to_vector(tags: list[dict]) -> dict[str, float]:
    """Same construction as the listening side: vote-weighted, capped, clamped."""
    top = sorted(tags, key=lambda t: -t["count"])[: config.TOP_N_TAGS_PER_ARTIST]
    return normalise_vector({t["tag"]: max(t["count"], 0) + 1.0 for t in top})


def score_candidates(shortlist: list[tuple[str, dict]], tag_cache: dict,
                     taste: dict[str, float]) -> list[dict]:
    """Rank candidates against a taste vector. Pure and offline.

    Split out from the pipeline so the dashboard can re-rank at a different
    lambda instantly — every input is already on disk, so moving the dial costs
    no network calls at all.
    """
    max_sim = max((d["score"] for _, d in shortlist), default=1.0) or 1.0
    results = []
    for mbid, d in shortlist:
        tags = tag_cache.get(mbid, {}).get("tags", [])
        if not tags:
            continue
        cvec = tags_to_vector(tags)
        fit = cosine(cvec, taste)
        if fit <= 0:
            continue
        sim_norm = d["score"] / max_sim
        final = (sim_norm ** config.SIMILARITY_ALPHA) * (fit ** config.TRAJECTORY_BETA)
        why = sorted(
            ((t, w * taste.get(t, 0.0)) for t, w in cvec.items() if t in taste),
            key=lambda kv: -kv[1])[:3]
        top_seeds = sorted(d["seeds"], key=lambda kv: -kv[1])[:3]
        results.append({
            "artist_name": d["name"], "mbid": mbid, "comment": d["comment"],
            "score": final, "similarity": sim_norm, "trajectory_fit": fit,
            "matched_genres": ", ".join(t for t, _ in why),
            "via_artists": ", ".join(s for s, _ in top_seeds),
            "n_seeds": len(d["seeds"]),
        })
    results.sort(key=lambda r: -r["score"])
    return results


def rank_from_cache(con: duckdb.DuckDBPyConnection, lam: float) -> list[dict]:
    """Full re-rank at an arbitrary lambda using only cached data."""
    taste = build_taste_vector(con, lam)
    seeds = build_seeds(con)
    known_mbids = {r[0] for r in con.execute(
        "SELECT mbid FROM artist_resolution WHERE mbid IS NOT NULL").fetchall()}
    known_names = {normalise(r[0]) for r in con.execute(
        "SELECT DISTINCT artist_name FROM track_credits").fetchall()}

    sim_cache = load_jsonl(SIMILAR_CACHE, "seed_mbid")
    agg: dict[str, dict] = defaultdict(
        lambda: {"score": 0.0, "name": "", "seeds": [], "comment": ""})
    for name, mbid, weight in seeds:
        for cand in sim_cache.get(mbid, {}).get("similar", []):
            if cand["mbid"] in known_mbids or normalise(cand["name"]) in known_names:
                continue
            a = agg[cand["mbid"]]
            a["score"] += cand["score"] * weight
            a["name"] = cand["name"]
            a["comment"] = cand["comment"]
            a["seeds"].append((name, cand["score"]))

    shortlist = sorted(agg.items(), key=lambda kv: -kv[1]["score"])[
        : config.MAX_CANDIDATES_TO_TAG]
    return score_candidates(shortlist, load_jsonl(CANDIDATE_TAG_CACHE, "mbid"), taste)


# --------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Stage 5 trajectory-aware recommendations")
    ap.add_argument("--lambda", dest="lam", type=float, default=config.TRAJECTORY_LAMBDA,
                    help="trajectory emphasis; 0 scores against current taste only")
    ap.add_argument("--top", type=int, default=40, help="how many to print")
    ap.add_argument("--report", action="store_true",
                    help="re-rank from cache without any network calls")
    args = ap.parse_args()

    config.ensure_dirs()
    con = duckdb.connect()
    for name, path in (
        ("plays", config.PLAYS_PARQUET),
        ("track_credits", config.DATA_DIR / "track_credits.parquet"),
        ("artist_resolution", config.DATA_DIR / "artist_resolution.parquet"),
        ("tag_trends", config.TAG_TRENDS_PARQUET),
    ):
        if not path.exists():
            raise SystemExit(f"{path} not found — run the earlier stages first.")
        con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM '{path}'")

    http = Throttled(MB_MIN_INTERVAL)
    vocab = load_genre_vocabulary(http) if not args.report else set()

    taste = build_taste_vector(con, args.lam)
    print(f"\nTaste vector: {len(taste)} genres  (lambda = {args.lam})")
    print("  strongest pulls: " + ", ".join(
        f"{t} {100*w:.0f}" for t, w in
        sorted(taste.items(), key=lambda kv: -kv[1])[:8]))

    seeds = build_seeds(con)
    print(f"\nSeeds: {len(seeds)} artists from the last "
          f"{config.SEED_WINDOW_MONTHS} months")

    # Everything already listened to, by MBID and by folded name, so a
    # candidate cannot slip back in under a different spelling.
    known_mbids = {r[0] for r in con.execute(
        "SELECT mbid FROM artist_resolution WHERE mbid IS NOT NULL").fetchall()}
    known_names = {normalise(r[0]) for r in con.execute(
        "SELECT DISTINCT artist_name FROM track_credits").fetchall()}

    sim_cache = load_jsonl(SIMILAR_CACHE, "seed_mbid")
    todo = [s for s in seeds if s[1] not in sim_cache]
    if todo and not args.report:
        print(f"Querying ListenBrainz for {len(todo)} seeds "
              f"(~{len(todo)*MB_MIN_INTERVAL/60:.0f} min) ...")
    for i, (name, mbid, _) in enumerate(seeds, 1):
        if args.report and mbid not in sim_cache:
            continue
        fetch_similar(http, mbid, sim_cache)
        if i % 25 == 0:
            print(f"  [{i}/{len(seeds)}]", flush=True)

    # Aggregate: a candidate surfacing from several well-listened seeds is a
    # stronger signal than one scraped from a single seed.
    agg: dict[str, dict] = defaultdict(
        lambda: {"score": 0.0, "name": "", "seeds": [], "comment": ""})
    for name, mbid, weight in seeds:
        for cand in sim_cache.get(mbid, {}).get("similar", []):
            if cand["mbid"] in known_mbids or normalise(cand["name"]) in known_names:
                continue
            a = agg[cand["mbid"]]
            a["score"] += cand["score"] * weight
            a["name"] = cand["name"]
            a["comment"] = cand["comment"]
            a["seeds"].append((name, cand["score"]))

    print(f"Candidates after removing artists already listened to: {len(agg):,}")
    ranked = sorted(agg.items(), key=lambda kv: -kv[1]["score"])
    shortlist = ranked[: config.MAX_CANDIDATES_TO_TAG]

    tag_cache = load_jsonl(CANDIDATE_TAG_CACHE, "mbid")
    need = [m for m, _ in shortlist if m not in tag_cache]
    if need and not args.report:
        print(f"Tagging top {len(shortlist)} candidates "
              f"({len(need)} uncached, ~{len(need)*MB_MIN_INTERVAL/60:.0f} min) ...")
    for i, (mbid, _) in enumerate(shortlist, 1):
        if args.report and mbid not in tag_cache:
            continue
        fetch_candidate_tags(http, mbid, vocab, tag_cache)
        if i % 50 == 0:
            print(f"  [{i}/{len(shortlist)}]", flush=True)

    results = score_candidates(shortlist, tag_cache, taste)

    if not results:
        raise SystemExit("No recommendations produced — check the caches.")

    results_df = pd.DataFrame(results)
    con.register("results_df", results_df)
    con.execute("CREATE OR REPLACE TABLE recommendations AS SELECT * FROM results_df")
    con.execute(
        f"COPY (SELECT * FROM recommendations ORDER BY score DESC) "
        f"TO '{config.RECOMMENDATIONS_PARQUET}' (FORMAT PARQUET, COMPRESSION ZSTD)")

    print()
    print("=" * 86)
    print(f"STAGE 5 — RECOMMENDATIONS  (lambda = {args.lam})")
    print("=" * 86)
    print(f"{'#':>3}  {'artist':<26} {'score':>6} {'fit':>5} {'sim':>5}  genres / via")
    print("-" * 86)
    for i, r in enumerate(results[: args.top], 1):
        note = f" [{r['comment']}]" if r["comment"] else ""
        print(f"{i:>3}. {r['artist_name'][:26]:<26} {r['score']:>6.3f} "
              f"{r['trajectory_fit']:>5.2f} {r['similarity']:>5.2f}  "
              f"{r['matched_genres'][:38]}{note}")
        print(f"     {'':<26} {'':>6} {'':>5} {'':>5}  via {r['via_artists'][:52]}")
    print("=" * 86)
    print(f"\nWrote {config.RECOMMENDATIONS_PARQUET}  ({len(results):,} scored)")


if __name__ == "__main__":
    main()
