"""tests/test_consolidate.py — Stage 9 scoring, against synthetic tag tables.

No network, no Spotify. The run report is too coarse to verify scoring on its
own: it prints counts, and a filter that drops the right *number* of tracks
while dropping the wrong ones looks identical in it. So the cases that matter
are pinned here — above all the two real artists that break the naive rules,
Daft Punk and Kendrick Lamar.
"""
import csv
import io
import contextlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")
import duckdb

import config
import consolidate
from consolidate import DROP, KEEP, REVIEW

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")


def close(label, got, want, tol=1e-3):
    ok = got is not None and abs(got - want) <= tol
    if not ok:
        failures.append(f"{label}: got {got!r}, want ~{want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")


def tags_con(rows):
    """An in-memory `artist_tags` view from (artist, tag, count) triples."""
    con = duckdb.connect()
    con.execute("CREATE TABLE artist_tags ("
                "artist_name VARCHAR, mbid VARCHAR, tag VARCHAR, "
                "tag_count INTEGER, is_genre BOOLEAN, source VARCHAR)")
    con.executemany(
        "INSERT INTO artist_tags VALUES (?, 'mbid', ?, ?, TRUE, 'test')",
        [(a, t, c) for a, t, c in rows])
    return con


def track(name, artists, uri="spotify:track:x", added="2021-01-01T00:00:00Z"):
    return {"track_name": name, "artists": artists, "spotify_track_uri": uri,
            "added_at": added}


def weights():
    return consolidate.artist_family_weights(tags_con([
        ("Skrillex", "dubstep", 40), ("Skrillex", "hip hop", 5),
        ("Rapper", "gangsta rap", 50),
        ("Kendrick Lamar", "hip hop", 58), ("Kendrick Lamar", "electronic", 2),
        ("Rocker", "indie rock", 20),
    ]))


print("\ntag families — precedence and the blocklist")

# `hardcore hip hop` matches BOTH pattern lists and carries 48 artists in the
# real library. Testing electronic first would quietly rescue every one.
check("overlapping tag resolves to rap, not edm",
      consolidate.tag_family("hardcore hip hop"), "rap")
check("hip house is electronic (no 'hip hop' substring)",
      consolidate.tag_family("hip house"), "edm")

for tag in ("garage rock", "garage rock revival", "hardcore punk",
            "post-hardcore", "metalcore"):
    check(f"blocklisted {tag!r} is off-family", consolidate.tag_family(tag), None)
for tag in ("uk garage", "speed garage", "future garage", "hard techno",
            "bass house", "drum and bass"):
    check(f"{tag!r} is electronic", consolidate.tag_family(tag), "edm")

# `dance` is deliberately not a pattern: it would match `dancehall`.
check("dancehall is not electronic", consolidate.tag_family("dancehall"), None)
for tag in ("heavy metal", "indie rock", "soul", "pop"):
    check(f"off-family {tag!r}", consolidate.tag_family(tag), None)


print("\nthe two artists that break the naive rules")

# Daft Punk carries a rap tag, so a veto list deletes them. Kendrick carries an
# electronic tag, so an allow list keeps him. This is why it is a balance.
w = consolidate.artist_family_weights(tags_con([
    ("Daft Punk", "hip hop", 1), ("Daft Punk", "french house", 60),
    ("Daft Punk", "electronic", 32),
    ("Kendrick Lamar", "hip hop", 40), ("Kendrick Lamar", "gangsta rap", 18),
    ("Kendrick Lamar", "electronic", 2),
]))
close("Daft Punk scores ~0.01 rap", w["daftpunk"]["rap_share"], 1 / 93)
check("Daft Punk is kept", consolidate.decide(w["daftpunk"]["rap_share"]), KEEP)
close("Kendrick scores ~0.97 rap", w["kendricklamar"]["rap_share"], 58 / 60)
check("Kendrick is dropped",
      consolidate.decide(w["kendricklamar"]["rap_share"]), DROP)

