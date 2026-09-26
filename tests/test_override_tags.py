"""tests/test_override_tags.py — hand-supplied genres for artists MusicBrainz never tagged.

Tag coverage in this library falls monotonically with listening time: 100% of
50h+ artists carry a genre, 64% of the under-30-minute ones do. An artist can
resolve perfectly and still contribute nothing, and the smaller the act the
likelier that is. This is the only path by which a genre enters the project by
hand rather than by lookup, so it has to refuse bad input loudly.
"""
import sys
sys.path.insert(0, ".")
import enrich

failures = []
def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")


VOCAB = {"folk pop", "pop rap", "pop", "christian hip hop", "indie pop"}


def cache_of(**kw):
    return {name: dict(rec) for name, rec in kw.items()}


# --------------------------------------------------------------------------
# Hand tags REPLACE looked-up ones, and mark their own provenance.
# --------------------------------------------------------------------------
cache = cache_of(
    GRAHAM={"tags": [], "source": "musicbrainz", "mbid": "m1"},
    Henrik={"tags": [{"tag": "ambient", "count": 1}, {"tag": "art rock", "count": 1}],
            "source": "musicbrainz-release-group", "mbid": "m2"},
    Untouched={"tags": [{"tag": "pop", "count": 9}], "source": "musicbrainz", "mbid": "m3"},
)
overrides = {
    enrich.normalise("GRAHAM"): {"tags": ["folk pop", "pop rap"]},
    enrich.normalise("Henrik"): {"tags": ["folk pop"]},
    enrich.normalise("Nobody"): {"tags": ["pop"]},          # not in the cache
    enrich.normalise("Untouched"): {"tags": []},            # row with no tags column
}
n = enrich.apply_override_tags(cache, overrides, VOCAB)
check("only artists with hand tags are touched", n, 2)
check("empty tags leave the looked-up answer alone",
      cache["Untouched"]["tags"], [{"tag": "pop", "count": 9}])
check("hand tags replace, not merge",
      [t["tag"] for t in cache["Henrik"]["tags"]], ["folk pop"])
check("wrong release-group tags are gone",
      any(t["tag"] == "ambient" for t in cache["Henrik"]["tags"]), False)
check("provenance is recorded", cache["GRAHAM"]["source"], "override")
check("untouched artist keeps its provenance",
      cache["Untouched"]["source"], "musicbrainz")
check("hand tags carry equal weight",
      {t["count"] for t in cache["GRAHAM"]["tags"]},
      {enrich.config.OVERRIDE_TAG_COUNT})

# The weight must not be below the Stage 8 anchor floor, or a hand-tagged
# artist could never anchor a playlist — which is most of the point.
check("hand tag weight clears the anchor floor",
      enrich.config.OVERRIDE_TAG_COUNT >= enrich.config.MIN_TAG_COUNT_FOR_ANCHOR,
      True)

# --------------------------------------------------------------------------
# A tag outside the MusicBrainz vocabulary is refused, not silently kept.
# Hand-tagging is a shortcut around the lookup, not around the vocabulary.
# --------------------------------------------------------------------------
cache = cache_of(A={"tags": [], "source": "musicbrainz", "mbid": "m"})
enrich.apply_override_tags(
    cache, {enrich.normalise("A"): {"tags": ["folk pop", "melodic rap", "typpo"]}}, VOCAB)
check("non-canonical tags dropped",
      [t["tag"] for t in cache["A"]["tags"]], ["folk pop"])

cache = cache_of(B={"tags": [{"tag": "rock", "count": 4}], "source": "musicbrainz", "mbid": "m"})
enrich.apply_override_tags(
    cache, {enrich.normalise("B"): {"tags": ["not a genre at all"]}}, VOCAB)
check("an all-invalid row changes nothing",
      [t["tag"] for t in cache["B"]["tags"]], ["rock"])
check("an all-invalid row does not claim override provenance",
      cache["B"]["source"], "musicbrainz")

# No vocabulary loaded (the fetch failed) -> accept what the human wrote rather
# than silently discarding every hand tag.
cache = cache_of(C={"tags": [], "source": "musicbrainz", "mbid": "m"})
enrich.apply_override_tags(cache, {enrich.normalise("C"): {"tags": ["whatever"]}}, set())
check("no vocabulary means trust the human",
      [t["tag"] for t in cache["C"]["tags"]], ["whatever"])

