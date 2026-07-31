"""tests/test_playlist_selection.py — selection logic on synthetic tables."""
import sys
sys.path.insert(0, ".")
import duckdb
import playlists

con = duckdb.connect()
con.execute("""
CREATE TABLE genre_gaps AS SELECT * FROM (VALUES
  ('dubstep', 0.0005, 30.0, 81, 0.91), ('classical', 0.00008, 6.4, 83, 0.78),
  ('dance', 0.00003, 62.8, 231, 0.20), ('deep house', 0.00002, 8.2, 73, 0.19),
  ('wave', 0.00001, 2.0, 5, 0.50)
) t(tag, gap_score, hours, n_artists, rel_change_per_year)""")
con.execute("""
CREATE TABLE plays AS SELECT * FROM (VALUES
  -- recent dubstep-artist listening: two tracks by Sub, one by Ex
  ('Subtronics', 'Griztronics',  'uri:griz',  7200.0, DATE '2026-06-01'),
  ('Subtronics', 'Scream Saver', 'uri:scream',3600.0, DATE '2026-05-01'),
  ('Subtronics', 'Third Track',  'uri:third', 1800.0, DATE '2026-04-01'),
  ('Excision',   'The Paradox',  'uri:parad', 5400.0, DATE '2026-06-01'),
  -- old play outside the anchor window: must not surface
  ('Subtronics', 'Ancient One',  'uri:old',   9999.0, DATE '2020-01-01'),
  -- library artist with no dubstep tag: must not anchor dubstep
  ('Drake',      'Passionfruit', 'uri:pass',  9000.0, DATE '2026-06-01'),
  -- a podcast row: no track uri, must never reach a playlist
  ('Some Show',  'Episode 12',   NULL,        9999.0, DATE '2026-06-01')
) t(artist_name, track_name, spotify_track_uri, played_seconds, month)""")
con.execute("""
CREATE TABLE artist_tags AS SELECT * FROM (VALUES
  ('Subtronics', 'dubstep', TRUE), ('Excision', 'dubstep', TRUE),
  ('Drake', 'hip hop', TRUE),
  -- a non-genre tag must not qualify an artist as serving the gap
  ('Some Show', 'dubstep', FALSE)
) t(artist_name, tag, is_genre)""")
con.execute("""
CREATE TABLE recommendations AS SELECT * FROM (VALUES
  ('Virtual Riot', 'mbid-vr', 0.9), ('SVDDEN DEATH', 'mbid-sd', 0.8),
  ('Boring Artist', 'mbid-ba', 0.7),
  ('Subtronics',    'mbid-sub', 0.99)   -- already in library: must be excluded
) t(artist_name, mbid, score)""")

TAG_CACHE = {
    "mbid-vr":  {"tags": [{"tag": "dubstep", "count": 5}]},
    "mbid-sd":  {"tags": [{"tag": "dubstep", "count": 2}, {"tag": "metal", "count": 1}]},
    "mbid-ba":  {"tags": [{"tag": "ambient", "count": 9}]},
    "mbid-sub": {"tags": [{"tag": "dubstep", "count": 9}]},
}

failures = []
def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")

gaps = playlists.select_gaps(con)
check("gaps capped at N_PLAYLISTS", len(gaps), 4)
check("gaps ordered by score", [g["tag"] for g in gaps][:2], ["dubstep", "classical"])

anchors = playlists.select_anchor_tracks(con, "dubstep")
check("anchor cap respected", len(anchors) <= playlists.config.ANCHOR_TRACKS, True)
check("per-artist cap: Subtronics contributes 2 not 3",
      sum(1 for a in anchors if a["artist_name"] == "Subtronics"), 2)
check("old play outside window excluded",
      any(a["spotify_track_uri"] == "uri:old" for a in anchors), False)
check("untagged-artist track excluded",
      any(a["artist_name"] == "Drake" for a in anchors), False)
check("non-genre tag does not qualify an artist",
      any(a["artist_name"] == "Some Show" for a in anchors), False)
check("ranked by recent hours", anchors[0]["spotify_track_uri"], "uri:griz")

cands = playlists.select_candidates(con, "dubstep", TAG_CACHE)
check("gap-tag filter applied", [c["artist_name"] for c in cands],
      ["Virtual Riot", "SVDDEN DEATH"])
check("library artist excluded from discovery",
      any(c["artist_name"] == "Subtronics" for c in cands), False)

tracks = playlists.assemble(
    anchors=[{"spotify_track_uri": f"uri:a{i}"} for i in range(3)],
    discovery=[{"spotify_track_uri": f"uri:d{i}"} for i in range(30)],
    size=10)
check("assembled to size", len(tracks), 10)
check("positions are 0..n-1", [t["position"] for t in tracks], list(range(10)))
check("anchors spread not clumped",
      [t["position"] for t in tracks if t["slot"] == "anchor"], [0, 3, 6])
check("no uri repeats", len({t["spotify_track_uri"] for t in tracks}), 10)

# Degenerate shapes: the playlist must never be padded with repeats, and must
# never exceed `size`.
thin = playlists.assemble(
    anchors=[{"spotify_track_uri": "uri:a0"}],
    discovery=[{"spotify_track_uri": "uri:d0"}], size=10)
check("short pools give a short playlist, not repeats", len(thin), 2)
check("no duplicates when pools run dry",
      len({t["spotify_track_uri"] for t in thin}), 2)

no_anchor = playlists.assemble(
    anchors=[], discovery=[{"spotify_track_uri": f"uri:d{i}"} for i in range(5)],
    size=3)
check("no anchors at all is fine", len(no_anchor), 3)
check("all-discovery when there are no anchors",
      {t["slot"] for t in no_anchor}, {"discovery"})

# A URI present in both pools must appear once, not twice.
dupe = playlists.assemble(
    anchors=[{"spotify_track_uri": "uri:same"}],
    discovery=[{"spotify_track_uri": "uri:same"},
               {"spotify_track_uri": "uri:other"}], size=5)
check("uri in both pools appears once",
      [t["spotify_track_uri"] for t in dupe], ["uri:same", "uri:other"])

check("empty everything is tolerated", playlists.assemble([], [], 10), [])

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
