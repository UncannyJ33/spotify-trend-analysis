"""tests/test_recommend_cache.py — Stage 5's caches hold answers, never failures.

No network. A FakeHttp stands in for enrich.Throttled; the caches are real
append-only JSONL files in a temporary directory, so the reload path is the
one a re-run takes.

fetch_similar and fetch_candidate_tags used to append a record whatever came
back, so a 503 that outlasted Throttled's retries was written down as "this
artist has no similar artists" and never asked again — about 30 of 150 seeds
on the last write carry an empty list. Pinned here, for BOTH fetchers:

  * a failure returns [] and appends nothing, so the next run asks again;
  * a 200 is appended with "status": 200, including an empty 200, which is a
    real answer and final;
  * a cached empty record with no status is a legacy entry that may have been
    a failure: it is re-asked exactly once, and the new record then wins;
  * a legacy non-empty record is an answer (a failure never produced one) and
    is never re-asked.
"""
import json
import pathlib
import shutil
import sys
import tempfile
sys.path.insert(0, ".")

import recommend

failures = []
def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")


class FakeResp:
    def __init__(self, status_code, payload=None, bad_json=False):
        self.status_code = status_code
        self._payload = payload
        self._bad = bad_json
    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._payload


class FakeHttp:
    """Returns the queued responses in order; None is Throttled giving up."""
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0
    def get(self, url, params=None):
        self.calls += 1
        return self.responses.pop(0) if self.responses else None


def lines(path):
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l]


tmp = pathlib.Path(tempfile.mkdtemp())
recommend.SIMILAR_CACHE = tmp / "similar_artists.jsonl"
recommend.CANDIDATE_TAG_CACHE = tmp / "candidate_tags.jsonl"

LB_HIT = [{"artist_mbid": "cand-1", "name": "Eptic", "score": 12, "comment": ""}]
MB_HIT = {"genres": [{"name": "Dubstep", "count": 3}], "tags": []}

# One row per fetcher: how to call it, the cache file, the key and list fields,
# a non-empty 200 payload and an empty 200 payload.
FETCHERS = [
    ("fetch_similar",
     lambda http, key, cache: recommend.fetch_similar(http, key, cache),
     lambda: recommend.SIMILAR_CACHE, "seed_mbid", "similar", LB_HIT, []),
    ("fetch_candidate_tags",
     lambda http, key, cache: recommend.fetch_candidate_tags(http, key, set(), cache),
     lambda: recommend.CANDIDATE_TAG_CACHE, "mbid", "tags", MB_HIT,
     {"genres": [], "tags": []}),
]

for name, fetch, path_of, key_field, list_field, hit, empty in FETCHERS:
    print(f"{name}:")
    path = path_of()

    # --- failures cache nothing -------------------------------------------
    for label, resp in (("Throttled gave up (503 x4 -> None)", None),
                        ("a 503 response", FakeResp(503)),
                        ("a 500 response", FakeResp(500)),
                        ("a 200 whose body is not JSON", FakeResp(200, bad_json=True))):
        cache = {}
        http = FakeHttp(resp)
        got = fetch(http, "fail-mbid", cache)
        check(f"{label}: returns []", got, [])
        check(f"{label}: nothing appended", lines(path), [])
        check(f"{label}: nothing cached in memory", "fail-mbid" in cache, False)
    # ... so the next run asks again.
    http = FakeHttp(FakeResp(200, hit))
    fetch(http, "fail-mbid", {})
    check("a failed key is asked again on the next run", http.calls, 1)
    path.unlink()

    # --- an empty 200 is a real answer --------------------------------------
    cache = {}
    http = FakeHttp(FakeResp(200, empty))
    check("an empty 200 returns []", fetch(http, "empty-mbid", cache), [])
    recs = lines(path)
    check("an empty 200 is appended with its status",
          [(r[key_field], r[list_field], r.get("status")) for r in recs],
          [("empty-mbid", [], 200)])
    fetch(http, "empty-mbid", cache)
    reloaded = recommend.load_jsonl(path, key_field)
    fetch(http, "empty-mbid", reloaded)
    check("an empty 200 is final, in this run and the next", http.calls, 1)

    # --- a non-empty 200 ----------------------------------------------------
    cache = {}
    http = FakeHttp(FakeResp(200, hit))
    got = fetch(http, "hit-mbid", cache)
    check("a 200 returns its answer", len(got), 1)
    check("a 200 is appended with its status",
          lines(path)[-1].get("status"), 200)
    path.unlink()

    # --- legacy entries (written before a status was recorded) --------------
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({key_field: "legacy-empty", list_field: []}) + "\n")
        fh.write(json.dumps({key_field: "legacy-full",
                             list_field: [{"legacy": True}]}) + "\n")
    cache = recommend.load_jsonl(path, key_field)

    http = FakeHttp(FakeResp(200, hit))
    got = fetch(http, "legacy-full", cache)
    check("a legacy non-empty entry is never re-asked", http.calls, 0)
    check("a legacy non-empty entry is served as is", got, [{"legacy": True}])

    http = FakeHttp(FakeResp(200, hit))
    got = fetch(http, "legacy-empty", cache)
    check("a legacy empty entry is re-asked", http.calls, 1)
    check("the re-ask's answer is returned", len(got), 1)
    fetch(http, "legacy-empty", cache)
    fetch(http, "legacy-empty", recommend.load_jsonl(path, key_field))
    check("... exactly once: the new record wins, in this run and the next",
          http.calls, 1)
    check("the new record is appended, not rewritten over the old one",
          [r.get("status") for r in lines(path)], [None, None, 200])

    # A re-ask that fails leaves the legacy entry to be asked again next run,
    # rather than stamping the possible failure as final.
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({key_field: "legacy-empty", list_field: []}) + "\n")
    http = FakeHttp(None)
    check("a failed re-ask returns []",
          fetch(http, "legacy-empty", recommend.load_jsonl(path, key_field)), [])
    check("a failed re-ask appends nothing", len(lines(path)), 1)
    http = FakeHttp(FakeResp(200, hit))
    fetch(http, "legacy-empty", recommend.load_jsonl(path, key_field))
    check("... and is asked again on the next run", http.calls, 1)
    path.unlink()

# --- the report path never re-asks --------------------------------------------
# main() decides what still needs a request through cached_answer; --report
# must not reach the network, legacy entries included.
cache = {"legacy": {"seed_mbid": "legacy", "similar": []},
         "answered-empty": {"seed_mbid": "answered-empty", "similar": [], "status": 200},
         "old-full": {"seed_mbid": "old-full", "similar": [{"x": 1}]}}
check("cached_answer: a legacy empty entry still needs asking",
      recommend.cached_answer(cache, "legacy", "similar"), None)
check("cached_answer: an empty 200 is an answer",
      recommend.cached_answer(cache, "answered-empty", "similar"), [])
check("cached_answer: a legacy non-empty entry is an answer",
      recommend.cached_answer(cache, "old-full", "similar"), [{"x": 1}])
check("cached_answer: an unseen key needs asking",
      recommend.cached_answer(cache, "unseen", "similar"), None)

shutil.rmtree(tmp, ignore_errors=True)

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
