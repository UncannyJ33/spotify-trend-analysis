"""Stage 2 — resolve artists to MusicBrainz and attach a tag vector to each.

    .venv/bin/python enrich.py              # resume; only touches new artists
    .venv/bin/python enrich.py --limit 50   # short trial run
    .venv/bin/python enrich.py --report     # re-print the summary, no network

Design notes, because this stage deviates from the original plan:

The spec routed tags through ListenBrainz's `bulk-tag-lookup`. That endpoint is
keyed on *recording* MBIDs, not artist MBIDs, so it cannot consume an artist
resolution — feeding it would mean resolving all 8k tracks to recording MBIDs
first, which is the per-track explosion the spec set out to avoid.

MusicBrainz's artist *search* turns out to return tags with counts inline, so a
single request per artist yields both the MBID and the tag vector. The tags are
then filtered against MusicBrainz's canonical genre vocabulary (~2,180 terms,
fetched once) which strips the noise search returns alongside real genres —
'usa', 'english', '2010s', 'gen z' go; 'trap', 'emo rap', 'cloud rap' stay.

Everything is cached to `.cache/artist_resolution.jsonl`, appended one line at a
time, so a run that dies half way resumes exactly where it stopped and a
quarterly re-run only spends requests on artists it has never seen.

Ambiguous names are NOT guessed at. A name resolves only on an exact normalised
match against the MusicBrainz name or one of its aliases; anything else lands on
a review list.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import duckdb
import requests

import config

MB_SEARCH_URL = "https://musicbrainz.org/ws/2/artist"
MB_GENRES_URL = "https://musicbrainz.org/ws/2/genre/all"
MB_RELEASE_GROUP_URL = "https://musicbrainz.org/ws/2/release-group"

# MusicBrainz asks for a descriptive agent with contact details and throttles
# anonymous clients to one request per second. Exceed it and they block you.
USER_AGENT = (
    "spotify-trend-analysis/0.1 "
    "( https://github.com/UncannyJ33/spotify-trend-analysis )"
)
MB_MIN_INTERVAL = 1.1  # seconds between MusicBrainz requests, with headroom

# An override row must carry a real MBID or the literal IGNORE. Anything else is
# a typo, and a typo'd MBID would otherwise be pinned as gospel.
MBID_RE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")

CACHE_FILE = config.CACHE_DIR / "artist_resolution.jsonl"
GENRE_VOCAB_FILE = config.CACHE_DIR / "mb_genre_vocabulary.txt"
REVIEW_PARQUET = config.DATA_DIR / "artist_review.parquet"
RESOLUTION_PARQUET = config.DATA_DIR / "artist_resolution.parquet"


# --------------------------------------------------------------------------
# Name normalisation
# --------------------------------------------------------------------------

# Stylised substitutions that are cosmetic rather than distinguishing, so
# "A$AP Rocky" and "ASAP Rocky" collapse to the same key.
_SUBSTITUTIONS = {"$": "s", "€": "e", "£": "l", "@": "a", "!": "i", "0": "o"}


def normalise(name: str) -> str:
    """Fold a name to a comparison key: accents, case and punctuation removed."""
    if not name:
        return ""
    name = "".join(_SUBSTITUTIONS.get(ch, ch) for ch in name)
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.casefold()
    name = re.sub(r"\b(?:the|and)\b", "", name)
    return re.sub(r"[^a-z0-9]+", "", name)


# --------------------------------------------------------------------------
# Throttled session
# --------------------------------------------------------------------------


class Throttled:
    """A requests session that never exceeds one call per `interval` seconds."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._last = 0.0
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def get(self, url: str, **kw) -> requests.Response | None:
        for attempt in range(4):
            wait = self.interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.session.get(url, timeout=30, **kw)
            except requests.RequestException as exc:
                print(f"    network error ({exc.__class__.__name__}), retrying", flush=True)
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 200:
                return r
            if r.status_code in (429, 503):
                # Being rate limited. Back off hard rather than hammering.
                # MusicBrainz sends `Retry-After: 0` on a 503, so trusting the
                # header alone means retrying with no backoff at all — exactly
                # the behaviour that gets a client blocked. Floor it.
                try:
                    hinted = float(r.headers.get("Retry-After", 0))
                except ValueError:
                    hinted = 0.0
                back = max(hinted, 2.0 * (attempt + 1))
                print(f"    throttled ({r.status_code}), sleeping {back:.0f}s", flush=True)
                time.sleep(back)
                continue
            return r  # 404 and friends: let the caller decide
        return None


