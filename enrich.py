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
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_ARTIST_SEARCH_URL = "https://api.spotify.com/v1/search"

# MusicBrainz asks for a descriptive agent with contact details and throttles
# anonymous clients to one request per second. Exceed it and they block you.
USER_AGENT = (
    "spotify-trend-analysis/0.1 "
    "( https://github.com/UncannyJ33/spotify-trend-analysis )"
)
MB_MIN_INTERVAL = 1.1  # seconds between MusicBrainz requests, with headroom

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
    exact = None
    for c in candidates:
        names = [c.get("name", "")] + [
            a.get("name", "") for a in c.get("aliases", []) or []
        ]
        if any(normalise(n) == target for n in names):
            exact = c
            break

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
# Spotify fallback (optional; only fills gaps MusicBrainz left)
# --------------------------------------------------------------------------


def spotify_token() -> str | None:
    cid, secret = os.getenv("SPOTIFY_CLIENT_ID"), os.getenv("SPOTIFY_CLIENT_SECRET")
    if not (cid and secret):
        return None
    r = requests.post(
        SPOTIFY_TOKEN_URL,
        data={"grant_type": "client_credentials"},
        auth=(cid, secret),
        timeout=30,
    )
    if r.status_code != 200:
        print(f"  ! Spotify auth failed ({r.status_code})")
        return None
    return r.json().get("access_token")


def resolve_via_spotify(token: str, name: str) -> dict | None:
    """Spotify still exposes `genres` on the artist object. Fallback only."""
    r = requests.get(
        SPOTIFY_ARTIST_SEARCH_URL,
        headers={"Authorization": f"Bearer {token}"},
        params={"q": name, "type": "artist", "limit": 5},
        timeout=30,
    )
    if r.status_code != 200:
        return None
    items = r.json().get("artists", {}).get("items", [])
    target = normalise(name)
    for it in items:
        if normalise(it.get("name", "")) == target and it.get("genres"):
            return {
                "artist_name": name, "status": "resolved", "source": "spotify",
                "mbid": None, "score": None, "matched_name": it["name"],
                "n_candidates": len(items),
                "tags": [{"tag": g.casefold(), "count": 1} for g in it["genres"]],
            }
    return None


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
            WHERE r.status <> 'resolved' OR r.n_tags = 0
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
        WHERE r.status <> 'resolved' OR r.n_tags = 0
        ORDER BY w.listening_hours DESC NULLS LAST LIMIT 20
        """
    ).fetchall()
    if not rows:
        print("   (none)")
    for a, st, m, h in rows:
        near = f"  nearest: {m}" if m else ""
        print(f"   {(h or 0):>6.1f} h  {a[:32]:<32} {st}{near}")
    print(f"\n   full review list -> {REVIEW_PARQUET}")
    print("=" * 74)


# --------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Stage 2 genre enrichment")
    ap.add_argument("--limit", type=int, help="resolve at most N new artists")
    ap.add_argument("--report", action="store_true",
                    help="rebuild outputs and print the summary; no network calls")
    ap.add_argument("--retry-errors", action="store_true",
                    help="re-attempt names previously cached as error/not_found")
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

    if not args.report:
        retry = {"error", "not_found"} if args.retry_errors else {"error"}
        todo = [a for a in artists
                if a not in cache or cache[a].get("status") in retry]
        if args.limit:
            todo = todo[: args.limit]

        if todo:
            eta = len(todo) * MB_MIN_INTERVAL / 60
            print(f"Resolving {len(todo):,} artists via MusicBrainz "
                  f"(~{eta:.0f} min at {MB_MIN_INTERVAL}s/req). Ctrl-C is safe.\n")
            token = spotify_token()
            if token:
                print("Spotify fallback: enabled\n")

            try:
                for i, name in enumerate(todo, 1):
                    rec = resolve_via_musicbrainz(http, name)
                    if token and (rec["status"] != "resolved" or not rec["tags"]):
                        alt = resolve_via_spotify(token, name)
                        if alt:
                            rec = alt
                    append_cache(rec)
                    cache[name] = rec
                    if i % 25 == 0 or i == len(todo):
                        done = sum(1 for r in cache.values() if r.get("status") == "resolved")
                        pct = 100 * i / len(todo)
                        print(f"  [{i:>5,}/{len(todo):,}] {pct:5.1f}%  "
                              f"resolved so far: {done:,}", flush=True)
            except KeyboardInterrupt:
                print("\nInterrupted — progress is cached, re-run to resume.\n")

    write_outputs(con, cache, vocab)
    report(con)
    print(f"\nWrote {config.ARTIST_TAGS_PARQUET}")
    print(f"Wrote {RESOLUTION_PARQUET}")


if __name__ == "__main__":
    main()
