"""tests/test_running_selection.py — Stage 10 classification and fill.

No network. Synthetic tables only.

The run report prints counts, and a classifier that admits the right NUMBER of
artists while admitting the wrong ones looks identical in it. So the artists
that break the naive rules are pinned here: ILLENIUM (an include-list admits
him and he is the sag), REAPER (carries a genre nobody voted for), and the
tags that look electronic and are not.
"""
import sys
sys.path.insert(0, ".")
import duckdb
import config
import running

con = duckdb.connect()
con.execute("""
CREATE TABLE artist_tags AS SELECT * FROM (VALUES
  -- Uniformly on-genre: the easy cases.
  ('Subtronics',  'dubstep',         4, TRUE),
  ('Blair Muir',  'speed garage',    1, TRUE),
  ('Blair Muir',  'uk garage',       1, TRUE),
  -- The case an include-list gets wrong. dubstep(3)+trap edm(3)=6 against
  -- melodic dubstep(3)+future bass(2)=5 -> 0.545, under the 0.60 line.
  ('ILLENIUM',    'dubstep',         3, TRUE),
  ('ILLENIUM',    'trap edm',        3, TRUE),
  ('ILLENIUM',    'melodic dubstep', 3, TRUE),
  ('ILLENIUM',    'future bass',     2, TRUE),
  -- Downvoted to zero. MusicBrainz counts go negative and Stage 2 clamps them,
  -- so 0 means nobody stands behind it. REAPER anchored a metal playlist on
  -- exactly this shape.
  ('REAPER',      'heavy metal',     0, TRUE),
  ('REAPER',      'dubstep',         0, TRUE),
  -- Qualifies for BOTH clusters; the heavier weight decides.
  ('Knock2',      'bass house',      4, TRUE),
  ('Knock2',      'hybrid trap',     2, TRUE),
  -- Looks electronic, is not. `garage rock` is not UK garage.
  ('Garage Band', 'garage rock',     9, TRUE),
  ('Punk Band',   'hardcore punk',   9, TRUE),
  -- Bare `trap` is ambiguous and must neither qualify nor disqualify.
  ('Trap Rapper', 'trap',            5, TRUE),
  ('Trap Rapper', 'hip hop',         5, TRUE),
  -- A non-genre tag must not classify anyone.
  ('Some Show',   'dubstep',         9, FALSE),
  -- Drag only.
  ('Pop Act',     'pop',             6, TRUE)
) t(artist_name, tag, tag_count, is_genre)""")

running.build_artist_clusters(con)

failures = []
def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")

def cluster_of(name):
    r = con.execute("SELECT cluster FROM artist_clusters WHERE artist_name = ?",
                    [name]).fetchone()
    return r[0] if r else None

def share_of(name, col):
    r = con.execute(f"SELECT {col} FROM artist_clusters WHERE artist_name = ?",
                    [name]).fetchone()
    return round(r[0], 3) if r and r[0] is not None else None

# --- classification -----------------------------------------------------
check("clean bass artist lands in dubstep", cluster_of("Subtronics"), "dubstep")
check("clean garage artist lands in speed garage",
      cluster_of("Blair Muir"), "speed garage")
check("ILLENIUM's share is genuinely borderline",
      share_of("ILLENIUM", "bass_share"), 0.545)
check("ILLENIUM excluded — an include-list would have admitted him",
      cluster_of("ILLENIUM"), None)
check("zero-count tags classify nobody", cluster_of("REAPER"), None)
check("dual-qualifying artist goes to the heavier cluster",
      cluster_of("Knock2"), "speed garage")
check("`garage rock` is not UK garage", cluster_of("Garage Band"), None)
check("`hardcore punk` is not happy hardcore", cluster_of("Punk Band"), None)
check("bare `trap` neither qualifies nor disqualifies",
      cluster_of("Trap Rapper"), None)
check("non-genre tag classifies nobody", cluster_of("Some Show"), None)
check("drag-only artist is excluded", cluster_of("Pop Act"), None)

# --- discovery candidates face a harder bar than library artists --------
# A stranger with a sparse tag vector exploits the ratio: one cluster tag at
# count 1 and no drag tags gives 1/1 = a perfect score. Boys Noize and Mr. Oizo
# did exactly this on `tech house(1)` and were offered as speed garage.
MIN_W = config.RUN_MIN_CANDIDATE_CLUSTER_WEIGHT
broad_only = [{"tag": "tech house", "count": 1}, {"tag": "electro", "count": 3}]
check("sparse broad tag scores 1.00 on ratio alone",
      round(running.classify_tags(broad_only, 0)[1], 2), 1.0)
check("...but a candidate needs absolute weight too",
      running.classify_tags(broad_only, MIN_W)[0], None)
check("a library artist is NOT held to that floor",
      running.classify_tags(broad_only, 0)[0], "speed garage")

# A NARROW tag is meaningful at one vote where a broad one is not.
narrow_one = [{"tag": "bass house", "count": 1}, {"tag": "house", "count": 1}]
check("one vote for a narrow tag clears the floor",
      running.classify_tags(narrow_one, MIN_W)[0], "speed garage")