# Name matching folds the same way resolution does.
cache = cache_of(**{"A$AP Rocky": {"tags": [], "source": "musicbrainz", "mbid": "m"}})
enrich.apply_override_tags(
    cache, {enrich.normalise("ASAP Rocky"): {"tags": ["pop"]}}, VOCAB)
check("stylised names still match",
      [t["tag"] for t in cache["A$AP Rocky"]["tags"]], ["pop"])

# --------------------------------------------------------------------------
# A row may supply tags with NO mbid. The artists MusicBrainz serves worst are
# exactly the ones it cannot resolve either, so demanding an MBID first would
# lock out the cases hand-tagging exists for.
# --------------------------------------------------------------------------
import pathlib
import tempfile

d = pathlib.Path(tempfile.mkdtemp())
csv_path = d / "overrides.csv"
enrich.config.ARTIST_OVERRIDES_CSV = csv_path

csv_path.write_text(
    "artist_name,mbid,note,tags\n"
    "NoMbid,,MusicBrainz has no entry at all,drill|hip hop\n"
    "Pinned,e142ed6b-3b35-40e6-92fc-722bbb497dc1,right artist,pop\n"
    "BadUuid,not-a-uuid,typo,pop\n"
    "Suppressed,IGNORE,not an artist,\n"
    "NothingAtAll,,,\n", encoding="utf-8")
ov = enrich.load_overrides()

check("tags-only row is kept", enrich.normalise("NoMbid") in ov, True)
check("tags-only row pins nothing",
      ov[enrich.normalise("NoMbid")]["mbid"], None)
check("tags-only row carries its tags",
      ov[enrich.normalise("NoMbid")]["tags"], ["drill", "hip hop"])
check("a row with neither mbid nor tags is still skipped",
      enrich.normalise("NothingAtAll") in ov, False)
check("IGNORE still suppresses",
      ov[enrich.normalise("Suppressed")]["ignore"], True)
check("IGNORE pins nothing either",
      ov[enrich.normalise("Suppressed")]["mbid"], None)
check("a malformed MBID is still rejected",
      enrich.normalise("BadUuid") in ov, False)
check("a real MBID still pins",
      ov[enrich.normalise("Pinned")]["mbid"],
      "e142ed6b-3b35-40e6-92fc-722bbb497dc1")

# A tags-only row must never reach resolve_via_override — there is no MBID to
# resolve, and passing None would send a null into the MusicBrainz URL.
check("tags-only row is not mistaken for a pin",
      ov[enrich.normalise("NoMbid")]["mbid"] is None
      and not ov[enrich.normalise("NoMbid")]["ignore"], True)

# --------------------------------------------------------------------------
# NONE: a real artist with no MusicBrainz entry, whose name was auto-matched to
# somebody else BEFORE the row was written. A blank mbid cannot fix that: the
# tags-only path leaves resolution alone, the name is never searched again, and
# the hand tags end up hung on the wrong MBID. Three research-pass rows were in
# exactly this state — PLAT. on the vaporwave ＰＬＡＴ, Unconscious Mind on a
# Canadian black-metal band, emerge on a dark-ambient act — and the MBID is
# what seeds ListenBrainz, so discovery ran on a stranger's neighbours while
# the genres looked right.
# --------------------------------------------------------------------------
import contextlib
import io
import json
from collections import Counter

import duckdb

WRONG = "11111111-2222-4333-8444-555555555555"      # the other artists' MBIDs
WRONG2 = "66666666-7777-4888-9999-aaaaaaaaaaaa"
ORGANIC = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
PIN = "e142ed6b-3b35-40e6-92fc-722bbb497dc1"

enrich.CACHE_FILE = d / "artist_resolution.jsonl"
enrich.REVIEW_PARQUET = d / "artist_review.parquet"
enrich.RESOLUTION_PARQUET = d / "artist_resolution.parquet"
enrich.config.ARTIST_TAGS_PARQUET = d / "artist_tags.parquet"


class FakeResponse:
    status_code = 200
    def __init__(self, body):
        self._body = body
    def json(self):
        return self._body


class FakeHttp:
    """Serves the one MBID pin and counts everything, so a NONE row that spends
    a request, or a backfill that reaches a wrong MBID, shows up as a count."""
    def __init__(self):
        self.calls = Counter()
    def get(self, url, **kw):
        if url == enrich.MB_RELEASE_GROUP_URL:
            self.calls["release-group"] += 1
            return FakeResponse({"release-groups": [{"tags": [{"name": "pop"}]}]})
        if url.startswith(enrich.MB_SEARCH_URL + "/"):
            self.calls["lookup"] += 1
            return FakeResponse({"id": PIN, "name": "Pinned",
                                 "genres": [{"name": "pop", "count": 2}]})
        raise AssertionError(f"unexpected request: {url}")


