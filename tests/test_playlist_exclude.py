"""tests/test_playlist_exclude.py — a spec exclude keeps artists out of anchors,
candidates and credited discovery tracks. No network."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

# config resolves every path at import time, so point them all at a scratch
# directory FIRST. Nothing here may read real data or append to a real cache.
_TMP = Path(tempfile.mkdtemp(prefix="s8-excl-"))
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
con.execute("""CREATE TABLE plays AS SELECT * FROM (VALUES
  ('Drake',       'Passionfruit',          'spotify:track:d1', 9000.0, DATE '2026-08-01', false),
  ('Daniel Caesar','Get You',              'spotify:track:c1', 3000.0, DATE '2026-08-01', false),
  ('Halsey',      'Colors - Drake Remix',  'spotify:track:h1', 5000.0, DATE '2026-08-01', false)
) t(artist_name, track_name, spotify_track_uri, played_seconds, month, ms_played_estimated)""")
con.execute("""CREATE TABLE artist_tags AS SELECT * FROM (VALUES
  ('Drake', 'm-drake', 'alternative r&b', 5, true, 'musicbrainz'),
  ('Daniel Caesar', 'm-dc', 'neo soul', 5, true, 'musicbrainz')
) t(artist_name, mbid, tag, tag_count, is_genre, source)""")

tags = ["neo soul", "alternative r&b"]
got = {a["judge"] for a in playlists.select_anchor_tracks(con, tags)}
check("fixture valid: both anchor without exclude", got, {"Drake", "Daniel Caesar"})

got = {a["judge"] for a in playlists.select_anchor_tracks(con, tags, exclude=["drake"])}
check("excluded album artist never anchors (case-folded)", got, {"Daniel Caesar"})

got = [a["track_name"] for a in playlists.select_anchor_tracks(con, tags, exclude=["Drake"])]
check("a remix JUDGED as the excluded artist never anchors",
      "Colors - Drake Remix" in got, False)

specs = [{"label": "vs", "tags": tags, "exclude": ["Drake"]}]
got = {a["judge"] for a in playlists.select_run_anchors(con, specs)[0]}
check("select_run_anchors passes the spec's exclude", got, {"Daniel Caesar"})

con.execute("""CREATE TABLE recommendations AS SELECT * FROM (VALUES
  ('NAV', 'm-nav', 0.9), ('Brent Faiyaz', 'm-bf', 0.8)
) t(artist_name, mbid, score)""")
tag_cache = {"m-nav": {"tags": [{"tag": "alternative r&b", "count": 3}]},
             "m-bf":  {"tags": [{"tag": "alternative r&b", "count": 3}]}}
got = [c["artist_name"] for c in playlists.select_candidates(con, tags, tag_cache, exclude=["NAV"])]
check("excluded candidate dropped", got, ["Brent Faiyaz"])

tracks = [
  {"track_name": "Clouded", "artists": [{"name": "Brent Faiyaz", "id": "b"}]},
  {"track_name": "Wasting Time", "artists": [{"name": "Brent Faiyaz", "id": "b"},
                                             {"name": "Drake", "id": "d"}]},
]
got = [t["track_name"] for t in playlists.drop_excluded(tracks, ["drake"])]
check("a discovery track featuring an excluded artist is dropped", got, ["Clouded"])
check("empty exclude is a no-op", len(playlists.drop_excluded(tracks, [])), 2)

nonlatin = [{"track_name": "A", "artists": [{"name": "Brent Faiyaz"}]},
            {"track_name": "B", "artists": [{"name": None}]},
            {"track_name": "C", "artists": [{"name": "Земфира"}]}]
check("an exclude folding to '' drops nothing",
      len(playlists.drop_excluded(nonlatin, ["Земфира"])), 3)
check("_exclude_keys discards empty folds",
      playlists._exclude_keys(["Земфира", "Drake"]), {playlists.normalise("Drake")})

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