broad_two = [{"tag": "tech house", "count": 2}]
check("two votes for a broad tag also clear it",
      running.classify_tags(broad_two, MIN_W)[0], "speed garage")

# The floor must not rescue an artist who fails the SHARE test.
draggy = [{"tag": "dubstep", "count": 4}, {"tag": "melodic dubstep", "count": 9}]
check("absolute weight does not override a failing share",
      running.classify_tags(draggy, MIN_W)[0], None)
check("no cluster tags at all is None",
      running.classify_tags([{"tag": "pop", "count": 5}], MIN_W)[0], None)
check("empty tag vector is None", running.classify_tags([], MIN_W)[0], None)

# The Python classifier and the SQL one must agree, or discovery and the
# library drift apart silently.
check("classify_tags agrees with the SQL on ILLENIUM",
      running.classify_tags([
          {"tag": "dubstep", "count": 3}, {"tag": "trap edm", "count": 3},
          {"tag": "melodic dubstep", "count": 3},
          {"tag": "future bass", "count": 2}], 0)[0],
      cluster_of("ILLENIUM"))
check("classify_tags agrees with the SQL on a zero-count vector",
      running.classify_tags([{"tag": "dubstep", "count": 0},
                             {"tag": "heavy metal", "count": 0}], 0)[0],
      cluster_of("REAPER"))

# --- known pool: credits, completion, and the per-artist cap ------------
con.execute("""
CREATE TABLE plays AS SELECT * FROM (VALUES
  -- The remix case. Album artist is a pop act; the remixer is garage. This
  -- track must survive on the REMIXER's tags, which is the entire reason
  -- Stage 1b learned to parse '- X Remix'.
  ('uri:remix', 'Die For Me - Blair Muir Remix', 'Halsey', 300.0, 'trackdone', DATE '2026-06-01', 200000),
  -- Two by one artist plus a third: the cap must bite.
  ('uri:s1', 'Griztronics',  'Subtronics', 600.0, 'trackdone', DATE '2026-06-01', 200000),
  ('uri:s2', 'Scream Saver', 'Subtronics', 500.0, 'trackdone', DATE '2026-06-01', 200000),
  ('uri:s3', 'Third One',    'Subtronics', 400.0, 'trackdone', DATE '2026-06-01', 200000),
  -- Repeatedly skipped: enough plays for the evidence to mean something.
  ('uri:skip', 'Skipped A', 'Subtronics', 30.0, 'fwdbtn', DATE '2026-06-01', 200000),
  ('uri:skip', 'Skipped A', 'Subtronics', 30.0, 'fwdbtn', DATE '2026-06-01', 200000),
  ('uri:skip', 'Skipped A', 'Subtronics', 30.0, 'fwdbtn', DATE '2026-06-01', 200000),
  -- Played once and skipped: one play is not evidence, must NOT be dropped.
  ('uri:once', 'Heard Once', 'Blair Muir', 40.0, 'fwdbtn', DATE '2026-06-01', 200000),
  -- Outside the window.
  ('uri:old', 'Ancient', 'Subtronics', 9999.0, 'trackdone', DATE '2019-01-01', 200000),
  -- Excluded artist must contribute nothing.
  ('uri:ill', 'Good Things', 'ILLENIUM', 900.0, 'trackdone', DATE '2026-06-01', 200000)
) t(spotify_track_uri, track_name, artist_name, played_seconds, reason_end, month, ms_played)""")
con.execute("""
CREATE TABLE track_credits AS SELECT * FROM (VALUES
  ('uri:remix', 'Halsey',     'album_artist'),
  ('uri:remix', 'Blair Muir', 'remixer'),
  ('uri:s1', 'Subtronics', 'album_artist'),
  ('uri:s2', 'Subtronics', 'album_artist'),
  ('uri:s3', 'Subtronics', 'album_artist'),
  ('uri:skip', 'Subtronics', 'album_artist'),
  ('uri:once', 'Blair Muir', 'album_artist'),
  ('uri:old', 'Subtronics', 'album_artist'),
  ('uri:ill', 'ILLENIUM', 'album_artist')
) t(spotify_track_uri, artist_name, credit_type)""")

running.build_known_pool(con)
bass = running.select_known(con, "dubstep", limit=50)
garage = running.select_known(con, "speed garage", limit=50)
bass_uris = [t["spotify_track_uri"] for t in bass]
garage_uris = [t["spotify_track_uri"] for t in garage]

check("a pop record survives on its remixer's tags",
      "uri:remix" in garage_uris, True)
check("per-artist cap holds", len([t for t in bass if t["artist_name"] == "Subtronics"]),
      config.RUN_TRACKS_PER_ARTIST)
check("ranked by hours x completion", bass_uris[0], "uri:s1")
check("repeatedly-skipped track dropped", "uri:skip" in bass_uris, False)
check("a single skipped play is not enough evidence to drop",
      "uri:once" in garage_uris, True)
