"""tests/test_override_cache.py — a pinned override costs requests once, not every run.

The release-group backfill used to rewrite a record's `source` from `override`
to `musicbrainz-release-group`. On the next run `override_satisfied` no longer
recognised the record as the override's answer, so the pin was fetched again,
came back untagged again, and was backfilled again: REAPER, Reaper, NOTION,
Ylti, CJ, ALLEYCVT and Levity cost about 14 requests on every run, forever.
Provenance has to survive the backfill.

Offline: a FakeHttp counts calls, and the cache and override file live in a
temp dir. Each "run" reloads the cache from disk, exactly as enrich.py does.
"""
import sys
sys.path.insert(0, ".")
import json
import pathlib
import tempfile
from collections import Counter

import enrich

failures = []
def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")


d = pathlib.Path(tempfile.mkdtemp())
enrich.CACHE_FILE = d / "artist_resolution.jsonl"
enrich.config.ARTIST_OVERRIDES_CSV = d / "artist_overrides.csv"

PIN = "0f3a6f2e-1c4b-4d5e-8a9b-0c1d2e3f4a5b"       # made up; shape is all that matters
ORGANIC = "9e8d7c6b-5a49-4382-9176-5e4d3c2b1a09"
ARTISTS = ["REAPER", "Tion Wayne"]
RG_TAGS = [{"tag": "speed garage", "count": 3}]


class FakeResponse:
    status_code = 200
    def __init__(self, body):
        self._body = body
    def json(self):
        return self._body


class FakeHttp:
    """The artist lookup comes back untagged; its release groups carry tags.

    That is the exact shape that triggered the loop: a pin whose artist page has
    no genres, so the backfill is what finds them.
    """
    def __init__(self):
        self.calls = Counter()
    def get(self, url, **kw):
        if url == enrich.MB_RELEASE_GROUP_URL:
            self.calls["release-group"] += 1
            return FakeResponse({"release-groups": [
                {"tags": [{"name": "Speed Garage", "count": 3}]}]})
        if url.startswith(enrich.MB_SEARCH_URL + "/"):
            self.calls["lookup"] += 1
            return FakeResponse({"id": url.rsplit("/", 1)[1], "name": "REAPER",
                                 "genres": [], "tags": []})
        raise AssertionError(f"unexpected request: {url}")


def write_overrides(*rows):
    enrich.config.ARTIST_OVERRIDES_CSV.write_text(
        "artist_name,mbid,note,tags\n" + "".join(r + "\n" for r in rows),
        encoding="utf-8")


def run(http):
    """One enrich.py pass over the override and backfill paths, in main()'s order."""
    cache = enrich.load_cache()
    overrides = enrich.load_overrides()
    purged = enrich.purge_stale_overrides(cache, overrides)
    stats = enrich.apply_overrides(http, ARTISTS, cache, overrides)
    recovered = enrich.backfill_untagged(http, cache)
    return cache, stats, recovered, purged


# An organic record: resolved by search, untagged, never backfilled.
enrich.append_cache({"artist_name": "Tion Wayne", "source": "musicbrainz",
                     "status": "resolved", "mbid": ORGANIC, "score": 100,
                     "matched_name": "Tion Wayne", "n_candidates": 1, "tags": []})
write_overrides(f"REAPER,{PIN},the dubstep producer")

# --------------------------------------------------------------------------
# Run 1: one fetch for the pin, one backfill call each, and both find tags.
# --------------------------------------------------------------------------
http = FakeHttp()
cache, stats, recovered, _ = run(http)
check("run 1: one override fetch", http.calls["lookup"], 1)
check("run 1: one backfill call per untagged artist", http.calls["release-group"], 2)
check("run 1: backfill recovered both", recovered, 2)
check("run 1: pin's tags came from its release groups",
      cache["REAPER"]["tags"], RG_TAGS)
check("run 1: the pin keeps override provenance", cache["REAPER"]["source"], "override")
check("run 1: and records where its tags came from",
      cache["REAPER"].get("tags_from"), "release-group")
check("run 1: organic backfill still writes musicbrainz-release-group",
      cache["Tion Wayne"]["source"], "musicbrainz-release-group")
check("run 1: organic record is not given the override marker",
      "tags_from" in cache["Tion Wayne"], False)

# What was written to disk is what the next run reads; check it, not memory.
last = {}
for line in enrich.CACHE_FILE.read_text(encoding="utf-8").splitlines():
    rec = json.loads(line)
    last[rec["artist_name"]] = rec
check("run 1: the cached pin still says override", last["REAPER"]["source"], "override")

# --------------------------------------------------------------------------
# Run 2: nothing changed, so nothing is spent.
# --------------------------------------------------------------------------
http = FakeHttp()
cache, stats, recovered, _ = run(http)
check("run 2: zero requests", sum(http.calls.values()), 0)
check("run 2: the pin is satisfied from the cache", (stats["pinned"], stats["fetched"]), (1, 0))
check("run 2: backfill recovered nothing new", recovered, 0)
check("run 2: tags survive the reload", cache["REAPER"]["tags"], RG_TAGS)

# --------------------------------------------------------------------------
# Deleting the override row still frees the artist. Provenance surviving the
# backfill is what keeps purge_stale_overrides able to see it.
# --------------------------------------------------------------------------
write_overrides()
http = FakeHttp()
cache, stats, recovered, purged = run(http)
check("row removed: the pinned record is purged", "REAPER" in cache, False)
check("row removed: purge counted it", purged, 1)
check("row removed: purge costs no request", sum(http.calls.values()), 0)
check("row removed: organic record untouched",
      cache["Tion Wayne"]["source"], "musicbrainz-release-group")

# --------------------------------------------------------------------------
# A legacy record — written by the old backfill with source rewritten — fails
# override_satisfied once, is re-fetched once, and is then final.
# --------------------------------------------------------------------------
enrich.CACHE_FILE.unlink()
enrich.append_cache({"artist_name": "REAPER", "source": "musicbrainz-release-group",
                     "status": "resolved", "mbid": PIN, "score": 100,
                     "matched_name": "REAPER", "n_candidates": 1,
                     "tags": RG_TAGS, "backfilled": True})
write_overrides(f"REAPER,{PIN},the dubstep producer")

http = FakeHttp()
run(http)
check("legacy: first run after the fix re-fetches once",
      dict(http.calls), {"lookup": 1, "release-group": 1})
http = FakeHttp()
cache, *_ = run(http)
check("legacy: and then never again", sum(http.calls.values()), 0)
check("legacy: now carries override provenance", cache["REAPER"]["source"], "override")

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
