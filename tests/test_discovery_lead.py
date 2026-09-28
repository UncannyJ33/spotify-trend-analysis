"""tests/test_discovery_lead.py — Stage 8 discovery offers only the candidate's
own records.

No network: Spotify is a fake, MusicBrainz is stubbed, the tables are synthetic.

SJ's review of "indie frontier · Claude" found ILLENIUM in it. RUNN, a
discovery candidate, came back from Spotify's search with "Free Fall", an
ILLENIUM record featuring RUNN (credits: ILLENIUM, RUNN), and Stage 8 offered
it as RUNN's work. Its old search cache kept a name and a URI per hit, so it
could not see who led a track. A count in the report looks the same either way;
only asserting on WHICH tracks survive catches it.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

# config resolves every path at import time, so point them all at a scratch
# directory FIRST. Nothing here may read real data or append to a real cache.
_TMP = Path(tempfile.mkdtemp(prefix="s8-lead-"))
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


def trk(uri, name, *artists):
    """A credited-cache row: every credit as {name, id}, lead first."""
    return {"spotify_track_uri": uri, "track_name": name, "duration_ms": 200_000,
            "artists": [{"name": a, "id": i} for a, i in artists]}


def uris(rows):
    return [r["spotify_track_uri"] for r in rows]


RUNN = ("RUNN", "sp-runn")

# --- candidate_led: lead or named remixer, nothing else --------------------
free_fall = trk("u:ff", "Free Fall", ("ILLENIUM", "sp-ill"), RUNN)
own = trk("u:own", "Falling Apart", RUNN)
own_remix = trk("u:rmx", "Wires - RUNN Remix", ("Someone Else", "sp-se"), RUNN)
led_other_remix = trk("u:triv", "Alive - Trivecta Remix", RUNN,
                      ("Trivecta", "sp-triv"))
alive = trk("u:alive", "Alive", RUNN)

check("a feature (candidate credited second) is refused",
      uris(playlists.candidate_led([free_fall], "RUNN", "sp-runn")), [])
check("a record the candidate leads is kept",
      uris(playlists.candidate_led([own], "RUNN", "sp-runn")), ["u:own"])
check("the candidate's own remix of someone else is kept, billed second or not",
      uris(playlists.candidate_led([own_remix], "RUNN", "sp-runn")), ["u:rmx"])
check("a candidate-led remix passes the lead test...",
      uris(playlists.candidate_led([led_other_remix], "RUNN", "sp-runn")),
      ["u:triv"])
check("...and discovery_eligible then refuses it as someone else's remix",
      uris(playlists.discovery_eligible(
          playlists.candidate_led([led_other_remix], "RUNN", "sp-runn"), "RUNN")),
      [])
page = [led_other_remix, free_fall, own_remix, own, alive]
check("relevance order survives the filter",
      uris(playlists.candidate_led(page, "RUNN", "sp-runn")),
      ["u:triv", "u:rmx", "u:own", "u:alive"])
check("no pinned id offers nothing",
      playlists.candidate_led(page, "RUNN", None), [])
check("a remix title naming the candidate does not stand in for their id",
      uris(playlists.candidate_led(
          [trk("u:x", "Tune - RUNN Remix", ("Someone Else", "sp-se"),
               ("RUNN", "sp-other-runn"))], "RUNN", "sp-runn")), [])

# --- namesakes: the pin decides who "leads" --------------------------------
# DEM2 and Dem 2 both fold to `dem2`. The Dem 2 search returned DEM2's
# "Discoteca" and it went out as Dem 2 discovery (Stage 10, first); Stage 8
# reads the same cache, so it inherits the same trap.
dem_page = [
    trk("u:disc", "Discoteca", ("DEM2", "sp-DEM2")),
    trk("u:dfeat", "Feat Thing", ("DEM2", "sp-DEM2"), ("Dem 2", "sp-dem-2")),
    trk("u:dest", "Destiny", ("Dem 2", "sp-dem-2")),
    trk("u:drmx", "Big Tune - DEM2 Remix", ("Someone Else", "sp-se"),
        ("DEM2", "sp-DEM2")),
]
pid, kept = playlists.pin_artist_id(dem_page, "Dem 2")
check("'Dem 2' pins its own id, not the namesake's", pid, "sp-dem-2")
check("the namesake's record, its lead and its remix are all not taken",
      uris(playlists.candidate_led(kept, "Dem 2", pid)), ["u:dest"])
check("...even handed the unpinned page, only the pinned id leads",
      uris(playlists.candidate_led(dem_page, "Dem 2", pid)), ["u:dest"])

# --- the loop: build_selections puts every candidate through it ------------
con = duckdb.connect()
con.execute("""
CREATE TABLE genre_gaps AS SELECT * FROM (VALUES
  ('indie', 0.0005, 30.0, 12, 0.9)
) t(tag, gap_score, hours, n_artists, rel_change_per_year)""")
con.execute("""
CREATE TABLE plays AS SELECT * FROM (VALUES
  ('Library Act', 'Known Song', 'u:known', 3600.0, DATE '2026-06-01', FALSE)
) t(artist_name, track_name, spotify_track_uri, played_seconds, month,
    ms_played_estimated)""")
con.execute("""
CREATE TABLE artist_tags AS SELECT * FROM (VALUES
  ('Library Act', 'hip hop', 4, TRUE)
) t(artist_name, tag, tag_count, is_genre)""")
con.execute("""
CREATE TABLE recommendations AS SELECT * FROM (VALUES
  ('RUNN', 'm-runn', 0.9), ('Guest Only', 'm-guest', 0.8), ('Dem 2', 'm-dem2', 0.7)
) t(artist_name, mbid, score)""")
with (config.CACHE_DIR / "candidate_tags.jsonl").open("w", encoding="utf-8") as fh:
    for mbid in ("m-runn", "m-guest", "m-dem2"):
        fh.write(json.dumps({"mbid": mbid,
                             "tags": [{"tag": "indie", "count": 3}]}) + "\n")

PAGES = {
    "RUNN": [led_other_remix, free_fall, own_remix, own, alive],
    # Credited on every hit, leads none of them.
    "Guest Only": [trk("u:g1", "Big Single", ("Famous Act", "sp-fa"),
                       ("Guest Only", "sp-guest"))],
    "Dem 2": dem_page,
}


def as_items(rows):
    return [{"uri": r["spotify_track_uri"], "name": r["track_name"],
             "duration_ms": r["duration_ms"], "artists": r["artists"]}
            for r in rows]


class SearchSp:
    def __init__(self):
        self.calls = []
    def get(self, path, params=None):
        self.calls.append((path, params))
        if path != "/search":
            failures.append(f"unexpected Spotify call {path}")
            return None
        name = params["q"].removeprefix('artist:"').removesuffix('"')
        return {"tracks": {"items": as_items(PAGES.get(name, []))}}


class NoNetwork:
    def get(self, url, params=None):
        failures.append(f"MusicBrainz reached: {url} {params}")
        return None


mb_asked = []
saved = playlists.mb_genre_recordings
playlists.mb_genre_recordings = (
    lambda http, mbid, tags, cache: mb_asked.append(mbid) or set())
sp = SearchSp()
try:
    with contextlib.redirect_stdout(io.StringIO()):
        sels = playlists.build_selections(con, NoNetwork(), sp)
finally:
    playlists.mb_genre_recordings = saved

disc = [t for t in sels[0]["tracks"] if t["slot"] == "discovery"]
check("discovery is only each candidate's own records, in relevance order",
      [(t["artist_name"], t["track_name"]) for t in disc],
      [("RUNN", "Wires - RUNN Remix"), ("RUNN", "Falling Apart"),
       ("Dem 2", "Destiny")])
check("Free Fall is not offered as RUNN's work", "u:ff" in uris(disc), False)
check("a candidate with no record of their own costs no MusicBrainz lookup",
      mb_asked, ["m-runn", "m-dem2"])
check("one Spotify search per candidate",
      [p["q"] for _, p in sp.calls],
      ['artist:"RUNN"', 'artist:"Guest Only"', 'artist:"Dem 2"'])
written = {json.loads(x)["key"] for x in
           playlists.SP_TRACKS_CREDITED_CACHE.read_text(encoding="utf-8").splitlines()}
check("searches land in the credited cache Stage 10 shares",
      written, {playlists.normalise(n) for n in PAGES})
check("the retired spotify_artist_tracks.jsonl is never written",
      (config.CACHE_DIR / "spotify_artist_tracks.jsonl").exists(), False)

# A re-run reads the shared cache: Stage 10's searches cost Stage 8 nothing.
sp2 = SearchSp()
playlists.mb_genre_recordings = lambda http, mbid, tags, cache: set()
try:
    with contextlib.redirect_stdout(io.StringIO()):
        again = playlists.build_selections(con, NoNetwork(), sp2)
finally:
    playlists.mb_genre_recordings = saved
check("a cached candidate spends no search", sp2.calls, [])
check("...and selects the same tracks",
      uris(again[0]["tracks"]), uris(sels[0]["tracks"]))

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