def seed(recs):
    for name, rec in recs.items():
        enrich.append_cache({"artist_name": name, "score": None,
                             "matched_name": None, "n_candidates": 0, **rec})


def run(http, artists):
    """One enrich.py pass, in main()'s order, minus the search for new names."""
    cache = enrich.load_cache()
    overrides = enrich.load_overrides()
    enrich.purge_stale_overrides(cache, overrides)
    stats = enrich.apply_overrides(http, artists, cache, overrides)
    enrich.backfill_untagged(http, cache)
    enrich.apply_override_tags(cache, overrides, VOCAB)
    return cache, stats


ROWS = [
    "PlatLike,NONE,was matching a different artist; no correct entry exists,indie pop|pop",
    "BareNone,none,real artist; MusicBrainz has nothing,",
    "MixedCase,None,,pop",
    "Typo,NON,meant NONE,pop",
    "Suppressed,IGNORE,not an artist,",
    f"Pinned,{PIN},right artist,",
    "TagsOnly,,no MB entry,pop rap",
]
csv_path.write_text("artist_name,mbid,note,tags\n" + "\n".join(ROWS) + "\n",
                    encoding="utf-8")

out = io.StringIO()
with contextlib.redirect_stdout(out):
    ov = enrich.load_overrides()
k = enrich.normalise

check("NONE is accepted", ov.get(k("PlatLike"), {}).get("none"), True)
check("NONE pins nothing", ov.get(k("PlatLike"), {}).get("mbid", "missing"), None)
check("NONE is not IGNORE", ov.get(k("PlatLike"), {}).get("ignore"), False)
check("NONE keeps its tags", ov.get(k("PlatLike"), {}).get("tags"), ["indie pop", "pop"])
check("NONE with no tags is accepted, not skipped",
      ov.get(k("BareNone"), {}).get("none"), True)
check("lowercase none is accepted, tags empty", ov.get(k("BareNone"), {}).get("tags"), [])
check("mixed-case None is accepted", ov.get(k("MixedCase"), {}).get("none"), True)
check("NON is refused", k("Typo") in ov, False)
check("the refusal names NONE as a valid value",
      "'NON' is neither a UUID, IGNORE nor NONE" in out.getvalue(), True)
check("IGNORE is not NONE", ov.get(k("Suppressed"), {}).get("none"), False)
check("an MBID row is not NONE", ov.get(k("Pinned"), {}).get("none"), False)
check("a blank mbid is not upgraded to NONE", ov.get(k("TagsOnly"), {}).get("none"), False)

# The cache as the research pass found it: two names auto-matched to the wrong
# artist, one of them untagged (so the backfill would go and fetch the WRONG
# artist's release-group tags), plus the rows that must behave as before.
seed({
    "PlatLike": {"source": "musicbrainz", "status": "resolved", "mbid": WRONG,
                 "matched_name": "ＰＬＡＴ", "n_candidates": 3,
                 "tags": [{"tag": "vaporwave", "count": 5}]},
    "BareNone": {"source": "musicbrainz", "status": "resolved", "mbid": WRONG2,
                 "matched_name": "Bare None", "n_candidates": 1, "tags": []},
    "TagsOnly": {"source": "musicbrainz", "status": "not_found", "mbid": None, "tags": []},
    "Organic": {"source": "musicbrainz", "status": "resolved", "mbid": ORGANIC,
                "tags": [{"tag": "indie pop", "count": 4}]},
    "Abe Act": {"source": "musicbrainz", "status": "not_found", "mbid": None, "tags": []},
    "Zed Act": {"source": "musicbrainz", "status": "not_found", "mbid": None, "tags": []},
})
HOURS = {"Organic": 10, "Pinned": 6, "PlatLike": 5, "BareNone": 4, "MixedCase": 3,
         "TagsOnly": 2.5, "Zed Act": 1, "Abe Act": 1, "Suppressed": 0.5}
ARTISTS = list(HOURS)
before = enrich.CACHE_FILE.read_text(encoding="utf-8")

http = FakeHttp()
cache, stats = run(http, ARTISTS)