# --------------------------------------------------------------------------
# Genre vocabulary
# --------------------------------------------------------------------------


def load_genre_vocabulary(http: Throttled) -> set[str]:
    """MusicBrainz's canonical genre list, cached to disk after the first run."""
    if GENRE_VOCAB_FILE.exists():
        text = GENRE_VOCAB_FILE.read_text(encoding="utf-8")
    else:
        print("Fetching MusicBrainz genre vocabulary ...")
        r = http.get(MB_GENRES_URL, params={"fmt": "txt"})
        if r is None or r.status_code != 200:
            print("  ! could not fetch genre vocabulary; keeping all tags")
            return set()
        text = r.text
        GENRE_VOCAB_FILE.write_text(text, encoding="utf-8")
    vocab = {line.strip().casefold() for line in text.splitlines() if line.strip()}
    print(f"Genre vocabulary: {len(vocab):,} canonical genres")
    return vocab


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


def load_overrides() -> dict[str, dict]:
    """Hand-written answers to the review list, keyed by normalised name.

    Resolution refuses to guess, which is right, but it left the review list
    write-only: Stage 2 ranked what it could not resolve by listening time and
    offered no way to hand an answer back. This is that way.

    Two kinds of row:

        Wale,ab2528dd-...,the US rapper not the percussionist
        Various Artists,IGNORE,compilation placeholder

    An MBID pins the artist and skips the search entirely. IGNORE marks a name
    that is not an artist at all, so it stops surfacing in the review list on
    every future run.

    Keyed on the *normalised* name, so an entry written `A$AP Rocky` matches
    however the export happens to spell it — the same folding resolution uses.
    """
    path = config.ARTIST_OVERRIDES_CSV
    if not path.exists():
        return {}

    out: dict[str, dict] = {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for lineno, row in enumerate(csv.DictReader(fh), start=2):
            name = (row.get("artist_name") or "").strip()
            raw = (row.get("mbid") or "").strip()
            # Comment rows are skipped before validation — a prose line with a
            # comma in it would otherwise parse as a malformed override.
            if not name or name.startswith("#"):
                continue
            has_tags = bool((row.get("tags") or "").strip())
            # A row may carry tags with no MBID. The artists worst served by
            # MusicBrainz are exactly the ones it cannot resolve either, so
            # requiring an MBID before accepting hand tags would lock out the
            # very cases hand-tagging exists for. Resolution stays unresolved;
            # only the genres are supplied.
            if not raw and not has_tags:
                continue
            ignore = raw.casefold() == "ignore" if raw else False
            if raw and not ignore and not MBID_RE.fullmatch(raw):
                print(f"  ⚠ {path.name} line {lineno}: "
                      f"'{raw}' is neither a UUID nor IGNORE — skipped")
                continue
            # Hand-supplied genres, pipe-separated. MusicBrainz coverage falls
            # off hard for smaller artists — in this library 100% of 50h+
            # artists carry a genre tag but only 64% of the under-30-minute
            # ones do — so an artist can resolve perfectly and still contribute
            # nothing. This is how you answer that, and it is the only way a
            # genre ever enters this project by hand rather than by lookup.
            tags = [t.strip() for t in (row.get("tags") or "").split("|") if t.strip()]
            out[normalise(name)] = {
                # None for both IGNORE and a tags-only row: neither pins an
                # artist, so neither may reach resolve_via_override.
                "mbid": raw.casefold() if (raw and not ignore) else None,
                "ignore": ignore,
                "note": (row.get("note") or "").strip(),
                "tags": tags,
            }
    return out


def apply_override_tags(cache: dict[str, dict], overrides: dict[str, dict],
                        vocab: set[str]) -> int:
    """Replace an artist's tags with hand-supplied ones where the file gives them.

    REPLACES rather than merges, for the same reason the poller's credits
    replace the title regex's: a hand answer exists because the looked-up one
    was absent or wrong, and merging would keep the thing being corrected.

    These are never written to the resolution cache — like IGNORE rows, they
    are recomputed on every run, so editing the CSV takes effect immediately
    instead of being frozen behind an append-only cache entry.

    A tag outside the MusicBrainz genre vocabulary is refused rather than
    written as a non-genre: hand-tagging is a shortcut around the lookup, not
    around the vocabulary, and a typo would otherwise sit in the data unnoticed.
    """
    applied = 0
    for name, rec in cache.items():
        want = (overrides.get(normalise(name)) or {}).get("tags") or []
        if not want:
            continue
        good = [t for t in want if (not vocab) or (t.casefold() in vocab)]
        for bad in [t for t in want if t not in good]:
            print(f"  ⚠ {config.ARTIST_OVERRIDES_CSV.name}: '{bad}' is not a "
                  f"MusicBrainz genre — skipped for {name}")
        if not good:
            continue
        # Equal weight: a person saying "these are the genres" is not casting
        # votes, so pretending to know relative strength would be invention.
        rec["tags"] = [{"tag": t, "count": config.OVERRIDE_TAG_COUNT} for t in good]
        rec["source"] = "override"
        applied += 1
    return applied


def override_satisfied(rec: dict, ov: dict) -> bool:
    """Is this cached record the answer the override file currently asks for?"""
    return (rec.get("source") == "override"
            and rec.get("status") == "resolved"
            and rec.get("mbid") == ov["mbid"])


def purge_stale_overrides(cache: dict[str, dict], overrides: dict[str, dict]) -> int:
    """Drop cached override answers the file no longer backs.

    The cache is append-only and last-write-wins, so without this, deleting a
    line from the override file would leave its answer frozen in place forever
    and the artist would never be resolved normally again.
    """
    stale = [
        name for name, rec in cache.items()
        if rec.get("source") == "override"
        and not override_satisfied(rec, overrides.get(normalise(name)) or {"mbid": object()})
    ]
    for name in stale:
        del cache[name]
    return len(stale)


def resolve_via_override(http: Throttled, name: str, mbid: str) -> dict:
    """Fetch tags for a hand-supplied MBID. No search, no ranking, no guessing.

    Tags are returned unfiltered, exactly as `resolve_via_musicbrainz` does —
    `write_outputs` is the single place the genre vocabulary is applied.
    """
    base = {"artist_name": name, "source": "override", "mbid": mbid}
    # MB_SEARCH_URL is the artist endpoint; /{mbid} is a direct lookup on it.
    r = http.get(f"{MB_SEARCH_URL}/{mbid}",
                 params={"inc": "tags+genres", "fmt": "json"})
    if r is None or r.status_code != 200:
        return {**base, "status": "error", "score": None, "matched_name": None,
                "n_candidates": 0, "tags": []}

    d = r.json()
    # `inc=genres` is MusicBrainz's curated list; raw tags are the fallback.
    src = d.get("genres") or d.get("tags") or []
    tags = [{"tag": t["name"].casefold(), "count": max(t.get("count") or 0, 0)}
            for t in src if t.get("name")]
    return {**base, "status": "resolved", "score": 100,
            "matched_name": d.get("name"), "n_candidates": 1, "tags": tags}


def apply_overrides(http: Throttled | None, artists: list[str],
                    cache: dict[str, dict], overrides: dict[str, dict]) -> dict:
    """Fold manual answers over the cache. An override always wins.

    IGNORE entries are applied in memory and never cached: they cost no request,
    so recomputing them every run keeps the file authoritative for free. MBID
    entries do cost a request, so those are cached and re-fetched only when the
    file changes.
    """
    stats = {"ignored": 0, "pinned": 0, "fetched": 0, "failed": 0, "unused": 0,
             "tags_only": 0}
    seen: set[str] = set()

    for name in artists:
        key = normalise(name)
        ov = overrides.get(key)
        if not ov:
            continue
        seen.add(key)

        if ov["ignore"]:
            cache[name] = {
                "artist_name": name, "source": "override", "status": "ignored",
                "mbid": None, "score": None, "matched_name": None,
                "n_candidates": 0, "tags": [], "note": ov["note"],
            }
            stats["ignored"] += 1
            continue

        if ov["mbid"] is None:
            # A tags-only row. It says nothing about WHICH artist this is, so
            # resolution is left to run (or fail) exactly as it would without
            # the file; only the genres are supplied, later, by
            # apply_override_tags. Nothing is pinned and nothing is cached.
            stats["tags_only"] += 1
            continue

        prev = cache.get(name)
        if prev and override_satisfied(prev, ov):
            stats["pinned"] += 1
            continue
        if http is None:            # --report: no network, leave the cache alone
            continue

        rec = resolve_via_override(http, name, ov["mbid"])
        rec["note"] = ov["note"]
        append_cache(rec)
        cache[name] = rec
        if rec["status"] == "resolved":
            stats["fetched"] += 1
            stats["pinned"] += 1
        else:
            stats["failed"] += 1

    stats["unused"] = len(set(overrides) - seen)
    return stats


def load_cache() -> dict[str, dict]:
    """Read the append-only resolution cache. Last entry for a name wins."""
    if not CACHE_FILE.exists():
        return {}
    cache: dict[str, dict] = {}
    with CACHE_FILE.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # truncated final line from an interrupted run
            cache[rec["artist_name"]] = rec
    return cache


def append_cache(rec: dict) -> None:
    """One record, one line, flushed immediately so a crash loses nothing."""
    with CACHE_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


# --------------------------------------------------------------------------
# MusicBrainz resolution
# --------------------------------------------------------------------------


def resolve_via_musicbrainz(http: Throttled, name: str) -> dict:
    """Resolve one artist name. Never guesses: no exact match means review."""
    # Search name AND alias. `artist:"..."` alone matches only the primary
    # name, which silently loses every artist MusicBrainz has since renamed:
    # "Kanye West" returns a tribute band, because the real entry is now "Ye"
    # with "Kanye West" demoted to an alias.
    escaped = name.replace('\\', r'\\').replace('"', r'\"')
    query = f'artist:"{escaped}" OR alias:"{escaped}"'
    r = http.get(
        MB_SEARCH_URL,
        params={"query": query, "fmt": "json", "limit": 8},
    )
    base = {"artist_name": name, "source": "musicbrainz", "tags": []}

    if r is None or r.status_code != 200:
        return {**base, "status": "error", "mbid": None, "score": None,
                "matched_name": None, "n_candidates": 0}

    candidates = r.json().get("artists", [])
    if not candidates:
        return {**base, "status": "not_found", "mbid": None, "score": None,
                "matched_name": None, "n_candidates": 0}

    target = normalise(name)

    def match_rank(c: dict) -> tuple | None:
        """Rank an exact match, or None if it does not match at all.

        Taking the FIRST exact match is wrong: MusicBrainz sorts by its own
        score, and an obscure artist carrying the name as an *alias* can outrank
        the artist whose actual name it is. Searching "Wale" puts percussionist
        "Reg Wale" first at score 100 (alias match) ahead of the US rapper Wale
        at 82 — and the rapper is the one with tags. Preferring a primary-name
        match over an alias match settles it; tags and score break ties.
        """
        primary = normalise(c.get("name", "")) == target
        alias = any(
            normalise(a.get("name", "")) == target
            for a in (c.get("aliases") or [])
        )
        if not (primary or alias):
            return None
        n_tags = len(c.get("tags") or [])
        return (primary, n_tags > 0, c.get("score") or 0, n_tags)

    ranked = sorted(
        ((match_rank(c), c) for c in candidates),
        key=lambda pair: pair[0] or (),
        reverse=True,
    )
    exact = next((c for rank, c in ranked if rank is not None), None)

    if exact is None:
        top = candidates[0]
        return {
            **base, "status": "ambiguous", "mbid": None,
            "score": top.get("score"), "matched_name": top.get("name"),
            "n_candidates": len(candidates),
        }

    return {
        **base,
        "status": "resolved",
        "mbid": exact.get("id"),
        "score": exact.get("score"),
        "matched_name": exact.get("name"),
        "n_candidates": len(candidates),
        "tags": [
            {"tag": t["name"].casefold(), "count": t.get("count") or 0}
            for t in (exact.get("tags") or [])
            if t.get("name")
        ],
    }


# --------------------------------------------------------------------------
# Backfill for artists MusicBrainz resolved but never tagged
# --------------------------------------------------------------------------
#
# The plan here was Spotify's artist endpoint, which used to expose `genres`.
# It no longer does: as of this writing `/v1/artists/{id}` returns 200 with
# `genres`, `popularity` and `followers` all absent (verified against Drake,
# Taylor Swift, Metallica and ArrDee), batch `/v1/artists` and `top-tracks`
# answer 403, and search results never carried genres to begin with. There is
# no genre data left to fall back to, so that path was removed rather than
# left in place to silently find nothing.
#
# ListenBrainz's metadata lookup would work but answers 401 without an
# Authorization header — the no-auth guarantee covers only the `labs` host.
#
# What does work, with no auth and the infrastructure already here: an artist's
# RELEASE GROUPS are frequently tagged even when the artist page is not.
# Tion Wayne carries no artist tags but his release groups yield dance, hip
# hop, uk garage, drill and uk drill.


def tags_from_release_groups(http: Throttled, mbid: str) -> list[dict]:
    """Aggregate tag votes across everything the artist released."""
    r = http.get(
        MB_RELEASE_GROUP_URL,
        params={"artist": mbid, "inc": "tags", "fmt": "json", "limit": 100},
    )
    if r is None or r.status_code != 200:
        return []
    totals: dict[str, int] = {}
    for rg in r.json().get("release-groups", []):
        for t in rg.get("tags") or []:
            name = (t.get("name") or "").casefold()
            if name:
                totals[name] = totals.get(name, 0) + (t.get("count") or 1)
    return [{"tag": k, "count": v} for k, v in
            sorted(totals.items(), key=lambda kv: -kv[1])]


# --------------------------------------------------------------------------
# Persist + report
# --------------------------------------------------------------------------


def write_outputs(con: duckdb.DuckDBPyConnection, cache: dict[str, dict],
                  vocab: set[str]) -> None:
    """Flatten the cache into artist_tags / artist_resolution / artist_review."""
    tag_rows, res_rows = [], []
    for name, rec in cache.items():
        res_rows.append(
            (name, rec.get("mbid"), rec.get("status"), rec.get("source"),
             rec.get("matched_name"), rec.get("score"), rec.get("n_candidates") or 0,
             len(rec.get("tags") or []))
        )
        for t in rec.get("tags") or []:
            tag = t["tag"]
            tag_rows.append(
                (name, rec.get("mbid"), tag, t.get("count") or 0,
                 (not vocab) or (tag in vocab), rec.get("source"))
            )

    con.execute("""CREATE OR REPLACE TABLE artist_resolution (
        artist_name VARCHAR, mbid VARCHAR, status VARCHAR, source VARCHAR,
        matched_name VARCHAR, score INTEGER, n_candidates INTEGER, n_tags INTEGER)""")
    if res_rows:
        con.executemany(
            "INSERT INTO artist_resolution VALUES (?,?,?,?,?,?,?,?)", res_rows)

    con.execute("""CREATE OR REPLACE TABLE artist_tags (
        artist_name VARCHAR, mbid VARCHAR, tag VARCHAR, tag_count INTEGER,
        is_genre BOOLEAN, source VARCHAR)""")
    if tag_rows:
        con.executemany("INSERT INTO artist_tags VALUES (?,?,?,?,?,?)", tag_rows)

    for table, path in (
        ("artist_tags", config.ARTIST_TAGS_PARQUET),
        ("artist_resolution", RESOLUTION_PARQUET),
    ):
        con.execute(
            f"COPY (SELECT * FROM {table} ORDER BY ALL) TO '{path}' "
            f"(FORMAT PARQUET, COMPRESSION ZSTD)")

    con.execute(
        f"""COPY (
            SELECT r.artist_name, r.status, r.matched_name, r.score, r.n_candidates,
                   w.listening_hours
            FROM artist_resolution r
            LEFT JOIN artist_weight w USING (artist_name)
            -- 'ignored' names were reviewed once and declared not-an-artist.
            -- Re-listing them is exactly what the override file exists to stop.
            WHERE r.status <> 'ignored'
              AND (r.status <> 'resolved' OR r.n_tags = 0)
            ORDER BY w.listening_hours DESC NULLS LAST
        ) TO '{REVIEW_PARQUET}' (FORMAT PARQUET, COMPRESSION ZSTD)"""
    )


def report(con: duckdb.DuckDBPyConnection) -> None:
    q = lambda s: con.execute(s).fetchone()  # noqa: E731

    print()
    print("=" * 74)
    print("STAGE 2 — GENRE ENRICHMENT")
    print("=" * 74)

    print("\n--- Resolution by artist count ---")
    total = q("SELECT count(*) FROM artist_resolution")[0]
    for st, n in con.execute(
        "SELECT status, count(*) FROM artist_resolution GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall():
        print(f"   {st:<12} {n:>6,}  ({100*n/total:>5.1f}%)")

    # The number that actually matters: if a big share of listening time hangs
    # off unresolved artists, the trend analysis downstream is not trustworthy.
    print("\n--- Resolution WEIGHTED BY LISTENING TIME (the number that matters) ---")
    for st, h, pct in con.execute(
        """
        SELECT coalesce(r.status, 'never_attempted') AS st,
               sum(w.listening_hours) AS h,
               100.0 * sum(w.listening_hours) / (SELECT sum(listening_hours) FROM artist_weight)
        FROM artist_weight w
        LEFT JOIN artist_resolution r USING (artist_name)
        GROUP BY 1 ORDER BY h DESC
        """
    ).fetchall():
        print(f"   {st:<16} {h:>8,.1f} h  ({pct:>5.1f}%)")

    covered = q(
        """
        SELECT 100.0 * sum(w.listening_hours) FILTER (
                   WHERE r.status = 'resolved' AND r.n_tags > 0)
             / sum(w.listening_hours)
        FROM artist_weight w LEFT JOIN artist_resolution r USING (artist_name)
        """
    )[0]
    genre_covered = q(
        """
        SELECT 100.0 * sum(w.listening_hours) FILTER (WHERE t.artist_name IS NOT NULL)
             / sum(w.listening_hours)
        FROM artist_weight w
        LEFT JOIN (SELECT DISTINCT artist_name FROM artist_tags WHERE is_genre) t
               USING (artist_name)
        """
    )[0]
    print(f"\n   ► listening time with ANY tags   : {covered:.1f}%")
    print(f"   ► listening time with a GENRE tag: {genre_covered:.1f}%")

    n_tags, n_genres = q(
        "SELECT count(*), count(*) FILTER (WHERE is_genre) FROM artist_tags")
    print(f"\n   tag rows: {n_tags:,}   of which canonical genres: {n_genres:,}")

    print("\n--- Top genres by artist count ---")
    for t, n in con.execute(
        "SELECT tag, count(DISTINCT artist_name) c FROM artist_tags "
        "WHERE is_genre GROUP BY 1 ORDER BY c DESC LIMIT 15"
    ).fetchall():
        print(f"   {t:<28} {n:>5,} artists")

    print("\n--- REVIEW LIST: unresolved/untagged artists, by listening time ---")
    rows = con.execute(
        """
        SELECT r.artist_name, r.status, r.matched_name, w.listening_hours
        FROM artist_resolution r LEFT JOIN artist_weight w USING (artist_name)
        WHERE r.status <> 'ignored'
          AND (r.status <> 'resolved' OR r.n_tags = 0)
        ORDER BY w.listening_hours DESC NULLS LAST LIMIT 20
        """
    ).fetchall()
    if not rows:
        print("   (none)")
    for a, st, m, h in rows:
        near = f"  nearest: {m}" if m else ""
        print(f"   {(h or 0):>6.1f} h  {a[:32]:<32} {st}{near}")
    print(f"\n   full review list -> {REVIEW_PARQUET}")

    n_ignored = q("SELECT count(*) FROM artist_resolution "
                  "WHERE status = 'ignored'")[0]
    print(f"\n   To answer any of these by hand, add a row to "
          f"{config.ARTIST_OVERRIDES_CSV.name}:")
    print("       artist_name,mbid,note")
    print("       Wale,ab2528dd-...,the US rapper not the percussionist")
    print("       Various Artists,IGNORE,not an artist")
    if n_ignored:
        print(f"   ({n_ignored:,} name(s) currently suppressed by IGNORE)")
    print("=" * 74)


# --------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Stage 2 genre enrichment")
    ap.add_argument("--limit", type=int, help="resolve at most N new artists")
    ap.add_argument("--report", action="store_true",
                    help="rebuild outputs and print the summary; no network calls")
    ap.add_argument("--retry-errors", action="store_true",
                    help="re-attempt names previously cached as error/not_found")
    ap.add_argument("--no-backfill", action="store_true",
                    help="skip the release-group pass for untagged artists")
    args = ap.parse_args()

    config.ensure_dirs()
    credits_path = config.DATA_DIR / "track_credits.parquet"
    if not credits_path.exists():
        raise SystemExit(f"{credits_path} not found — run ingest.py then credits.py.")

    con = duckdb.connect()
    con.execute(f"CREATE VIEW track_credits AS SELECT * FROM '{credits_path}'")
    # Listening weight per artist, counting every credit. This is what the
    # resolution rate is weighted by.
    con.execute(
        """
        CREATE OR REPLACE TABLE artist_weight AS
        SELECT artist_name, sum(played_seconds)/3600.0 AS listening_hours
        FROM track_credits GROUP BY artist_name
        """
    )

    artists = [r[0] for r in con.execute(
        "SELECT artist_name FROM artist_weight ORDER BY listening_hours DESC"
    ).fetchall()]

    http = Throttled(MB_MIN_INTERVAL)
    vocab = load_genre_vocabulary(http) if not args.report else (
        {l.strip().casefold() for l in GENRE_VOCAB_FILE.read_text().splitlines() if l.strip()}
        if GENRE_VOCAB_FILE.exists() else set()
    )
    cache = load_cache()
    print(f"Artists to cover: {len(artists):,}   already cached: {len(cache):,}")

    overrides = load_overrides()
    if overrides:
        dropped = purge_stale_overrides(cache, overrides)
        ov_stats = apply_overrides(None if args.report else http,
                                   artists, cache, overrides)
        print(
            f"Overrides: {len(overrides):,} in {config.ARTIST_OVERRIDES_CSV.name} "
            f"— {ov_stats['pinned']:,} pinned "
            f"({ov_stats['fetched']:,} newly fetched), "
            f"{ov_stats['ignored']:,} ignored"
            + (f", {ov_stats['failed']:,} failed" if ov_stats["failed"] else "")
            + (f", {dropped:,} stale dropped" if dropped else "")
            + (f", {ov_stats['unused']:,} match no artist"
               if ov_stats["unused"] else "")
        )

    if not args.report:
        retry = {"error", "not_found"} if args.retry_errors else {"error"}
        # An override always wins, so an overridden name is never searched —
        # including one whose override fetch failed, which retries as an
        # override on the next run rather than falling back to a guess.
        overridden = set(overrides)
        todo = [a for a in artists
                if normalise(a) not in overridden
                and (a not in cache or cache[a].get("status") in retry)]
        if args.limit:
            todo = todo[: args.limit]

        if todo:
            eta = len(todo) * MB_MIN_INTERVAL / 60
            print(f"Resolving {len(todo):,} artists via MusicBrainz "
                  f"(~{eta:.0f} min at {MB_MIN_INTERVAL}s/req). Ctrl-C is safe.\n")
            try:
                for i, name in enumerate(todo, 1):
                    rec = resolve_via_musicbrainz(http, name)
                    append_cache(rec)
                    cache[name] = rec
                    if i % 25 == 0 or i == len(todo):
                        done = sum(1 for r in cache.values() if r.get("status") == "resolved")
                        pct = 100 * i / len(todo)
                        print(f"  [{i:>5,}/{len(todo):,}] {pct:5.1f}%  "
                              f"resolved so far: {done:,}", flush=True)
            except KeyboardInterrupt:
                print("\nInterrupted — progress is cached, re-run to resume.\n")

        # Second pass: artists that resolved to a real MBID but carry no tags.
        # `backfilled` marks a record as already attempted so a re-run does not
        # spend requests re-checking artists whose releases are also untagged.
        if not args.no_backfill:
            gaps = [
                n for n, r in cache.items()
                if r.get("status") == "resolved" and r.get("mbid")
                and not r.get("tags") and not r.get("backfilled")
            ]
            if gaps:
                print(f"\nBackfilling {len(gaps):,} untagged artists from release "
                      f"groups (~{len(gaps) * MB_MIN_INTERVAL / 60:.0f} min).\n")
                try:
                    for i, name in enumerate(gaps, 1):
                        rec = dict(cache[name])
                        tags = tags_from_release_groups(http, rec["mbid"])
                        rec["tags"] = tags
                        rec["backfilled"] = True
                        if tags:
                            rec["source"] = "musicbrainz-release-group"
                        append_cache(rec)
                        cache[name] = rec
                        if i % 25 == 0 or i == len(gaps):
                            filled = sum(
                                1 for r in cache.values()
                                if r.get("source") == "musicbrainz-release-group"
                                and r.get("tags")
                            )
                            print(f"  [{i:>5,}/{len(gaps):,}] "
                                  f"{100*i/len(gaps):5.1f}%  recovered: {filled:,}",
                                  flush=True)
                except KeyboardInterrupt:
                    print("\nInterrupted — progress is cached, re-run to resume.\n")

    # Applied last, so a hand answer beats both the lookup and the
    # release-group backfill. Not cached — see apply_override_tags.
    hand = apply_override_tags(cache, overrides, vocab)
    if hand:
        print(f"\nHand-tagged {hand} artist(s) from "
              f"{config.ARTIST_OVERRIDES_CSV.name}")

    write_outputs(con, cache, vocab)
    report(con)
    print(f"\nWrote {config.ARTIST_TAGS_PARQUET}")
    print(f"Wrote {RESOLUTION_PARQUET}")


if __name__ == "__main__":
    main()