# Votes go negative on downvotes and are clamped at 0; an artist whose family
# tags all sit at zero would score 0/0 and be called off-family despite being
# clearly tagged. Same failure MIN_TAG_COUNT_FOR_ANCHOR describes.
zw = consolidate.artist_family_weights(
    tags_con([("Zero Act", "techno", 0), ("Zero Act", "house", 0)]))
check("zero-vote tags fall back to presence", zw["zeroact"]["rap_share"], 0.0)
check("zero-vote techno act is kept",
      consolidate.decide(zw["zeroact"]["rap_share"]), KEEP)

nw = consolidate.artist_family_weights(tags_con([
    ("Odd", "hip hop", -5), ("Odd", "gangsta rap", 10), ("Odd", "techno", 0)]))
check("negative counts clamp rather than subtract", nw["odd"]["rap_w"], 10.0)


print("\nfeature weighting — worst credit wins, features at half")

W = weights()
share, reason, _ = consolidate.track_rap_share(["Skrillex", "Rapper"], W)
close("rap feature under electronic primary scores 1.0 * 0.5", share, 0.5)
check("...which is review, not drop", consolidate.decide(share), REVIEW)
check("...with no 'unscorable' reason attached", reason, "")

share, _, _ = consolidate.track_rap_share(["Kendrick Lamar", "Skrillex"], W)
check("rap primary still drops despite an electronic feature",
      consolidate.decide(share), DROP)

primary_only, _, _ = consolidate.track_rap_share(["Skrillex"], W)
with_feature, _, _ = consolidate.track_rap_share(["Skrillex", "Rapper"], W)
check("primary alone is kept", consolidate.decide(primary_only), KEEP)
check("a rap feature raises the score", with_feature > primary_only, True)

share, reason, _ = consolidate.track_rap_share(["Never Heard Of Them"], W)
check("unknown artist is unscorable", share, None)
check("...reported as untagged", reason.startswith("untagged"), True)
share, reason, _ = consolidate.track_rap_share(["Rocker"], W)
check("rock artist is unscorable", share, None)
check("...reported as off-family, a different question for a human",
      reason.startswith("off-family"), True)

# The discount protects a KNOWN primary from a guest. With the primary untagged
# there is nothing to protect, and applying it anyway scores every rap track
# with an unknown lead at exactly 1.0 * 0.5 = 0.5 — review instead of drop. The
# first real run rescued 15 UK rap tracks this way ("Dave, Central Cee -
# Sprinter"), every one of them plainly rap.
share, reason, note = consolidate.track_rap_share(["Unknown Lead", "Rapper"], W)
close("untagged primary suspends the discount", share, 1.0)
check("...so the rap feature drops the track", consolidate.decide(share), DROP)
check("...and the report says why", "primary untagged" in note, True)

share, _, _ = consolidate.track_rap_share(["Skrillex", "Rapper"], W)
close("a KNOWN primary still gets the guest discounted", share, 0.5)
check("...leaving the genuine co-headline call to a human",
      consolidate.decide(share), REVIEW)

_saved = config.CONSOLIDATE_FEATURE_WEIGHT
config.CONSOLIDATE_FEATURE_WEIGHT = 1.0
share, _, _ = consolidate.track_rap_share(["Skrillex", "Rapper"], W)
close("feature weight is read from config, not hardcoded", share, 1.0)
config.CONSOLIDATE_FEATURE_WEIGHT = _saved


print("\ndedupe — folded title, not URI")

# URI-dedupe alone gives one song several slots: Stage 8's first dry run
# produced "Papa Roach — Last Resort" twice exactly this way.
pressings = [track("Last Resort", ["Papa Roach"], "spotify:track:1"),
             track("Last Resort - 2020 Remaster", ["Papa Roach"], "spotify:track:2"),
             track("Last Resort (Radio Edit)", ["Papa Roach"], "spotify:track:3")]
check("three pressings fold to one slot",
      len({consolidate.dedupe_key(r) for r in pressings}), 1)
