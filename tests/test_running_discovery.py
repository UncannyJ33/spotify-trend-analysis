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

# --- Stage 10 no longer reads Stage 5 -----------------------------------
# The Stage 5 top-up's only garage contribution after the fixes above was
# Basement Jaxx — the failure cluster seeding was built to fix — and every
# dubstep passer was already seeded. It is gone, and so is the dependency: a
# data directory with no recommendations.parquet must be enough.
for view in ("plays", "artist_tags", "track_credits"):
    con.execute(f"COPY (SELECT * FROM {view}) TO "
                f"'{config.DATA_DIR / (view + '.parquet')}' (FORMAT PARQUET)")
check("the scratch data dir really lacks recommendations.parquet",
      config.RECOMMENDATIONS_PARQUET.exists(), False)
fresh_con = duckdb.connect()
try:
    running.register_sources(fresh_con)
    raised = None
except SystemExit as e:
    raised = str(e)
check("register_sources needs no recommendations.parquet", raised, None)
views = {r[0] for r in fresh_con.execute(
    "SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall()}
check("...registers what it does need",
      {"plays", "artist_tags", "track_credits"} <= views, True)
check("...and does not register recommendations", "recommendations" in views,
      False)
check("select_candidates is no longer imported",
      hasattr(running, "select_candidates"), False)

# --- C2/B4: every discovery pick is judged as a TRACK -------------------
# Stage 8's search cache kept a name and a URI per hit and relabelled every hit
# as the candidate, so nothing downstream could see who was actually on it.
# Tion Wayne's 2025 rap single "Crazy Love" went out as MJ Cole garage (a title
# collision with MJ Cole's 2000 record), the Wideboys slot went to a Bieber
# remix, "Everyday - Netsky Remix" inherited Rusko's own `everyday` recording,
# and DEM2's "Discoteca" went out as Dem 2. Each of those is a case below.
gcon = duckdb.connect()
gcon.execute("""
CREATE TABLE artist_tags AS SELECT * FROM (VALUES
  ('MJ Cole',         'uk garage',       3, TRUE),
  ('Sammy Virji',     'uk garage',       2, TRUE),
  ('Subtronics',      'dubstep',         5, TRUE),
  -- hip hop 2 against uk garage 1: drag outweighs his cluster weight.
  ('Tion Wayne',      'hip hop',         2, TRUE),
  ('Tion Wayne',      'uk garage',       1, TRUE),
  ('Justin Bieber',   'pop',            22, TRUE),
  -- Hand tags at count 1. `house` is on neither list, melodic dubstep is drag.
  ('Inéz',            'house',           1, TRUE),
  ('Inéz',            'melodic dubstep', 1, TRUE),
  ('Melodic Remixer', 'melodic dubstep', 3, TRUE),
  ('Melodic Remixer', 'future bass',     2, TRUE),
  -- Level is not drag: drag must OUTWEIGH the cluster weight.
  ('Even Act',        'dubstep',         2, TRUE),
  ('Even Act',        'pop',             2, TRUE)
) t(artist_name, tag, tag_count, is_genre)""")
running.build_artist_clusters(gcon)
drag = running.drag_artists(gcon)
check("drag_artists: drag outweighs both cluster weights, names normalised",
      drag, {N("Tion Wayne"), N("Justin Bieber"), N("Inéz"),
             N("Melodic Remixer")})


def trk(uri, name, ms, *artists):
    """A track as the credited cache returns it: Spotify's own credit list."""
    return {"track_name": name, "spotify_track_uri": uri, "duration_ms": ms,
            "artists": [{"name": a, "id": i} for a, i in artists]}


def gate(tracks, cand, pinned, on_genre=(), vetoes=(), is_fresh=None, k=10):
    rows = [dict(t, artist_name=cand) for t in tracks]
    return running.gate_discovery(
        rows, {"artist_name": cand, "mbid": "m-x"}, pinned, set(on_genre),
        drag, set(vetoes), is_fresh or (lambda r: True), k=k)


def uris(rows):
    return [r["spotify_track_uri"] for r in rows]


MJ, TION, LEO = ("MJ Cole", "sp-mj"), ("Tion Wayne", "sp-tion"), ("LeoStayTrill", "sp-leo")
SUB, INEZ, SV = ("Subtronics", "sp-sub"), ("Inéz", "sp-inez"), ("Sammy Virji", "sp-sv")

# (iii) A drag LEAD refuses the track. on_genre is empty here so relevance order
# applies and nothing but the drag test can refuse it.
crazy = trk("u:crazy", "Crazy Love", 190_000, TION, LEO, MJ)
sincere = trk("u:sincere", "Sincere", 230_000, MJ)
check("Crazy Love refused on MJ Cole: Tion Wayne is the drag lead",
      uris(gate([crazy, sincere], "MJ Cole", "sp-mj")), ["u:sincere"])
check("...and the title collision that let it in no longer helps",
      uris(gate([crazy], "MJ Cole", "sp-mj", on_genre={"crazylove"})), [])
wideboys = trk("u:bab", "Beauty And A Beat - Wideboys Radio Mix", 200_000,
               ("Justin Bieber", "sp-jb"), ("Nicki Minaj", "sp-nm"),
               ("Wideboys", "sp-wb"))
check("the Wideboys mix is refused: Bieber is the drag lead",
      uris(gate([wideboys], "Wideboys", "sp-wb")), [])

# A FEATURED vocalist is never drag-tested. Inéz carries melodic dubstep by
# hand, and she is on three records SJ already keeps.
ecd = trk("u:ecd", "Eyes Cut Deeper", 200_000, SUB, INEZ)
check("a drag-tagged featured vocalist does not refuse the track",
      uris(gate([ecd], "Subtronics", "sp-sub")), ["u:ecd"])
ecd_led = trk("u:ecd2", "Eyes Cut Deeper", 200_000, INEZ, SUB)
check("...the same track with her as lead is refused",
      uris(gate([ecd_led], "Subtronics", "sp-sub")), [])

# The named remixer is drag-tested when Spotify credits them on the record.
remixed = trk("u:rmx", "Song - Melodic Remixer Remix", 200_000, SV,
              ("Melodic Remixer", "sp-mr"))
check("a cluster-led track remixed by a drag artist is refused",
      uris(gate([remixed], "Sammy Virji", "sp-sv")), [])
uncredited = trk("u:rmx2", "Song - Melodic Remixer Remix", 200_000, SV)
check("...but a remixer named only by the title regex is not trusted to refuse",
      uris(gate([uncredited], "Sammy Virji", "sp-sv")), ["u:rmx2"])

# (vi) Length and live recordings, before choose_tracks spends a pick on them.
long_one = trk("u:long", "Bass Head", 384_000, SUB)
ok_one = trk("u:ok", "Ok Length", 264_000, SUB)
unknown = trk("u:unk", "No Length Yet", None, SUB)
check("6.4 min refused, 4.4 min kept, unknown left for the fallback",
      uris(gate([long_one, ok_one, unknown], "Subtronics", "sp-sub")),
      ["u:ok", "u:unk"])
check("a live recording is refused",
      uris(gate([trk("u:live", "Energy (Live at Red Rocks)", 200_000, SUB)],
                "Subtronics", "sp-sub")), [])

# (ii)/(iv) A genre flag is the candidate's own. Rusko's `everyday` recording
# is dubstep; Netsky's remix of it is not Rusko's work and must not inherit it.
everyday = trk("u:ev", "Everyday", 230_000, ("Rusko", "sp-rusko"))
netsky_mix = trk("u:evn", "Everyday - Netsky Remix", 250_000,
                 ("Rusko", "sp-rusko"), ("Netsky", "sp-netsky"))
got = gate([everyday], "Rusko", "sp-rusko", on_genre={"everyday"})
check("the candidate's own recording is genre-matched",
      [(r["spotify_track_uri"], r["genre_matched"]) for r in got],
      [("u:ev", True)])
check("'Everyday - Netsky Remix' is not matched on Rusko, so it is dropped",
      uris(gate([netsky_mix], "Rusko", "sp-rusko", on_genre={"everyday"})), [])
got = gate([netsky_mix], "Netsky", "sp-netsky", on_genre={"everyday"})
check("...but IS matched on Netsky, who the title names as remixer",
      [(r["spotify_track_uri"], r["genre_matched"]) for r in got],
      [("u:evn", True)])
featured_only = trk("u:fo", "Everyday", 230_000, ("Other Lead", "sp-ol"),
                    ("Rusko", "sp-rusko"))
check("a matched title led by someone else, with no remix credit, is not",
      uris(gate([featured_only], "Rusko", "sp-rusko", on_genre={"everyday"})),
      [])

# (i) With recording tags, only matched tracks; without, plain relevance.
a, b, c = (trk(f"u:{x}", x.upper(), 200_000, SUB) for x in "abc")
check("empty on_genre keeps relevance order",
      uris(gate([a, b, c], "Subtronics", "sp-sub")), ["u:a", "u:b", "u:c"])
check("...with every row unmatched",
      {r["genre_matched"] for r in gate([a, b, c], "Subtronics", "sp-sub")},
      {False})
check("non-empty on_genre keeps only matched tracks",
      uris(gate([a, b, c], "Subtronics", "sp-sub", on_genre={"b"})), ["u:b"])
check("choose_tracks still caps at k",
      uris(gate([a, b, c], "Subtronics", "sp-sub", k=2)), ["u:a", "u:b"])

# C3 on the discovery side: a veto tests every credit, features included.
gud = trk("u:gud", "GUD VIBRATIONS", 200_000, ("NGHTMRE", "sp-ng"),
          ("SLANDER", "sp-sl"))
other = trk("u:oth", "Other Record", 200_000, ("NGHTMRE", "sp-ng"),
            ("SLANDER", "sp-sl"))
check("a track crediting an artist-wide-vetoed name is dropped",
      uris(gate([gud, other], "NGHTMRE", "sp-ng",
                vetoes={(N("SLANDER"), "")})), [])
check("a (credited name, title) veto drops only that title",
      uris(gate([gud, other], "NGHTMRE", "sp-ng",
                vetoes={(N("SLANDER"), N("GUD VIBRATIONS"))})), ["u:oth"])
check("a track that is not fresh is dropped",
      uris(gate([gud, other], "NGHTMRE", "sp-ng",
                is_fresh=lambda r: r["spotify_track_uri"] != "u:gud")),
      ["u:oth"])
got = gate([ecd], "Subtronics", "sp-sub")
check("rows carry every credit as a LIST, never a joined string",
      got[0]["credited"], ["Subtronics", "Inéz"])
check("...and stay labelled as the candidate", got[0]["artist_name"],
      "Subtronics")

# --- namesakes: pin the candidate's Spotify artist id --------------------
dem = [trk("u:d1", "Discoteca", 200_000, ("DEM2", "sp-DEM2")),
       trk("u:d2", "Second", 200_000, ("DEM2", "sp-DEM2")),
       trk("u:d3", "Third", 200_000, ("DEM2", "sp-DEM2")),
       trk("u:d4", "Destiny", 200_000, ("Dem 2", "sp-dem-2")),
       trk("u:d5", "Dig", 200_000, ("Dem 2", "sp-dem-2"))]
pid, kept = running.pin_artist_id(dem, "Dem 2")
check("'Dem 2' pins the exact-name id over the one on more tracks",
      pid, "sp-dem-2")
check("...and DEM2's Discoteca is dropped", uris(kept), ["u:d4", "u:d5"])
pid, kept = running.pin_artist_id(dem, "Dem2")
check("with no exact name, the id on the most tracks wins", pid, "sp-DEM2")
pid, _ = running.pin_artist_id(
    [trk("u:t1", "One", 1, ("dem 2", "sp-first")),
     trk("u:t2", "Two", 1, ("DEM 2", "sp-second"))], "Dem2")
check("a remaining tie goes to relevance order", pid, "sp-first")
pid, _ = running.pin_artist_id(
    [trk("u:n1", "One", 1, ("INEZ", "sp-caps")),
     trk("u:n2", "Two", 1, ("INEZ", "sp-caps")),
     trk("u:n3", "Three", 1, ("Inéz", "sp-inez"))], "Inéz")
check("exact is compared after NFKD, so composed and decomposed agree",
      pid, "sp-inez")
check("no credit folding to the candidate pins nothing",
      running.pin_artist_id([trk("u:x", "X", 1, ("Someone", "sp-s"))], "Dem 2"),
      (None, []))

# --- remix credit: credits.py's pattern, imported, not copied -----------
for title, want in [("Everyday - Netsky Remix", "Netsky"),
                    ("Halsey - Ian Asher Remix", "Ian Asher"),
                    ("Bounce - Radio Edit", None),
                    ("War Pigs - 2012 - Remaster", None),
                    ("Song - 2019 Remix", None),
                    ("Plain Title", None)]:
    check(f"remix_credit({title!r})", running.remix_credit(title), want)
for title in ("Everyday - Netsky Remix", "Beauty And A Beat - Wideboys Radio Mix"):
    sql = gcon.execute(
        f"SELECT trim(regexp_extract(?, '{running.REMIX_CREDIT_RE}', 1, 'i'))",
        [title]).fetchone()[0]
    check(f"...Python agrees with Stage 1b's SQL on {title!r}",
          running.remix_credit(title), sql)

# --- the credited search cache: only answers are kept --------------------
class FakeSp:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []
    def get(self, path, params=None):
        self.calls.append((path, params))
        return self.resp


def item(uri, name, ms, *artists):
    return {"uri": uri, "name": name, "duration_ms": ms,
            "artists": [{"name": a, "id": i} for a, i in artists]}


def cache_lines():
    p = running.SP_TRACKS_CREDITED_CACHE
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


check("the credited cache is a new file, not Stage 8's",
      running.SP_TRACKS_CREDITED_CACHE.name,
      "spotify_artist_tracks_credited.jsonl")
cc = {}
check("a 429 returns nothing",
      running.sp_artist_tracks_credited(
          FakeSp({"_status": 429, "_body": "slow down"}), "Dem 2", cc), [])
check("...and appends nothing", (cache_lines(), cc), ([], {}))
check("no response returns nothing",
      running.sp_artist_tracks_credited(FakeSp(None), "Dem 2", cc), [])
check("...and appends nothing", (cache_lines(), cc), ([], {}))

sp = FakeSp({"tracks": {"items": []}})
check("an empty 200 returns nothing",
      running.sp_artist_tracks_credited(sp, "Nobody At All", cc), [])
check("...and IS cached, with its status",
      [running.json.loads(x) for x in cache_lines()],
      [{"key": N("Nobody At All"), "artist": "Nobody At All", "status": 200,
        "tracks": []}])
running.sp_artist_tracks_credited(sp, "Nobody At All", cc)
check("...so it is asked once", len(sp.calls), 1)

sp = FakeSp({"tracks": {"items": [
    item("spotify:track:d4", "Destiny", 250_000, ("Dem 2", "sp-dem-2")),
    item("spotify:track:k", "Tribute", 180_000, ("Karaoke Crew", "sp-k")),
    item(None, "Ghost", 180_000, ("Dem 2", "sp-dem-2")),
    item("spotify:track:d1", "Discoteca", 200_000, ("DEM2", "sp-DEM2"),
         ("Feature", "sp-f")),
]}})
live = running.sp_artist_tracks_credited(sp, "Dem 2", cc)
check("search is scoped to the artist, one page of SP_SEARCH_LIMIT",
      (sp.calls[0][0], sp.calls[0][1]["q"], sp.calls[0][1]["limit"]),
      ("/search", 'artist:"Dem 2"', running.SP_SEARCH_LIMIT))
check("wrong-artist and URI-less hits dropped, relevance order kept",
      uris(live), ["spotify:track:d4", "spotify:track:d1"])
check("every credit kept as {name, id}, in Spotify's order",
      live[1]["artists"], [{"name": "DEM2", "id": "sp-DEM2"},
                           {"name": "Feature", "id": "sp-f"}])
check("duration comes from search, costing no /tracks request",
      [t["duration_ms"] for t in live], [250_000, 200_000])
check("rows are labelled as the name asked for", {t["artist_name"] for t in live},
      {"Dem 2"})
cached = running.sp_artist_tracks_credited(sp, "Dem 2", cc)
check("a cache hit spends no request", len(sp.calls), 1)
check("...and has the same shape as a live call", cached, live)
reloaded = running.load_jsonl(running.SP_TRACKS_CREDITED_CACHE, "key")
check("the record survives a reload from disk",
      reloaded[N("Dem 2")]["tracks"], cc[N("Dem 2")]["tracks"])
check("Stage 8's search cache is never written",
      running.config.CACHE_DIR.joinpath("spotify_artist_tracks.jsonl").exists(),
      False)

# --- the loop: build_selections puts every pick through the gate --------
# Wiring only. Known pool empty, one garage stranger, recording tags absent so
# relevance applies; the gate itself is pinned above.
lcon = duckdb.connect()
lcon.execute("""
CREATE TABLE artist_tags AS SELECT * FROM (VALUES
  ('Tion Wayne', 'hip hop',   2, TRUE),
  ('Tion Wayne', 'uk garage', 1, TRUE)
) t(artist_name, tag, tag_count, is_genre)""")
running.build_artist_clusters(lcon)
lcon.execute("""
CREATE TABLE known_pool (spotify_track_uri VARCHAR, track_name VARCHAR,
    album_artist VARCHAR, cluster VARCHAR, duration_ms DOUBLE, hours DOUBLE,
    done_rate DOUBLE, n_plays BIGINT, score DOUBLE, uris VARCHAR[])""")
lcon.execute("""
CREATE TABLE track_credits (spotify_track_uri VARCHAR, artist_name VARCHAR,
    credit_type VARCHAR, credit_source VARCHAR)""")


class LoopSp:
    def __init__(self):
        self.calls = []
    def get(self, path, params=None):
        self.calls.append(path)
        if path == "/search":
            return {"tracks": {"items": [
                item("spotify:track:drag", "Crazy Love", 190_000,
                     ("Tion Wayne", "sp-tion"), ("Garage Stranger", "sp-gs")),
                item("spotify:track:ok", "Clean One", 200_000,
                     ("Garage Stranger", "sp-gs"), ("Feat Person", "sp-fp")),
                # No duration in the search hit: fetched, then capped too.
                item("spotify:track:nolen", "No Length", None,
                     ("Garage Stranger", "sp-gs")),
            ]}}
        if path == "/tracks/nolen":
            return {"duration_ms": 300_000}
        failures.append(f"unexpected Spotify call {path}")
        return None


saved = (running.cluster_candidates, running.load_genre_vocabulary,
         running.mb_genre_recordings)
running.cluster_candidates = lambda con, http, label, *a: (
    [{"artist_name": "Garage Stranger", "mbid": "m-gs", "score": 1.0,
      "share": 1.0}] if label == "speed garage" else [])
running.load_genre_vocabulary = lambda http: set()
running.mb_genre_recordings = lambda http, mbid, tags, cache: set()
loop_sp = LoopSp()
try:
    sels = running.build_selections(lcon, NoNetwork(), loop_sp)
finally:
    (running.cluster_candidates, running.load_genre_vocabulary,
     running.mb_genre_recordings) = saved
garage = next(s for s in sels if s["label"] == "speed garage")
new = [t for t in garage["tracks"] if t["slot"] == "discovery"]
check("the loop admits only what the gate passes, and B4 on a fetched length",
      uris(new), ["spotify:track:ok"])
check("the discovery row carries its credits", new[0]["credited"],
      ["Garage Stranger", "Feat Person"])
check("a search-supplied length costs no /tracks call; a missing one costs one",
      loop_sp.calls, ["/search", "/tracks/nolen"])

# --- the known side, end to end: C3, C8 and D1 through build_selections ---
# test_running_selection.py pins each filter; this pins the ORDER they run in
# inside build_selections, which is where a cap-before-filter bug lives.
kcon = duckdb.connect()
kcon.execute("""
CREATE TABLE artist_tags AS SELECT * FROM (VALUES
  ('Dillon Francis', CAST(NULL AS VARCHAR), 'dubstep',   3, TRUE),
  ('Four Act',       NULL,                  'dubstep',   3, TRUE),
  ('Other Act',      NULL,                  'dubstep',   3, TRUE),
  ('Garage Feat',    NULL,                  'uk garage', 3, TRUE)
) t(artist_name, mbid, tag, tag_count, is_genre)""")
running.build_artist_clusters(kcon)
kcon.execute("""
CREATE TABLE known_pool AS SELECT * FROM (VALUES
  ('uri:ddlm', 'Don''t Let Me Let Go', 'Dillon Francis', 'dubstep', 200000.0, 1.0, 1.0, 9::BIGINT, 0.90::DOUBLE, ['uri:ddlm']),
  -- Four Act's top track also carries a garage act (a poller-seen feature),
  -- so it sits in both pools and the garage run, built first, takes it.
  ('uri:f1', 'Four Tune 1', 'Four Act', 'speed garage', 200000.0, 1.0, 1.0, 9, 0.80, ['uri:f1']),
  ('uri:f1', 'Four Tune 1', 'Four Act', 'dubstep',      200000.0, 1.0, 1.0, 9, 0.80, ['uri:f1']),
  ('uri:f2', 'Four Tune 2', 'Four Act', 'dubstep',      200000.0, 1.0, 1.0, 9, 0.70, ['uri:f2']),
  ('uri:f3', 'Four Tune 3', 'Four Act', 'dubstep',      200000.0, 1.0, 1.0, 9, 0.60, ['uri:f3']),
  ('uri:f4', 'Four Tune 4', 'Four Act', 'dubstep',      200000.0, 1.0, 1.0, 9, 0.50, ['uri:f4']),
  -- In the Workout playlist, at 0.9x of Four Tune 4.
  ('uri:w',  'Workout Tune', 'Other Act', 'dubstep',    200000.0, 1.0, 1.0, 9, 0.45, ['uri:w'])
) t(spotify_track_uri, track_name, album_artist, cluster, duration_ms, hours,
    done_rate, n_plays, score, uris)""")
kcon.execute("""
CREATE TABLE track_credits AS SELECT * FROM (VALUES
  ('uri:ddlm', 'Dillon Francis', 'album_artist', 'export'),
  ('uri:ddlm', 'ILLENIUM',       'featured',     'poller'),
  ('uri:f1', 'Four Act',    'album_artist', 'export'),
  ('uri:f1', 'Garage Feat', 'featured',     'poller'),
  ('uri:f2', 'Four Act', 'album_artist', 'export'),
  ('uri:f3', 'Four Act', 'album_artist', 'export'),
  ('uri:f4', 'Four Act', 'album_artist', 'export'),
  ('uri:w',  'Other Act', 'album_artist', 'export')
) t(spotify_track_uri, artist_name, credit_type, credit_source)""")
kcon.execute("""
CREATE TABLE plays (spotify_track_uri VARCHAR, track_name VARCHAR,
    artist_name VARCHAR, played_seconds DOUBLE, reason_end VARCHAR, month DATE,
    ms_played BIGINT, ms_played_estimated BOOLEAN)""")
kcon.execute("CREATE TABLE plays_raw AS SELECT *, 'music' AS content_type FROM plays")


class WorkoutSp:
    """Answers only what prefer_members reads; `listing` None means the
    playlist list itself fails."""
    def __init__(self, listing):
        self.listing = listing
        self.calls = []
    def get(self, path, params=None):
        self.calls.append(path)
        if path == "/me/playlists":
            if self.listing is None:
                return {"_status": 503, "_body": "unavailable"}
            return {"items": self.listing, "next": None}
        if path.startswith("/playlists/pl-w/items"):
            return {"items": [{"added_at": "", "item": {
                "uri": "uri:w", "name": "Workout Tune",
                "artists": [{"name": "Other Act"}]}}], "next": None}
        if path == "/search":
            return {"tracks": {"items": []}}
        failures.append(f"unexpected Spotify call {path}")
        return None


def run_known(overrides: str, sp) -> dict:
    config.RUNNING_OVERRIDES_CSV.write_text(
        "playlist,decision,artist_name,track_name,note\n" + overrides,
        encoding="utf-8")
    saved = (running.cluster_candidates, running.load_genre_vocabulary)
    running.cluster_candidates = lambda *a: []
    running.load_genre_vocabulary = lambda http: set()
    try:
        sels = running.build_selections(kcon, NoNetwork(), sp)
    finally:
        running.cluster_candidates, running.load_genre_vocabulary = saved
        config.RUNNING_OVERRIDES_CSV.unlink()
    return {s["label"]: [t["spotify_track_uri"]
                         for t in sorted(s["tracks"], key=lambda t: t["position"])]
            for s in sels}


base = run_known("", WorkoutSp([]))
check("with no overrides the featured-ILLENIUM track is in dubstep",
      "uri:ddlm" in base["dubstep"], True)
check("garage, built first, takes the track both pools hold",
      base["speed garage"], ["uri:f1"])
check("...and the dubstep row it consumed promotes Four Act's 4th",
      [u for u in base["dubstep"] if u.startswith("uri:f")],
      ["uri:f2", "uri:f3", "uri:f4"])

vetoed_sel = run_known(",drop,ILLENIUM,,\n", WorkoutSp([]))
check("an artist-wide veto on a featured credit removes it from dubstep",
      "uri:ddlm" in vetoed_sel["dubstep"], False)
check("...leaving everything else where it was",
      vetoed_sel["dubstep"], [u for u in base["dubstep"] if u != "uri:ddlm"])

# A `prefer` row has no artist: the old "no artist, skip" guard would have
# thrown it away before it was read.
prefer_row = ",prefer,,Workout · Claude,D1 near-tie prior\n"
failing = WorkoutSp(None)
check("find_playlist failing leaves the selection identical to no prefer row",
      run_known(prefer_row, failing), base)
check("...having asked, and warned rather than exited",
      failing.calls.count("/me/playlists") >= 1, True)
boosted = run_known(prefer_row, WorkoutSp([{"id": "pl-w", "name": "Workout · Claude"}]))
check("a member within the margin now leads the non-member it trailed",
      boosted["dubstep"].index("uri:w") < boosted["dubstep"].index("uri:f4"), True)
check("...and only the order changed",
      sorted(boosted["dubstep"]), sorted(base["dubstep"]))
check("a veto still removes a member",
      "uri:w" in run_known(prefer_row + ",drop,Other Act,Workout Tune,\n",
                           WorkoutSp([{"id": "pl-w", "name": "Workout · Claude"}])
                           )["dubstep"], False)
check("a commented-out example row is inert",
      run_known("# ,drop,ILLENIUM,,example\n", WorkoutSp([])), base)

shutil.rmtree(_TMP, ignore_errors=True)
if failures:
    print(f"{len(failures)} FAILURE(S)")
    for f in failures:
        print("   ", f)
    sys.exit(1)
print("all assertions passed")