plat = cache["PlatLike"]
check("NONE over a wrong match: status no_entry", plat["status"], "no_entry")
check("NONE over a wrong match: the wrong MBID is gone", plat["mbid"], None)
check("NONE over a wrong match: override provenance", plat["source"], "override")
check("NONE over a wrong match: the hand tags, not the wrong artist's",
      [t["tag"] for t in plat["tags"]], ["indie pop", "pop"])
check("NONE without tags: no_entry and untagged",
      (cache["BareNone"]["status"], cache["BareNone"]["tags"]), ("no_entry", []))
check("NONE for a name never cached still answers it",
      cache.get("MixedCase", {}).get("status"), "no_entry")
check("NONE is counted under its own stat", stats.get("no_entry"), 3)
check("NONE costs no request, and the backfill never reaches the wrong MBID",
      dict(http.calls), {"lookup": 1})
appended = [json.loads(l)["artist_name"] for l in
            enrich.CACHE_FILE.read_text(encoding="utf-8")[len(before):].splitlines()]
check("NONE is never written to the cache; only the MBID pin is", appended, ["Pinned"])

# The rows that were already here behave exactly as before.
check("IGNORE unchanged", (cache["Suppressed"]["status"], stats["ignored"]), ("ignored", 1))
check("MBID pin unchanged",
      (cache["Pinned"]["status"], cache["Pinned"]["mbid"], cache["Pinned"]["source"]),
      ("resolved", PIN, "override"))
check("tags-only unchanged: resolution left alone, genres supplied",
      (cache["TagsOnly"]["status"], [t["tag"] for t in cache["TagsOnly"]["tags"]],
       stats["tags_only"]),
      ("not_found", ["pop rap"], 1))

# --------------------------------------------------------------------------
# no_entry is a resolution status, not an absence: the review list must not
# bring back a name the override file answered. Both review sites.
# --------------------------------------------------------------------------
con = duckdb.connect()
con.execute("CREATE TABLE artist_weight (artist_name VARCHAR, listening_hours DOUBLE)")
con.executemany("INSERT INTO artist_weight VALUES (?, ?)", list(HOURS.items()))
enrich.write_outputs(con, cache, VOCAB)

res = {n: (m, s, src) for n, m, s, src in duckdb.sql(
    f"SELECT artist_name, mbid, status, source FROM '{enrich.RESOLUTION_PARQUET}'"
).fetchall()}
check("artist_resolution: PlatLike has no MBID",
      res["PlatLike"], (None, "no_entry", "override"))
check("artist_tags: the hand tags carry no MBID either",
      duckdb.sql(f"SELECT DISTINCT mbid FROM '{enrich.config.ARTIST_TAGS_PARQUET}' "
                 "WHERE artist_name = 'PlatLike'").fetchall(), [(None,)])
review = [r[0] for r in duckdb.sql(
    f"SELECT artist_name FROM '{enrich.REVIEW_PARQUET}'").fetchall()]
# TagsOnly stays: a blank mbid says nothing about resolution, as before. Ties on
# hours break on the name, so the file is a total order.
check("artist_review.parquet excludes no_entry and ignored", review,
      ["TagsOnly", "Abe Act", "Zed Act"])

out = io.StringIO()
with contextlib.redirect_stdout(out):
    enrich.report(con)
listed = out.getvalue().split("REVIEW LIST", 1)[1].split("full review list", 1)[0]
check("report's review list excludes no_entry and ignored",
      [n for n in ("PlatLike", "BareNone", "MixedCase", "Suppressed") if n in listed], [])
check("report's review list still shows real gaps",
      all(n in listed for n in ("TagsOnly", "Abe Act", "Zed Act")), True)

# --------------------------------------------------------------------------
# Removing the NONE row lets the cached record show again — last write wins,
# exactly as with IGNORE, because neither answer is ever cached. That is
# deliberate: the row IS the answer, and deleting it withdraws the answer. Do
# not "fix" this into a purge; to un-resolve a name, keep its NONE row.
# --------------------------------------------------------------------------
csv_path.write_text("artist_name,mbid,note,tags\n"
                    + "\n".join(r for r in ROWS if not r.startswith("PlatLike")) + "\n",
                    encoding="utf-8")
http = FakeHttp()
cache, stats = run(http, ARTISTS)
check("row removed: the cached match shows again",
      (cache["PlatLike"]["status"], cache["PlatLike"]["mbid"]), ("resolved", WRONG))
check("row removed: costs no request", sum(http.calls.values()), 0)

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
