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

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