check("different songs stay distinct", len({
    consolidate.dedupe_key(track("Breathe", ["The Prodigy"])),
    consolidate.dedupe_key(track("Firestarter", ["The Prodigy"])),
    consolidate.dedupe_key(track("Breathe", ["Télépopmusik"])),
}), 3)

new = [track("Shiver", ["John Summit"], "spotify:track:new"),
       track("Turn off the Lights", ["Chris Lake"], "spotify:track:n2")]
scored = [dict(track("Shiver - Extended Mix", ["John Summit"], "spotify:track:old"),
               rap_share=0.0, reason="", tag_note="", verdict=KEEP, overridden=False)]
merged, review = consolidate.build(new, scored)
check("newer playlist wins the tie, keeping its pressing and position",
      [r["spotify_track_uri"] for r in merged],
      ["spotify:track:new", "spotify:track:n2"])
check("nothing sent to review", review, [])


print("\noverrides — a hand answer outranks the score")

_tmp = Path(tempfile.mkdtemp())
_saved_ov, _saved_rv = (config.CONSOLIDATE_OVERRIDES_CSV,
                        config.CONSOLIDATE_REVIEW_CSV)
config.CONSOLIDATE_OVERRIDES_CSV = _tmp / "consolidate_overrides.csv"
config.CONSOLIDATE_REVIEW_CSV = _tmp / "consolidate_review.csv"

config.CONSOLIDATE_OVERRIDES_CSV.write_text(
    "decision,artist_name,track_name,note\n"
    "# a comment row, with a comma in it, must not parse as an override\n"
    "keep,KENDRICK LAMAR,HUMBLE. - 2018 Remaster,rescued by hand\n",
    encoding="utf-8")
ov = consolidate.load_overrides()
check("comment row skipped, one override loaded", len(ov), 1)

got = consolidate.classify([track("HUMBLE.", ["Kendrick Lamar"])], W, ov)
check("override beats the computed drop", got[0]["verdict"], KEEP)
check("...and is flagged as overridden", got[0]["overridden"], True)
check("...while the underlying score still says drop",
      got[0]["rap_share"] > config.CONSOLIDATE_DROP_ABOVE, True)

config.CONSOLIDATE_OVERRIDES_CSV.write_text(
    "decision,artist_name,track_name,note\nmaybe,Diplo,Revolution,typo\n",
    encoding="utf-8")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    bad = consolidate.load_overrides()
check("a typo'd decision is refused, not guessed", bad, {})
check("...loudly", "neither keep nor drop" in buf.getvalue(), True)


print("\nreview file — what still needs a human")

rows = [track("Collab", ["Skrillex", "Rapper"]), track("Rock Song", ["Rocker"])]
_, review = consolidate.build([], consolidate.classify(rows, W, {}))
consolidate.write_review(review)
with config.CONSOLIDATE_REVIEW_CSV.open(encoding="utf-8") as fh:
    got = list(csv.DictReader(fh))
check("both unresolved tracks listed",
      {r["track_name"] for r in got}, {"Collab", "Rock Song"})
check("decision column left blank for a human",
      all(r["decision"] == "" for r in got), True)
check("off-family reason carried through",
      any("off-family" in r["reason"] for r in got), True)
check("added_at carried through", all(r["added_at"] for r in got), True)

config.CONSOLIDATE_OVERRIDES_CSV, config.CONSOLIDATE_REVIEW_CSV = _saved_ov, _saved_rv


print("\nthe standing invariant")

# Spotify has no delete-playlist API at all, only unfollow. Stage 8's client
# implements no delete verb on purpose; Stage 9 borrows it and must not grow one.
from playlists import Spotify  # noqa: E402

check("the borrowed client has no delete verb", hasattr(Spotify, "delete"), False)
check("this stage issues no DELETE",
      "DELETE" in Path(consolidate.__file__).read_text(encoding="utf-8"), False)


print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  {f}")
    sys.exit(1)
print("all checks passed")
