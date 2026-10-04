"""tests/test_playlist_seeds.py — seeded discovery candidates: never-guess resolution,
per-seed pooling, the shared serving bar. No network."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

# config resolves every path at import time, so point them all at a scratch
# directory FIRST. Nothing here may read real data or append to a real cache.
_TMP = Path(tempfile.mkdtemp(prefix="s8-seeds-"))
os.environ["SPOTIFY_DATA_DIR"] = str(_TMP / "data")
os.environ["SPOTIFY_CACHE_DIR"] = str(_TMP / "cache")
os.environ["SPOTIFY_PLAYLIST_OVERRIDES"] = str(_TMP / "playlist_overrides.csv")
(_TMP / "data").mkdir()
(_TMP / "cache").mkdir()

import contextlib
import io
import json

import duckdb
import config
import playlists

failures = []
def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")


con = duckdb.connect()
con.execute("""CREATE TABLE plays AS SELECT * FROM (VALUES ('Joji','Glimpse of Us','u1')
) t(artist_name, track_name, spotify_track_uri)""")
con.execute("""CREATE TABLE artist_tags AS SELECT * FROM (VALUES
  ('Joji', 'm-joji', 'lo-fi hip hop', 3, true, 'musicbrainz')
) t(artist_name, mbid, tag, tag_count, is_genre, source)""")
known = {playlists.normalise("Joji")}

asked = []
def fake_resolve(http, name):
    asked.append(name)
    return {"FKJ": {"artist_name": "FKJ", "status": "ambiguous", "mbid": None},
            "Nujabes": {"artist_name": "Nujabes", "status": "resolved", "mbid": "m-nuj"},
            "Flaky": {"artist_name": "Flaky", "status": "error", "mbid": None},
            }[name]
playlists.resolve_via_musicbrainz = fake_resolve

out = io.StringIO()
with contextlib.redirect_stdout(out):
    r_lib = playlists.resolve_seed(con, None, "joji", {}, {})
    r_pin = playlists.resolve_seed(con, None, "keshi=0a1b2c3d-0000-4000-8000-000000000000", {}, {})
    mine = {}
    r_amb = playlists.resolve_seed(con, None, "FKJ", {}, mine)
    r_mb  = playlists.resolve_seed(con, None, "Nujabes", {}, mine)
    r_err = playlists.resolve_seed(con, None, "Flaky", {}, mine)
    asked_before = len(asked)
    r_mb2 = playlists.resolve_seed(con, None, "Nujabes", {}, mine)
    # artist_overrides.csv outranks every cache. Neither name is in fake_resolve,
    # so a wrongly spent search fails loudly (KeyError) rather than passing.
    ov = {"plat": {"mbid": None, "ignore": False, "none": True, "tags": [], "note": ""},
          "wale": {"mbid": "ab2528dd-0000-4000-8000-000000000000", "ignore": False,
                   "none": False, "tags": [], "note": ""},
          "tagsonly": {"mbid": None, "ignore": False, "none": False,
                       "tags": ["ambient"], "note": ""}}
    r_none = playlists.resolve_seed(
        con, None, "PLAT.", {"PLAT.": {"artist_name": "PLAT.", "status": "resolved",
                                       "mbid": "m-wrong"}}, {}, overrides=ov)
    r_ov = playlists.resolve_seed(con, None, "Wale", {}, {}, overrides=ov)
    r_tags_only = playlists.resolve_seed(
        con, None, "TagsOnly", {"TagsOnly": {"artist_name": "TagsOnly",
                                             "status": "resolved", "mbid": "m-to"}},
        {}, overrides=ov)
check("library seed resolves from artist_tags, case-folded",
      r_lib, {"artist_name": "Joji", "mbid": "m-joji"})
check("a NONE in artist_overrides.csv beats Stage 2's raw cache (PLAT.)", r_none, None)
check("...and says so", "PLAT." in out.getvalue() and "artist_overrides" in out.getvalue(), True)
check("an MBID in artist_overrides.csv is used, no search spent",
      r_ov, {"artist_name": "Wale", "mbid": "ab2528dd-0000-4000-8000-000000000000"})
check("a tags-only override row leaves resolution alone",
      r_tags_only, {"artist_name": "TagsOnly", "mbid": "m-to"})
check("override paths spend no search", len(asked), asked_before)
check("Name=MBID used as given",
      r_pin, {"artist_name": "keshi", "mbid": "0a1b2c3d-0000-4000-8000-000000000000"})
check("ambiguous seed skipped, not guessed", r_amb, None)
check("warning names the seed and the remedy",
      "FKJ" in out.getvalue() and "Name=MBID" in out.getvalue(), True)
check("resolved seed from MusicBrainz", r_mb, {"artist_name": "Nujabes", "mbid": "m-nuj"})
check("error is not cached", "Flaky" in mine, False)
check("an answered seed is not asked twice", len(asked), asked_before)
check("ambiguous IS cached (it is an answer)", mine.get("FKJ", {}).get("status"), "ambiguous")
check("seed cache file written for answers only",
      sorted(json.loads(l)["artist_name"]
             for l in playlists.PLAYLIST_SEED_CACHE.read_text().splitlines()),
      ["FKJ", "Nujabes"])

# --- fold-alike library names; error records do not shadow good ones --------
con2 = duckdb.connect()
con2.execute("""CREATE TABLE artist_tags AS SELECT * FROM (VALUES
  ('Dream', 'mbid-dream', 'eurobeat', 3, true, 'musicbrainz'),
  ('The-Dream', 'mbid-thedream', 'contemporary r&b', 3, true, 'musicbrainz')
) t(artist_name, mbid, tag, tag_count, is_genre, source)""")
asked_before = len(asked)
out3 = io.StringIO()
with contextlib.redirect_stdout(out3):
    r_exact = playlists.resolve_seed(con2, None, "The-Dream", {}, {})
    r_fold2 = playlists.resolve_seed(con2, None, "the dream", {}, {})
    mine_ok = {"Okay": {"artist_name": "Okay", "status": "resolved", "mbid": "m-okay"}}
    r_shadow = playlists.resolve_seed(
        con2, None, "Okay", {"Okay": {"artist_name": "Okay", "status": "error"}}, mine_ok)
check("exact library name wins over a fold-alike", r_exact,
      {"artist_name": "The-Dream", "mbid": "mbid-thedream"})
check("a seed folding to two library artists is refused", r_fold2, None)
check("...naming both and the Name=MBID remedy",
      all(x in out3.getvalue() for x in ("Dream", "The-Dream", "Name=MBID")), True)
check("neither spent a MusicBrainz call", len(asked), asked_before)
check("a Stage 2 error record does not shadow a good own-cache answer",
      r_shadow, {"artist_name": "Okay", "mbid": "m-okay"})

# --- pooling ---------------------------------------------------------------
# A real UUID: "Dead=m-dead" would be refused at the Name=MBID check
# (enrich.MBID_RE, verified) and the no-neighbours path never run.
DEAD = "00000000-0000-4000-8000-00000000dead"
sim = {
  "m-joji": [{"mbid": "m-hub", "name": "Hub", "score": 4000.0},
             {"mbid": "m-a", "name": "Alpha", "score": 2000.0},
             {"mbid": "m-drake", "name": "Drake", "score": 3000.0}],
  "m-nuj":  [{"mbid": "m-b", "name": "Beta", "score": 180.0},
             {"mbid": "m-off", "name": "OffGenre", "score": 170.0}],
  DEAD: [],
}
playlists.fetch_similar = lambda http, mbid, cache: sim[mbid]
tags = {"m-hub": [{"tag": "lo-fi hip hop", "count": 4}],
        "m-a":   [{"tag": "lo-fi hip hop", "count": 2}],
        "m-b":   [{"tag": "trip hop", "count": 3}],
        "m-off": [{"tag": "lo-fi hip hop", "count": 1}, {"tag": "metalcore", "count": 30}],
        "m-nuj": [{"tag": "lo-fi hip hop", "count": 5}]}
tag_cache = {}
def fake_tags(http, mbid, vocab, cache):
    cache[mbid] = {"mbid": mbid, "tags": tags.get(mbid, []), "status": 200}
    return cache[mbid]["tags"]
playlists.fetch_candidate_tags = fake_tags

spec = {"label": "lo-fi", "tags": ["lo-fi hip hop", "trip hop"],
        "seeds": ["Joji", "Nujabes"], "exclude": ["Drake"]}
mine = {"Nujabes": {"artist_name": "Nujabes", "status": "resolved", "mbid": "m-nuj"}}
got = playlists.seed_candidates(con, None, spec, sim_cache={}, tag_cache=tag_cache,
                                stage2={}, mine=mine, vocab=set(), known=known)
names = [c["artist_name"] for c in got]
check("unheard seed is itself a candidate, ranked first", names[0], "Nujabes")
check("library seed is never a discovery candidate", "Joji" in names, False)
check("excluded neighbour never pooled", "Drake" in names, False)
check("off-genre neighbour refused by the same share bar", "OffGenre" in names, False)
check("per-seed normalisation: Nujabes' best (180) ties Joji's hub (4000)",
      {c["artist_name"]: round(c["score"], 6) for c in got}["Beta"],
      {c["artist_name"]: round(c["score"], 6) for c in got}["Hub"])

spec2 = dict(spec, seeds=["Joji", f"Dead={DEAD}"])
out2 = io.StringIO()
with contextlib.redirect_stdout(out2):
    got2 = playlists.seed_candidates(con, None, spec2, sim_cache={}, tag_cache=tag_cache,
                                     stage2={}, mine={}, vocab=set(), known=known)
check("a seed with no neighbours does not sink the rest",
      [c["artist_name"] for c in got2][:2], ["Hub", "Alpha"])
check("the unheard, untagged seed is refused by the bar, not offered",
      "Dead" in [c["artist_name"] for c in got2], False)
check("...and both facts are said", "no neighbours" in out2.getvalue()
      and "do not serve" in out2.getvalue(), True)
check("no seeds -> []", playlists.seed_candidates(
      con, None, dict(spec, seeds=[]), sim_cache={}, tag_cache={}, stage2={},
      mine={}, vocab=set(), known=known), [])

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
