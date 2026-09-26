"""tests/test_running_discovery.py — Stage 10 discovery: seeds and candidates.

No network. Synthetic tables, a canned ListenBrainz cache, and an http fake that
fails the test if anything reaches it.

The report prints how many candidates cleared the bar, and a seed query that
counts an artist eighteen times, or a ranking that hands the pool to whichever
seed has the biggest raw similarity numbers, prints a plausible count either
way. Todd Edwards is the case that made this file: one join to artist_tags per
TAG ROW lifted him from rank 31 to garage seed #17, and his ListenBrainz tail
supplied 13 of the 21 garage discovery tracks.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

# config resolves every path at import time, so point them all at a scratch
# directory FIRST. Nothing here may read real data or append to a real cache.
_TMP = Path(tempfile.mkdtemp(prefix="s10-discovery-"))
os.environ["SPOTIFY_DATA_DIR"] = str(_TMP / "data")
os.environ["SPOTIFY_CACHE_DIR"] = str(_TMP / "cache")
os.environ["SPOTIFY_RUNNING_OVERRIDES"] = str(_TMP / "running_overrides.csv")
(_TMP / "data").mkdir()
(_TMP / "cache").mkdir()

import duckdb
import config
import running

failures = []
def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")


class NoNetwork:
    """Every seed and candidate is cached, so any request is a bug."""
    def __init__(self):
        self.calls = []
    def get(self, url, params=None):
        self.calls.append((url, params))
        failures.append(f"network call with everything cached: {url} {params}")
        return None


N = running.normalise

con = duckdb.connect()
filler = ",\n".join(
    f"  ('Subtronics', 'm-subtronics', 'filler {i:02d}', 1, TRUE)"
    for i in range(17))
con.execute(f"""
CREATE TABLE artist_tags AS SELECT * FROM (VALUES
  -- EIGHTEEN tag rows for one artist: one cluster tag and seventeen others.
  -- Joining artist_tags per row multiplied his score by 18.
  ('Subtronics', 'm-subtronics', 'dubstep', 5, TRUE),
{filler},
  -- Two spellings, one MusicBrainz artist: one seed, not two.
  ('Skrillex',   'm-skrillex',   'dubstep', 4, TRUE),
  ('SKRILLEX',   'm-skrillex',   'dubstep', 4, TRUE),
  ('REAPER',     'm-reaper',     'dubstep', 3, TRUE),
  -- Artist-wide vetoed, under a second name that shares the MBID.
  ('SLANDER',    'm-slander',    'dubstep', 3, TRUE),
  ('SLANDER (alias)', 'm-slander', 'dubstep', 3, TRUE),
  -- Under the hours floor however well it scores.
  ('Small Act',  'm-small',      'dubstep', 3, TRUE),
  -- Equal scores: order must come from the mbid, not insertion.
  ('Zed Act',    'm-zzz',        'dubstep', 3, TRUE),
  ('Aa Act',     'm-aaa',        'dubstep', 3, TRUE),
  -- Unresolved: no MBID to ask ListenBrainz about.
  ('No Mbid',    NULL,           'dubstep', 3, TRUE)
) t(artist_name, mbid, tag, tag_count, is_genre)""")
running.build_artist_clusters(con)

# known_pool is built directly: this file is about what happens downstream of
# it, and test_running_selection.py pins how it is built.
con.execute("""
CREATE TABLE known_pool AS
SELECT spotify_track_uri, cluster, score::DOUBLE AS score, hours::DOUBLE AS hours
FROM (VALUES
  ('uri:s1', 'dubstep', 2.0, 3.0),
  ('uri:s2', 'dubstep', 1.0, 1.5),
  ('uri:k1', 'dubstep', 1.5, 2.0),
  ('uri:k2', 'dubstep', 0.5, 0.6),
  ('uri:rp', 'dubstep', 1.5, 2.0),
  ('uri:sl', 'dubstep', 1.2, 1.5),
  ('uri:sa', 'dubstep', 0.1, 0.2),
  ('uri:sm', 'dubstep', 9.0, 0.9),
  ('uri:zz', 'dubstep', 1.0, 1.2),
  ('uri:aa', 'dubstep', 1.0, 1.2),
  ('uri:nm', 'dubstep', 5.0, 5.0)
) t(spotify_track_uri, cluster, score, hours)""")
con.execute("""
CREATE TABLE track_credits AS SELECT * FROM (VALUES
  ('uri:s1', 'Subtronics', 'album_artist'),
  ('uri:s2', 'Subtronics', 'album_artist'),
  ('uri:k1', 'Skrillex', 'album_artist'),
  ('uri:k2', 'SKRILLEX', 'album_artist'),
  ('uri:rp', 'REAPER', 'album_artist'),
  ('uri:sl', 'SLANDER', 'album_artist'),
  ('uri:sa', 'SLANDER (alias)', 'album_artist'),
  ('uri:sm', 'Small Act', 'album_artist'),
  ('uri:zz', 'Zed Act', 'album_artist'),
  ('uri:aa', 'Aa Act', 'album_artist'),
  ('uri:nm', 'No Mbid', 'album_artist')
) t(spotify_track_uri, artist_name, credit_type)""")
con.execute("""
CREATE TABLE plays AS SELECT DISTINCT artist_name FROM track_credits""")

# --- C1: one row per seed artist ----------------------------------------
seeds = running.cluster_seed_artists(con, "dubstep", set())
by_mbid = {s["mbid"]: s for s in seeds}
check("seed rows carry mbid, name, score and hours",
      set(seeds[0]), {"mbid", "artist_name", "score", "hours"})
check("18 tag rows count once: score is the sum of his tracks",
      by_mbid["m-subtronics"]["score"], 3.0)
check("...and so are his hours", by_mbid["m-subtronics"]["hours"], 4.5)
check("two names with one MBID are one seed",
      sum(1 for s in seeds if s["mbid"] == "m-skrillex"), 1)
check("...with the summed score", by_mbid["m-skrillex"]["score"], 2.0)
check("...under the name carrying more of it",
      by_mbid["m-skrillex"]["artist_name"], "Skrillex")
check("0.9 window hours is not a seed", "m-small" in by_mbid, False)
check("an unresolved artist is not a seed",
      any(s["artist_name"] == "No Mbid" for s in seeds), False)
check("ranked by score, equal scores by mbid",
      [s["mbid"] for s in seeds],
      ["m-subtronics", "m-skrillex", "m-reaper", "m-slander", "m-aaa", "m-zzz"])

vetoes = {(N("SLANDER"), N(""))}
check("an artist-wide veto removes the seed",
      "m-slander" in {s["mbid"] for s in running.cluster_seed_artists(
          con, "dubstep", vetoes)}, False)
check("...matched on ANY name sharing the MBID",
      "m-slander" in {s["mbid"] for s in running.cluster_seed_artists(
          con, "dubstep", {(N("SLANDER (alias)"), N(""))})}, False)
check("a track-level veto does not remove a seed",
      "m-slander" in {s["mbid"] for s in running.cluster_seed_artists(
          con, "dubstep", {(N("SLANDER"), N("Some Track"))})}, True)
check("a seed in the other cluster is not offered here",
      running.cluster_seed_artists(con, "speed garage", set()), [])

# --- C6: normalised per seed, weighted by seed ------------------------
# Skrillex's ListenBrainz list tops out at 3955 and REAPER's at 181. Summing
# raw scores hands the whole pool to the hub: Skrillex's 20th neighbour (400)
# would outrank REAPER's best.
skrillex_list = [{"mbid": f"n-sk-{i:02d}", "name": f"Hub Friend {i:02d}",
                  "score": float(3955 - i * 187), "comment": ""}
                 for i in range(20)]
skrillex_list.insert(5, {"mbid": "n-tie-z", "name": "Tie Z", "score": 1000.0,
                         "comment": ""})
skrillex_list.insert(6, {"mbid": "n-tie-a", "name": "Tie A", "score": 1000.0,
                         "comment": ""})
# A known artist and an MBID-less row are never candidates.
skrillex_list.append({"mbid": "m-reaper", "name": "REAPER", "score": 3000.0,
                      "comment": ""})
skrillex_list.append({"mbid": "", "name": "Nobody", "score": 3000.0,
                      "comment": ""})
reaper_list = [{"mbid": f"n-rp-{i}", "name": f"Niche Friend {i}",
                "score": float(181 - i * 20), "comment": ""} for i in range(5)]
sim_cache = {
    "m-subtronics": {"seed_mbid": "m-subtronics", "similar": []},
    "m-skrillex":   {"seed_mbid": "m-skrillex", "similar": skrillex_list},
    "m-reaper":     {"seed_mbid": "m-reaper", "similar": reaper_list},
    "m-slander":    {"seed_mbid": "m-slander", "similar": [
        {"mbid": "n-sl", "name": "Slander Friend", "score": 50.0, "comment": ""}]},
    "m-aaa":        {"seed_mbid": "m-aaa", "similar": [
        {"mbid": "n-aa", "name": "Aa Friend", "score": 10.0, "comment": ""}]},
    "m-zzz":        {"seed_mbid": "m-zzz", "similar": [
        {"mbid": "n-zz", "name": "Zed Friend", "score": 10.0, "comment": ""}]},
}
all_neighbours = {x["mbid"] for rec in sim_cache.values()
                  for x in rec["similar"] if x["mbid"]}
tag_cache = {m: {"mbid": m, "tags": [{"tag": "dubstep", "count": 5}]}
             for m in all_neighbours}

http = NoNetwork()
config.RUN_DISCOVERY_SEEDS = 2
cands = running.cluster_candidates(con, http, "dubstep", tag_cache, sim_cache,
                                   set(), {(N("SLANDER"), N(""))})
order = [c["mbid"] for c in cands]
check("no request when every seed and candidate is cached", http.calls, [])
check("an empty ListenBrainz answer is skipped, the next seed used",
      {c["mbid"][:4] for c in cands}, {"n-sk", "n-ti", "n-rp"})
check("stops at RUN_DISCOVERY_SEEDS seeds with an answer",
      any(m in order for m in ("n-aa", "n-zz", "n-sl")), False)
check("REAPER's best neighbour outranks Skrillex's 20th",
      order.index("n-rp-0") < order.index("n-sk-19"), True)
# Used seeds: Skrillex (2.0) and REAPER (1.5); Subtronics answered [].
check("pooled score is seed share x (sim / that seed's max)",
      round(next(c for c in cands if c["mbid"] == "n-rp-0")["score"], 6),
      round(1.5 / 3.5, 6))
check("...for the hub too",
      round(next(c for c in cands if c["mbid"] == "n-sk-19")["score"], 6),
      round(2.0 / 3.5 * (3955 - 19 * 187) / 3955, 6))
check("equal pooled scores sort by mbid, not insertion",
      order.index("n-tie-a") + 1, order.index("n-tie-z"))
check("a known artist is never a candidate", "m-reaper" in order, False)
check("an MBID-less neighbour is never a candidate", "" in order, False)
check("candidates carry the share they cleared",
      {c["share"] for c in cands}, {1.0})

# With room for every seed, the vetoed one still asks nothing and offers
# nothing, and everything else stays offline.
config.RUN_DISCOVERY_SEEDS = 20
cands = running.cluster_candidates(con, http, "dubstep", tag_cache, sim_cache,
                                   set(), {(N("SLANDER"), N(""))})
order = [c["mbid"] for c in cands]
check("a vetoed seed's neighbours are not offered", "n-sl" in order, False)
check("every other answering seed contributes",
      {"n-aa", "n-zz"} <= set(order), True)
check("still no request", http.calls, [])

# A candidate below the stranger bar is dropped after tagging.
tag_cache["n-rp-0"] = {"mbid": "n-rp-0", "tags": [
    {"tag": "drum and bass", "count": 10}, {"tag": "liquid funk", "count": 3}]}
cands = running.cluster_candidates(con, http, "dubstep", tag_cache, sim_cache,
                                   set(), set())
check("a 0.77 stranger is refused", "n-rp-0" in {c["mbid"] for c in cands},
      False)

shutil.rmtree(_TMP, ignore_errors=True)
if failures:
    print(f"{len(failures)} FAILURE(S)")
    for f in failures:
        print("   ", f)
    sys.exit(1)
print("all assertions passed")