check("play outside the window excluded", "uri:old" in bass_uris, False)
check("excluded artist contributes nothing", "uri:ill" in bass_uris, False)

# --- overrides ----------------------------------------------------------
# A veto naming only an artist removes all of their tracks.
vetoes = {(running.normalise("Subtronics"), running.normalise(""))}
check("artist-wide veto matches any track",
      running.vetoed({"artist_name": "Subtronics", "track_name": "Anything"},
                     vetoes), True)
check("artist-wide veto does not touch others",
      running.vetoed({"artist_name": "Blair Muir", "track_name": "Anything"},
                     vetoes), False)
track_veto = {(running.normalise("Subtronics"), running.normalise("Griztronics"))}
check("track veto is exact",
      running.vetoed({"artist_name": "Subtronics", "track_name": "Scream Saver"},
                     track_veto), False)

# --- fill to a duration target -----------------------------------------
rows = [{"spotify_track_uri": f"u{i}", "duration_ms": 200_000} for i in range(20)]
filled = running.fill_to_target(rows, 1_000_000)      # exactly 5 fit
check("fills to the target and stops", len(filled), 5)
check("never overshoots the target",
      sum(r["duration_ms"] for r in filled) <= 1_000_000, True)

# An overlong track is skipped, not allowed to end the playlist.
mixed = [{"spotify_track_uri": "long", "duration_ms": 900_000},
         {"spotify_track_uri": "a", "duration_ms": 200_000},
         {"spotify_track_uri": "b", "duration_ms": 200_000}]
out = running.fill_to_target(mixed, 500_000)
check("an overlong track is skipped, not fatal",
      [r["spotify_track_uri"] for r in out], ["a", "b"])

# A missing duration is charged the median rather than treated as free.
unknown = [{"spotify_track_uri": "x", "duration_ms": None}] * 40
check("unknown durations cannot blow the budget",
      len(running.fill_to_target(unknown, 10 * 60_000)) <= 4, True)

# --- live recordings are refused however on-genre they are --------------
# Crowd noise and a tempo chosen on the night break a run, and none of that is
# visible to a genre tag. The trap is that a bare /live/ substring also takes
# "Alive" — and BOTH playlists contain an "Alive" that belongs (Zeds Dead's and
# Dustycloud's), so this has to match structurally.
for title in ["Go Away - Live at iTunes Festival 2011", "Song (Live)",
              "Song [Live]", "Song - Live", "Track Live at Wembley",
              "Song - Live From Brixton", "Album Version - Unplugged",
              "Set - Live Session"]:
    check(f"live: {title[:38]!r}", running.is_live(title), True)

for title in ["Alive", "Live Your Life", "Livewire", "Olive Branch",
              "Stayin' Alive", "Deliverance", "Come Alive - VIP Mix"]:
    check(f"not live: {title!r}", running.is_live(title), False)
check("empty title is not live", running.is_live(""), False)

# --- interleaving must hold when known tracks are the MAJORITY ----------
# playlists.assemble spaces anchors by size//len(anchors), which collapses to a
# step of 1 once anchors outnumber discovery — every anchor first, discovery
# stapled on the end. Invisible at 33 tracks; at four hours on a 40-minute run
# it means the discovery half is never reached.
kn = [{"spotify_track_uri": f"k{i}", "duration_ms": 200_000} for i in range(45)]
dis = [{"spotify_track_uri": f"d{i}", "duration_ms": 200_000} for i in range(30)]
mixed = running.interleave(kn, dis)
check("interleave keeps every track", len(mixed), 75)
check("positions are 0..n-1", [t["position"] for t in mixed], list(range(75)))
check("no track duplicated", len({t["spotify_track_uri"] for t in mixed}), 75)
check("slots are labelled", {t["slot"] for t in mixed}, {"anchor", "discovery"})

first_third = [t["slot"] for t in mixed[:25]]
last_third = [t["slot"] for t in mixed[-25:]]
check("discovery reaches the first third even when outnumbered",
      "discovery" in first_third, True)
check("known reaches the last third", "anchor" in last_third, True)
# The real regression: with a majority-known list, more than half the discovery
# must appear before the final quarter.
early_disc = sum(1 for t in mixed[:56] if t["slot"] == "discovery")
check("most discovery lands before the last quarter", early_disc >= 15, True)
check("proportions preserved",
      (sum(1 for t in mixed if t["slot"] == "anchor"),
       sum(1 for t in mixed if t["slot"] == "discovery")), (45, 30))

check("interleave with no discovery is all anchors",
      {t["slot"] for t in running.interleave(kn, [])}, {"anchor"})
check("interleave with no known is all discovery",
      {t["slot"] for t in running.interleave([], dis)}, {"discovery"})
check("interleave of nothing is empty", running.interleave([], []), [])

# --- the client must never be able to delete ---------------------------
check("Spotify client has no delete verb",
      hasattr(running.Spotify, "delete"), False)

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
