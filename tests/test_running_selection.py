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
  ('Pop Act',     'pop',             6, TRUE),
  -- jigitz's confirmed hand tags. Before `breakbeat` left the bass list this
  -- was 1/1 dubstep; now no cluster tag survives and `house` is on neither
  -- list, so he classifies nowhere and his tracks come back only as pins.
  ('jigitz',      'breakbeat',       1, TRUE),
  ('jigitz',      'future garage',   1, TRUE),
  ('jigitz',      'house',           1, TRUE)
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

# --- breakbeat is not run music, and four more genres are drag ----------
# The cost of taking `breakbeat` out lands on library artists, and this is the
# biggest of it: jigitz, 5.9 h, classifies to neither run.
check("jigitz's confirmed tags carry no cluster tag any more",
      cluster_of("jigitz"), None)

# The Prodigy's real vector. With `big beat` as drag he would score 0.42 bass
# and 0.25 garage even with breakbeat still listed; with it gone, nothing.
prodigy = [{"tag": "big beat", "count": 21}, {"tag": "breakbeat", "count": 15},
           {"tag": "breakbeat hardcore", "count": 7},
           {"tag": "hardcore breaks", "count": 3}, {"tag": "rave", "count": 4}]
check("The Prodigy classifies nowhere, even at the library bar",
      running.classify_tags(prodigy, 0)[0], None)
check("...and his best share is zero", running.classify_tags(prodigy, 0)[1], 0.0)

# --- strangers clear 0.85, library artists still 0.60 -------------------
# Netsky: a clean drum-and-bass share for a library artist, and a liquid act a
# run playlist should never be offered by a stranger's say-so.
CAND = config.RUN_MIN_CANDIDATE_SHARE
netsky = [{"tag": "drum and bass", "count": 10}, {"tag": "liquid funk", "count": 3},
          {"tag": "electronic", "count": 2}]
check("Netsky fails the stranger bar",
      running.classify_tags(netsky, MIN_W, min_share=CAND)[0], None)
check("...at 0.77",
      round(running.classify_tags(netsky, MIN_W, min_share=CAND)[1], 2), 0.77)
check("the same vector at the library bar is still dubstep",
      running.classify_tags(netsky, 0)[0], "dubstep")
nero = [{"tag": "drum and bass", "count": 3}, {"tag": "dubstep", "count": 5},
        {"tag": "liquid funk", "count": 1}]
check("NERO clears the stranger bar at 0.89",
      running.classify_tags(nero, MIN_W, min_share=CAND),
      ("dubstep", 8 / 9))
check("ILLENIUM is refused at the stranger bar too",
      running.classify_tags([
          {"tag": "dubstep", "count": 3}, {"tag": "trap edm", "count": 3},
          {"tag": "melodic dubstep", "count": 3},
          {"tag": "future bass", "count": 2}], MIN_W, min_share=CAND)[0], None)

# --- `happy hardcore` is not a bass-run tag (SJ, 2026-09-27) ------------
# Stonebank reached the dubstep run as a stranger on drum and bass(1) plus
# happy hardcore(1): the narrow tag waived the weight floor, and the record
# was 2016 Monstercat happy hardcore with a pitched euphoric vocal, a cut.
check("`happy hardcore` is not a bass tag",
      "happy hardcore" in config.RUN_BASS_TAGS, False)
stonebank = [{"tag": "drum and bass", "count": 1},
             {"tag": "happy hardcore", "count": 1}]
check("Stonebank no longer clears the stranger bar",
      running.classify_tags(stonebank, MIN_W, min_share=CAND)[0], None)
# The cost, measured before the change, is Kayzo (0.89 h): his real vector sat
# at exactly 0.60 with the tag (3 bass against psytrance 2) and at 0.50
# without it, so he now classifies to neither run.
hc = duckdb.connect()
hc.execute("""
CREATE TABLE artist_tags AS SELECT * FROM (VALUES
  ('Kayzo', 'psytrance', 2, TRUE), ('Kayzo', 'electro', 1, TRUE),
  ('Kayzo', 'happy hardcore', 1, TRUE), ('Kayzo', 'hardstyle', 1, TRUE),
  ('Kayzo', 'house', 1, TRUE), ('Kayzo', 'dubstep', 1, TRUE),
  ('Kayzo', 'trap', 1, TRUE)
) t(artist_name, tag, tag_count, is_genre)""")
running.build_artist_clusters(hc)
check("...and Kayzo, whom it carried to 0.60, classifies nowhere",
      hc.execute("SELECT cluster, round(bass_share, 2) FROM artist_clusters"
                 ).fetchone(), (None, 0.5))

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
# Stage 3's horizon excludes estimated (polled) rows; these are all export rows.
con.execute("ALTER TABLE plays ADD COLUMN ms_played_estimated BOOLEAN DEFAULT FALSE")
con.execute("""
CREATE TABLE track_credits AS SELECT * FROM (VALUES
  ('uri:remix', 'Halsey',     'album_artist', 'export'),
  ('uri:remix', 'Blair Muir', 'remixer',      'export'),
  ('uri:s1', 'Subtronics', 'album_artist', 'export'),
  ('uri:s2', 'Subtronics', 'album_artist', 'export'),
  ('uri:s3', 'Subtronics', 'album_artist', 'export'),
  ('uri:skip', 'Subtronics', 'album_artist', 'export'),
  ('uri:once', 'Blair Muir', 'album_artist', 'export'),
  ('uri:old', 'Subtronics', 'album_artist', 'export'),
  ('uri:ill', 'ILLENIUM', 'album_artist', 'export')
) t(spotify_track_uri, artist_name, credit_type, credit_source)""")

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

# --- known pool: polled plays, the export horizon, admitting credits ----
# The poller's rows are provisional: estimated ms_played, NULL reason_end. Read
# as completion evidence they turned every polled play into a skip (Rain fell
# from 0.846), and read as "now" they slid the 36-month window forward and lost
# 82.6 h off its old end. They still count as HOURS: the listening happened.
kp = duckdb.connect()
kp.execute("""
CREATE TABLE artist_tags AS SELECT * FROM (VALUES
  ('Subtronics',   'dubstep',         4, TRUE),
  ('NGHTMRE',      'dubstep',         3, TRUE),
  ('Habstrakt',    'bass house',      2, TRUE),
  ('John Summit',  'tech house',      3, TRUE),
  ('Todd Edwards', 'uk garage',       2, TRUE),
  -- On neither list: Daft Punk places nothing on his own.
  ('Daft Punk',    'house',           5, TRUE),
  -- Inéz's hand tags: `house` is on neither list and melodic dubstep is drag,
  -- so she has no cluster — and on the known side that must change nothing.
  ('Inéz',         'house',           1, TRUE),
  ('Inéz',         'melodic dubstep', 1, TRUE)
) t(artist_name, tag, tag_count, is_genre)""")
running.build_artist_clusters(kp)
kp.execute("""
CREATE TABLE plays AS SELECT * FROM (VALUES
  -- 3 finished export plays, then 2 polled ones. Completion is the export's
  -- 3/3; the polled plays count as hours only. The export ends in 2026-07 and
  -- the poller runs on to 2026-09.
  ('uri:mix', 'Mixed Evidence', 'Subtronics', 200.0, 'trackdone', DATE '2026-07-01', 200000, FALSE),
  ('uri:mix', 'Mixed Evidence', 'Subtronics', 200.0, 'trackdone', DATE '2026-07-01', 200000, FALSE),
  ('uri:mix', 'Mixed Evidence', 'Subtronics', 200.0, 'trackdone', DATE '2026-07-01', 200000, FALSE),
  ('uri:mix', 'Mixed Evidence', 'Subtronics', 200.0, NULL,        DATE '2026-09-01', 200000, TRUE),
  ('uri:mix', 'Mixed Evidence', 'Subtronics', 200.0, NULL,        DATE '2026-09-01', 200000, TRUE),
  -- Heard only by the poller: no completion evidence at all, and a polled
  -- ms_played is the whole track, so it is the duration.
  ('uri:polled', 'Only Polled', 'Subtronics', 222.0, NULL, DATE '2026-09-01', 222000, TRUE),
  -- 2 export skips + 2 polled plays. Four "plays" at 0% done used to floor it;
  -- two export skips are not enough evidence to.
  ('uri:skip2', 'Two Skips', 'Subtronics', 40.0, 'fwdbtn', DATE '2026-06-01', 40000, FALSE),
  ('uri:skip2', 'Two Skips', 'Subtronics', 40.0, 'fwdbtn', DATE '2026-06-01', 40000, FALSE),
  ('uri:skip2', 'Two Skips', 'Subtronics', 200.0, NULL,    DATE '2026-09-01', 200000, TRUE),
  ('uri:skip2', 'Two Skips', 'Subtronics', 200.0, NULL,    DATE '2026-09-01', 200000, TRUE),
  -- 36 months back from the EXPORT's last month is 2023-07: in, and a month
  -- earlier out. Anchored on the polled 2026-09 it would start at 2023-09.
  ('uri:edge', 'Three Years Back', 'Subtronics', 300.0, 'trackdone', DATE '2023-07-01', 200000, FALSE),
  ('uri:past', 'One Month Older',  'Subtronics', 300.0, 'trackdone', DATE '2023-06-01', 200000, FALSE),
  -- C10: which credits may place a track.
  ('uri:frag',  'Fragments of Time', 'Daft Punk', 300.0, 'trackdone', DATE '2026-06-01', 280000, FALSE),
  ('uri:one',   'The One - NGHTMRE Remix', 'Habstrakt', 300.0, 'trackdone', DATE '2026-06-01', 200000, FALSE),
  ('uri:eyes',  'Eyes Cut Deeper (feat. Inéz)', 'Subtronics', 300.0, 'trackdone', DATE '2026-06-01', 200000, FALSE),
  ('uri:cryst', 'crystallized (feat. Inéz) - Subtronics Remix', 'John Summit', 300.0, 'trackdone', DATE '2026-06-01', 200000, FALSE),
  ('uri:light', 'light years (feat. Inéz)', 'John Summit', 300.0, 'trackdone', DATE '2026-06-01', 200000, FALSE)
) t(spotify_track_uri, track_name, artist_name, played_seconds, reason_end, month, ms_played,
    ms_played_estimated)""")
kp.execute("""
CREATE TABLE track_credits AS SELECT * FROM (VALUES
  ('uri:mix',    'Subtronics',   'album_artist', 'export'),
  ('uri:polled', 'Subtronics',   'album_artist', 'export'),
  ('uri:skip2',  'Subtronics',   'album_artist', 'export'),
  ('uri:edge',   'Subtronics',   'album_artist', 'export'),
  ('uri:past',   'Subtronics',   'album_artist', 'export'),
  ('uri:frag',   'Daft Punk',    'album_artist', 'export'),
  ('uri:frag',   'Todd Edwards', 'featured',     'export'),
  ('uri:one',    'Habstrakt',    'album_artist', 'export'),
  ('uri:one',    'NGHTMRE',      'remixer',      'export'),
  ('uri:eyes',   'Subtronics',   'album_artist', 'poller'),
  ('uri:eyes',   'Inéz',         'featured',     'poller'),
  ('uri:cryst',  'John Summit',  'album_artist', 'export'),
  ('uri:cryst',  'Inéz',         'featured',     'export'),
  ('uri:cryst',  'Subtronics',   'remixer',      'export'),
  ('uri:light',  'John Summit',  'album_artist', 'export'),
  ('uri:light',  'Inéz',         'featured',     'export')
) t(spotify_track_uri, artist_name, credit_type, credit_source)""")

def pool_clusters(uri):
    running.build_known_pool(kp)
    return sorted(r[0] for r in kp.execute(
        "SELECT cluster FROM known_pool WHERE spotify_track_uri = ?", [uri]
    ).fetchall())

def pool_row(uri):
    cols = ["n_plays", "hours", "done_rate", "done_smoothed", "duration_ms"]
    r = kp.execute(f"SELECT {', '.join(cols)} FROM known_pool "
                   "WHERE spotify_track_uri = ?", [uri]).fetchone()
    return dict(zip(cols, r)) if r else None

running.build_known_pool(kp)
mix = pool_row("uri:mix")
check("C9: completion is measured on export plays only",
      mix and mix["done_rate"], 1.0)
check("...and n_plays counts only those", mix and mix["n_plays"], 3)
check("...while the polled plays still count as hours",
      mix and round(mix["hours"], 4), round(1000 / 3600, 4))
polled = pool_row("uri:polled")
check("a polled-only track is in the pool, not NULLed out", polled is not None, True)
check("...at the 0.5 prior", polled and polled["done_smoothed"], 0.5)
check("...with the polled ms_played as its duration",
      polled and polled["duration_ms"], 222000)
check("2 export skips + 2 polled plays is not floored",
      pool_row("uri:skip2") is not None, True)
check("the window runs 36 months back from the EXPORT horizon",
      pool_row("uri:edge") is not None, True)
check("...and not a month further", pool_row("uri:past"), None)

# Todd Edwards is on Daft Punk's record only by the title regex. An export
# feature is a guess, and guessing is how his 0.54 h became a garage seed.
check("C10: an export feature does not admit (Fragments of Time)",
      pool_clusters("uri:frag"), [])
kp.execute("UPDATE track_credits SET credit_source = 'poller' "
           "WHERE spotify_track_uri = 'uri:frag'")
check("...a poller feature does", pool_clusters("uri:frag"), ["speed garage"])

# A remix belongs to its remixer's run. Habstrakt's bass house would also put
# "The One - NGHTMRE Remix" in garage; the record is NGHTMRE's.
check("C10 routing: a remix goes to the remixer's cluster only",
      pool_clusters("uri:one"), ["dubstep"])
# credits.py types every non-first poller artist as `featured`, remixers
# included, which is why routing reads the title rather than credit_type.
kp.execute("UPDATE track_credits SET credit_type = 'featured', "
           "credit_source = 'poller' WHERE spotify_track_uri = 'uri:one'")
check("...still when the poller types the remixer as featured",
      pool_clusters("uri:one"), ["dubstep"])

check("a featured credit with no cluster neither admits nor refuses",
      pool_clusters("uri:eyes"), ["dubstep"])
kp.execute("DELETE FROM track_credits WHERE spotify_track_uri = 'uri:eyes' "
           "AND artist_name = 'Inéz'")
check("...exactly as with no Inéz row at all", pool_clusters("uri:eyes"), ["dubstep"])
check("John Summit's Subtronics remix is dubstep only",
      pool_clusters("uri:cryst"), ["dubstep"])
check("...and his own light years stays garage",
      pool_clusters("uri:light"), ["speed garage"])

# --- song identity: pressings fold, remixes don't (C7) ------------------
# Two keys. version_key drops only PRESSING notes (remaster, radio edit,
# feat. ...), so the album cut and the single are one record while a remix
# stays its own. song_key is playlists._title_key, which drops every suffix.
# The folded title used to be the only key, so a garage original kept its own
# remix out of dubstep, and "tell you straight" was two half-counted rows.
VERSION_PAIRS = [
    # (a, b, same version?)
    ("Drugs I Like (AVELLO Remix)", "Drugs I Like", False),
    ("War Pigs - 2012 - Remaster", "War Pigs", True),
    ("Song - Radio Edit", "Song", True),
    ("Song - Extended Mix", "Song", True),
    ("Song (feat. X)", "Song", True),
    ("Song - Remastered 2011", "Song", True),
    ("Song (with X) - Radio Edit", "Song", True),
    ("Song - Radio Edit (feat. X)", "Song", True),
    ("Song - Sped Up", "Song", False),
    ("Song - VIP", "Song", False),
    ("iloveitiloveitiloveit - Garage", "iloveitiloveitiloveit", False),
    # `clean` is a pressing note only when it is the whole segment.
    ("Song - Clean Bandit Remix", "Song", False),
    ("Song (Clean)", "Song", True),
    # A title that is nothing but a note keeps its full form, not "".
    ("(Remastered)", "", False),
]
for a, b, same in VERSION_PAIRS:
    check(f"version_key: {a!r} {'==' if same else '!='} {b!r}",
          running.version_key(a) == running.version_key(b), same)
check("a remix is a different version of the SAME song",
      running._title_key("Drugs I Like (AVELLO Remix)"),
      running._title_key("Drugs I Like"))

# One regex, two engines. DuckDB compiles RE2, which refuses lookaround and
# backreferences that Python accepts, and a key that disagrees between the pool
# (SQL) and the dedupe (Python) fails silently.
titles = sorted({t for a, b, _ in VERSION_PAIRS for t in (a, b)}
                | {"Inéz — Cañón", "東京 - Radio Edit", "Song - X Remix - Radio Edit",
                   "Song  -  Radio Edit", "Song (feat. Jay-Z) [Explicit]"})
sql_keys = dict(duckdb.connect().execute(
    f"SELECT t, {running.version_key_sql('t')} FROM (SELECT unnest(?) AS t)",
    [titles]).fetchall())
for t in titles:
    check(f"SQL version_key agrees with Python on {t!r}",
          sql_keys[t], running.version_key(t))

vp = duckdb.connect()
vp.execute("""
CREATE TABLE artist_tags AS SELECT * FROM (VALUES
  ('Subtronics',  'dubstep', 4, TRUE),
  ('Nuclear Act', 'dubstep', 4, TRUE)
) t(artist_name, tag, tag_count, is_genre)""")
running.build_artist_clusters(vp)
vp.execute("""
CREATE TABLE plays (spotify_track_uri VARCHAR, track_name VARCHAR,
    artist_name VARCHAR, played_seconds DOUBLE, reason_end VARCHAR, month DATE,
    ms_played BIGINT, ms_played_estimated BOOLEAN)""")

def add_plays(uri, title, artist, n, n_done, secs):
    vp.execute("""
        INSERT INTO plays SELECT ?, ?, ?, ?,
               CASE WHEN i < ? THEN 'trackdone' ELSE 'fwdbtn' END,
               DATE '2026-06-01', 200000, FALSE
        FROM range(?) t(i)""", [uri, title, artist, secs, n_done, n])

# Two pressings of one version: counted whole, under the most-played URI.
add_plays("uri:me1", "Mixed Evidence", "Subtronics", 3, 3, 200.0)
add_plays("uri:me2", "Mixed Evidence - Radio Edit", "Subtronics", 1, 1, 200.0)
# The remix finishes far more often than the original, which has more hours:
# completion, not hours, decides which version of a song the playlist takes.
add_plays("uri:orig", "Tune", "Nuclear Act", 26, 14, 400.0)
add_plays("uri:rmx", "Tune (Other Remix)", "Nuclear Act", 39, 36, 100.0)
# ...but not on two plays: 2/2 is not evidence against 6/10.
add_plays("uri:o2", "Other Tune", "Nuclear Act", 10, 6, 200.0)
add_plays("uri:vip", "Other Tune - VIP", "Nuclear Act", 2, 2, 200.0)
vp.execute("""
CREATE TABLE track_credits AS
SELECT DISTINCT spotify_track_uri, artist_name, 'album_artist' AS credit_type,
       'export' AS credit_source
FROM plays""")
running.build_known_pool(vp)

def vp_rows(where):
    cols = ["spotify_track_uri", "n_plays", "uris"]
    return [dict(zip(cols, r)) for r in vp.execute(
        f"SELECT {', '.join(cols)} FROM known_pool WHERE {where} "
        "ORDER BY spotify_track_uri").fetchall()]

me = vp_rows("album_artist = 'Subtronics'")
check("two URIs of one version are one pool row", len(me), 1)
check("...with the plays summed", me and me[0]["n_plays"], 4)
check("...under the most-played pressing", me and me[0]["spotify_track_uri"], "uri:me1")
check("...and every pressing's URI kept", me and me[0]["uris"], ["uri:me1", "uri:me2"])
check("within a cluster the 0.92-done remix beats the 0.54-done original",
      [r["spotify_track_uri"] for r in vp_rows("track_name LIKE 'Tune%'")],
      ["uri:rmx"])
check("a 2-play remix does not beat the original",
      [r["spotify_track_uri"] for r in vp_rows("track_name LIKE 'Other Tune%'")],
      ["uri:o2"])

# Across the two playlists a song is judged by VERSION: an original placed in
# garage no longer blocks its remix from dubstep. Within one playlist it is
# still one version per song.
pl = running.Placements()
orig = {"spotify_track_uri": "u:o", "artist_name": "SIDEPIECE",
        "track_name": "Drugs I Like"}
remix = {"spotify_track_uri": "u:r", "artist_name": "SIDEPIECE",
         "track_name": "Drugs I Like (AVELLO Remix)"}
single = {"spotify_track_uri": "u:s", "artist_name": "SIDEPIECE",
          "track_name": "Drugs I Like - Radio Edit"}
pl.place(orig)
check("in garage, the original's remix is a second version: refused",
      pl.fresh(remix), False)
pl.new_playlist()
check("in dubstep, the remix is fresh", pl.fresh(remix), True)
check("...but another pressing of the placed original is not",
      pl.fresh(single), False)
check("...nor the same URI", pl.fresh(dict(remix, spotify_track_uri="u:o")), False)
pl.place(remix)
pl.release(remix)
check("a released row can be offered again", pl.fresh(remix), True)

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

# --- a veto reads every credit; the cap comes after the filters (C3, C8) --
# The veto used to test the album artist only, so an artist-wide ILLENIUM veto
# left Dillon Francis' "Don't Let Me Let Go" — ILLENIUM on it as a feature — in
# the dubstep run. And the per-artist cap ran in SQL before any filter, so a
# vetoed, live or already-placed track still used up one of its artist's three
# slots: vetoing one track SHRANK the artist instead of promoting the next.
N = running.normalise
vc = duckdb.connect()
vc.execute("""
CREATE TABLE artist_tags AS SELECT * FROM (VALUES
  ('Dillon Francis', 'dubstep', 3, TRUE),
  ('ILLENIUM',       'dubstep', 3, TRUE),
  ('ILLENIUM',       'melodic dubstep', 3, TRUE),
  ('ILLENIUM',       'future bass', 2, TRUE),
  ('NGHTMRE',        'dubstep', 3, TRUE),
  ('Other Act',      'dubstep', 3, TRUE),
  ('Four Act',       'dubstep', 3, TRUE),
  ('Live Act',       'dubstep', 3, TRUE)
) t(artist_name, tag, tag_count, is_genre)""")
running.build_artist_clusters(vc)
vc.execute("""
CREATE TABLE plays (spotify_track_uri VARCHAR, track_name VARCHAR,
    artist_name VARCHAR, played_seconds DOUBLE, reason_end VARCHAR, month DATE,
    ms_played BIGINT, ms_played_estimated BOOLEAN)""")

def vc_plays(uri, title, artist, n):
    vc.execute("""
        INSERT INTO plays SELECT ?, ?, ?, 200.0, 'trackdone', DATE '2026-06-01',
               200000, FALSE FROM range(?)""", [uri, title, artist, n])

# Two pressings of one record, so `credited` must be the union over both.
vc_plays("uri:ddlm",  "Don't Let Me Let Go", "Dillon Francis", 9)
vc_plays("uri:ddlm2", "Don't Let Me Let Go - Radio Edit", "Dillon Francis", 2)
vc_plays("uri:baf",   "Buried A Friend", "Other Act", 8)
vc_plays("uri:oth",   "Something Else",  "Other Act", 7)
for i, n in enumerate((6, 5, 4, 3), start=1):
    vc_plays(f"uri:f{i}", f"Four Tune {i}", "Four Act", n)
vc_plays("uri:l1", "Big Tune (Live)", "Live Act", 6)
for i, n in enumerate((5, 4, 3), start=2):
    vc_plays(f"uri:l{i}", f"Live Act Tune {i}", "Live Act", n)
vc.execute("""
CREATE TABLE track_credits AS
SELECT DISTINCT spotify_track_uri, artist_name, 'album_artist' AS credit_type,
       'export' AS credit_source
FROM plays""")
vc.execute("""INSERT INTO track_credits VALUES
  ('uri:ddlm2', 'ILLENIUM', 'featured', 'poller'),
  ('uri:baf',   'NGHTMRE',  'featured', 'export')""")
running.build_known_pool(vc)
vk = running.select_known(vc, "dubstep")
vk_by = {r["spotify_track_uri"]: r for r in vk}

check("select_known carries every credit, over every pressing",
      vk_by["uri:ddlm"]["credited"], ["Dillon Francis", "ILLENIUM"])
check("...as a LIST, never a joined string",
      type(vk_by["uri:ddlm"]["credited"]), list)
check("...and every pressing's URI", vk_by["uri:ddlm"]["uris"],
      ["uri:ddlm", "uri:ddlm2"])
check("select_known no longer caps: all four of Four Act's tracks come back",
      sum(1 for r in vk if r["artist_name"] == "Four Act"), 4)
check("...in score order, ties on the URI",
      [r["score"] for r in vk] == sorted((r["score"] for r in vk), reverse=True),
      True)

def eligible(rows, vetoes=frozenset(), fresh=lambda r: True, pinned=()):
    return [r["spotify_track_uri"]
            for r in running.eligible_known(rows, set(vetoes), fresh, pinned)]

got = eligible(vk, {(N("ILLENIUM"), "")})
check("an artist-wide veto on a FEATURED credit removes the track",
      "uri:ddlm" in got, False)
check("...and touches nothing that does not credit them",
      "uri:oth" in got and "uri:f1" in got, True)
got = eligible(vk, {(N("NGHTMRE"), N("Buried A Friend"))})
check("a (credited name, title) veto matches through `credited`",
      ("uri:baf" in got, "uri:oth" in got), (False, True))
check("vetoed() itself reads `credited`",
      running.vetoed(vk_by["uri:baf"], {(N("NGHTMRE"), "")}), True)
check("...and tolerates a row without it",
      running.vetoed({"artist_name": "NGHTMRE", "track_name": "X"},
                     {(N("NGHTMRE"), "")}), True)

four = lambda got: [u for u in got if u.startswith("uri:f")]
check("with no filter, the cap keeps an artist's top three",
      four(eligible(vk)), ["uri:f1", "uri:f2", "uri:f3"])
check("a vetoed track promotes the artist's 4th, who still ends with 3",
      four(eligible(vk, {(N("Four Act"), N("Four Tune 2"))})),
      ["uri:f1", "uri:f3", "uri:f4"])
check("a live-titled track promotes the next by the same artist",
      [u for u in eligible(vk) if u.startswith("uri:l")],
      ["uri:l2", "uri:l3", "uri:l4"])
check("a track the other playlist already placed promotes the next too",
      four(eligible(vk, fresh=lambda r: r["spotify_track_uri"] != "uri:f1")),
      ["uri:f2", "uri:f3", "uri:f4"])
check("a pin by the artist holds one of their three slots",
      four(eligible(vk, pinned=[{"artist_name": "Four Act"}])),
      ["uri:f1", "uri:f2"])

# fresh() is asked lazily, after the previous row was placed: one playlist's
# own placements are seen by the rows behind them, and a row refused that way
# costs no slot either.
placed = set()
def fresh_once(r):
    return r["track_name"] not in placed
out = []
# Another act, so the cap cannot be what refuses the duplicate.
for r in running.eligible_known(vk + [dict(vk_by["uri:f1"], artist_name="Dup Act",
                                           spotify_track_uri="uri:f1-dup")],
                                set(), fresh_once):
    placed.add(r["track_name"]); out.append(r["spotify_track_uri"])
check("fresh() is evaluated after each placement, not up front",
      "uri:f1-dup" in out, False)

caps = [{"artist_name": a} for a in ("Skrillex", "SKRILLEX", "Skrillex", "Skrillex")]
check("cap_per_artist keys on the normalised name",
      len(list(running.cap_per_artist(caps, 3))), 3)

# --- the Workout near-tie boost (D1) ------------------------------------
# A hand-built playlist of what the listener actually runs to is a prior: a
# member beats a non-member only when it is within RUN_PREFER_MARGIN of it.
M = config.RUN_PREFER_MARGIN
check("RUN_PREFER_MARGIN is 0.25", M, 0.25)

def prefer_rows(member_score):
    return [
        {"spotify_track_uri": "u:non", "artist_name": "A", "track_name": "Non",
         "score": 1.0, "uris": ["u:non"]},
        {"spotify_track_uri": "u:mem", "artist_name": "B", "track_name": "Mem",
         "score": member_score, "uris": ["u:mem-single", "u:mem"]},
    ]
members = {"uris": {"u:mem-single"}, "songs": set()}
check("a member at 0.81x a non-member's score beats it",
      [r["spotify_track_uri"] for r in running.apply_prefer(prefer_rows(0.81), members, M)],
      ["u:mem", "u:non"])
check("...one at 0.79x does not",
      [r["spotify_track_uri"] for r in running.apply_prefer(prefer_rows(0.79), members, M)],
      ["u:non", "u:mem"])
check("any pressing's URI makes a member",
      [r.get("preferred", False) for r in running.apply_prefer(prefer_rows(0.5), members, M)],
      [False, True])
by_song = {"uris": set(), "songs": {(N("B"), running._title_key("Mem - Radio Edit"))}}
check("...and so does (lead artist, song key)",
      running.apply_prefer(prefer_rows(0.9), by_song, M)[0]["spotify_track_uri"],
      "u:mem")
check("the boost does not touch the input rows",
      prefer_rows(0.81)[1]["score"], 0.81)
check("no members leaves the order alone",
      [r["spotify_track_uri"] for r in running.apply_prefer(
          prefer_rows(0.99), {"uris": set(), "songs": set()}, M)],
      ["u:non", "u:mem"])
check("a vetoed member is still absent",
      [r["spotify_track_uri"] for r in running.eligible_known(
          running.apply_prefer(prefer_rows(0.9), members, M),
          {(N("B"), N("Mem"))}, lambda r: True)],
      ["u:non"])


class PlaylistSp:
    """consolidate.find_playlist / read_playlist, answered from a dict."""
    def __init__(self, playlists):
        self.playlists = playlists      # name -> list of (uri, title, [artists])
        self.calls = []
    def get(self, path, params=None):
        self.calls.append(path)
        if path == "/me/playlists":
            return {"items": [{"id": f"pl-{i}", "name": n}
                              for i, n in enumerate(self.playlists)], "next": None}
        for i, (n, tracks) in enumerate(self.playlists.items()):
            if path.startswith(f"/playlists/pl-{i}/items"):
                return {"items": [{"added_at": "", "item": {
                    "uri": u, "name": t, "artists": [{"name": a} for a in arts]}}
                    for u, t, arts in tracks], "next": None}
        return {"_status": 404}

psp = PlaylistSp({"Workout · Claude": [
    ("u:1", "Nuclear (Hands Up)", ["Zomboy"]),
    ("u:2", "Tough - Gravagerz Remix", ["Gravagerz", "Other"])]})
mem = running.prefer_members(psp, ["Workout · Claude"])
check("prefer_members reads the playlist's URIs",
      mem["uris"], {"u:1", "u:2"})
check("...and (lead artist, song key) pairs",
      mem["songs"], {(N("Zomboy"), running._title_key("Nuclear (Hands Up)")),
                     (N("Gravagerz"), running._title_key("Tough - Gravagerz Remix"))})
check("...read-only: it only ever GETs", hasattr(psp, "put"), False)
missing = running.prefer_members(psp, ["Workout · claude"])
check("a name that is not an exact match is a warning, not an exit",
      missing, {"uris": set(), "songs": set()})

# --- pins are charged their MEDIAN completed play (C12) -----------------
# max() is the bug the known pool already fixed: the odd play reports far more
# than the track runs, and a pin charged 900k for a 3-minute record overcharged
# the garage run by 4.8 minutes. A skip's ms_played is not a length at all, and
# a polled play's is the whole track, so it is the fallback.
pc = duckdb.connect()
pc.execute("""
CREATE TABLE plays AS SELECT * FROM (VALUES
  ('uri:pin', 'Pinned Tune', 'Pin Act', 180.0, 'trackdone', 180000, FALSE),
  ('uri:pin', 'Pinned Tune', 'Pin Act', 190.0, 'trackdone', 190000, FALSE),
  ('uri:pin', 'Pinned Tune', 'Pin Act', 900.0, 'trackdone', 900000, FALSE),
  ('uri:pin', 'Pinned Tune', 'Pin Act',  40.0, 'fwdbtn',     40000, FALSE),
  -- A polled play beside export evidence changes nothing.
  ('uri:pin', 'Pinned Tune', 'Pin Act', 999.0, NULL,        999000, TRUE),
  ('uri:pp',  'Polled Pin',  'Pin Act', 222.0, NULL,        222000, TRUE)
) t(spotify_track_uri, track_name, artist_name, played_seconds, reason_end,
    ms_played, ms_played_estimated)""")
pins = running.resolve_pins(pc, [
    {"playlist": "", "artist_name": "Pin Act", "track_name": "Pinned Tune"},
    {"playlist": "", "artist_name": "pin act", "track_name": "POLLED PIN"}], "dubstep")
check("pin durations of 180k, 190k and 900k give the median, 190k",
      pins[0]["duration_ms"], 190000)
check("a pin heard only by the poller takes the polled ms_played",
      pins[1]["duration_ms"], 222000)

# --- the known top-up has a floor (C12) ---------------------------------
# When discovery leaves time over, known tracks fill it — but not at any score.
# Matt Sassari's "Give It To Me - Full Vocal Mix" (0.186) went in as filler; a
# playlist a few minutes short beats one padded with what the listener skips.
T = config.RUN_TOPUP_MIN_SCORE
check("RUN_TOPUP_MIN_SCORE is 0.20", T, 0.20)
M3 = 180_000
spare = [{"spotify_track_uri": u, "score": s, "duration_ms": d} for u, s, d in [
    ("t:kept", 0.9, M3), ("t:ok", 0.5, M3), ("t:long", 0.4, 900_000),
    ("t:filler", 0.186, M3), ("t:edge", T, M3)]]
placed_rows = []
got = running.known_topup(spare, {"t:kept"}, 12 * 60_000,
                          lambda r: placed_rows.append(r) or r)
check("a 0.186-score row is not topped up; one at the floor is",
      [r["spotify_track_uri"] for r in got], ["t:ok", "t:edge"])
check("...a row too long for the gap is passed over, not fatal",
      "t:long" in [r["spotify_track_uri"] for r in got], False)
check("...an already-kept row is not taken twice",
      "t:kept" in [r["spotify_track_uri"] for r in got], False)
check("...and every row taken is placed", placed_rows, got)
check("no spare time takes nothing",
      running.known_topup(spare, set(), 0, lambda r: r), [])

# The floor is on the track's OWN score. The Workout boost breaks near-ties
# between rows; it is not a lower bar for members, or a member at 0.17 would
# clear 0.20 as 0.2125 and pad the run with a track the listener lets go.
boosted = running.apply_prefer(
    [{"spotify_track_uri": "t:mem-low", "artist_name": "B", "track_name": "Low",
      "score": 0.17, "duration_ms": M3},
     {"spotify_track_uri": "t:mem-edge", "artist_name": "B", "track_name": "Edge",
      "score": T, "duration_ms": M3}],
    {"uris": {"t:mem-low", "t:mem-edge"}, "songs": set()}, config.RUN_PREFER_MARGIN)
check("a boosted member ranks on its boosted score",
      [round(r["score"], 4) for r in boosted], [0.25, 0.2125])
check("...but the top-up floor tests its own: 0.17 stays out, 0.20 goes in",
      [r["spotify_track_uri"] for r in running.known_topup(
          boosted, set(), 12 * 60_000, lambda r: r)], ["t:mem-edge"])

# --- the durations cache keeps only answers (C12) -----------------------
# A 503 used to append {duration_ms: null}, which then read as "Spotify knows
# no length for this track" forever. A failure is not an answer.
appended = []
saved_append = running.append_jsonl
running.append_jsonl = lambda path, rec: appended.append((path.name, rec))


class TracksSp:
    def __init__(self, resp):
        self.resp, self.calls = resp, []
    def get(self, path, params=None):
        self.calls.append(path)
        return self.resp


try:
    dc = {}
    sp503 = TracksSp({"_status": 503, "_body": "unavailable"})
    check("a 503 from /tracks gives no duration",
          running.track_duration(sp503, "spotify:track:abc", dc), None)
    check("...and appends nothing", (appended, dc), ([], {}))
    running.track_duration(sp503, "spotify:track:abc", dc)
    check("...so the next call asks again", len(sp503.calls), 2)
    check("no response at all appends nothing either",
          (running.track_duration(TracksSp(None), "spotify:track:abc", dc),
           appended), (None, []))
    sp200 = TracksSp({"id": "abc", "duration_ms": 201_000})
    check("a 200 gives its duration",
          running.track_duration(sp200, "spotify:track:abc", dc), 201_000)
    check("...and is cached with its status",
          appended, [("track_durations.jsonl",
                      {"key": "abc", "status": 200, "duration_ms": 201_000})])
    running.track_duration(sp200, "spotify:track:abc", dc)
    check("...so it is asked once", len(sp200.calls), 1)
    # Records written before the status existed: an empty one may have been a
    # failure, so it is asked ONCE more; one carrying a length never is.
    appended.clear()
    legacy = {"old": {"key": "old", "duration_ms": None},
              "good": {"key": "good", "duration_ms": 150_000}}
    sp_leg = TracksSp({"id": "old", "duration_ms": 199_000})
    check("a legacy empty entry is re-asked",
          running.track_duration(sp_leg, "spotify:track:old", legacy), 199_000)
    running.track_duration(sp_leg, "spotify:track:old", legacy)
    check("...once: the new record then wins", len(sp_leg.calls), 1)
    check("a legacy entry with a length is never re-asked",
          (running.track_duration(sp_leg, "spotify:track:good", legacy),
           len(sp_leg.calls)), (150_000, 1))
finally:
    running.append_jsonl = saved_append

# --- the borderline listing is in a total order --------------------------
# ORDER BY best alone left ties to DuckDB's parallel sort, so two runs of the
# same data could list different artists under the LIMIT.
bc = duckdb.connect()
bc.execute("""
CREATE TABLE artist_tags AS SELECT * FROM (VALUES
  ('Zz Tie', 'dubstep', 2, TRUE), ('Zz Tie', 'pop', 1, TRUE),
  ('Mm Tie', 'dubstep', 2, TRUE), ('Mm Tie', 'pop', 1, TRUE),
  ('Aa Tie', 'dubstep', 2, TRUE), ('Aa Tie', 'pop', 1, TRUE)
) t(artist_name, tag, tag_count, is_genre)""")
running.build_artist_clusters(bc)
bc.execute("""CREATE TABLE plays (artist_name VARCHAR, played_seconds DOUBLE,
    month DATE, ms_played_estimated BOOLEAN)""")
import contextlib, io
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    running.report(bc, [], dry=True)
lines = [ln.split()[0] for ln in buf.getvalue().splitlines() if " Tie " in ln]
check("tied borderline artists are listed by name", lines, ["Aa", "Mm", "Zz"])

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

# --- the known fill may take ONE track past its budget -------------------
# The 09-26 dubstep run came in at a known share of 0.597. fill_to_target
# stopped 0.93 min under its 144-min budget because nothing eligible was that
# short, and discovery took the rest. fill_known takes one more track, the next
# in rank order that fits the whole target, and discovery gets what is left.
MIN = 60_000
BUDGET, TARGET = int(240 * MIN * config.RUN_KNOWN_FRACTION), 240 * MIN
ranked = ([{"spotify_track_uri": f"k{i}", "score": 1.0 - i / 100,
            "duration_ms": 210_000} for i in range(40)]            # 140 min
          + [{"spotify_track_uri": "k40", "score": 0.55, "duration_ms": 184_200},
             {"spotify_track_uri": "next", "score": 0.50, "duration_ms": 192_000},
             {"spotify_track_uri": "short", "score": 0.30, "duration_ms": 118_800}])
ids = lambda rs: [r["spotify_track_uri"] for r in rs]
mins = lambda rs: sum(r["duration_ms"] for r in rs) / MIN
old = running.fill_to_target(ranked, BUDGET)
check("fill_to_target alone stops at 143.07 min, a 0.596 share",
      (round(mins(old), 2), round(mins(old) / 240, 3)), (143.07, 0.596))
new = running.fill_known(ranked, BUDGET, TARGET)
check("fill_known reaches the 0.60 known share",
      mins(new) / 240 >= config.RUN_KNOWN_FRACTION, True)
check("...with exactly one track more", ids(new), ids(old) + ["next"])
check("...the next ranked one (3.2 min), not the smallest overshoot (1.98)",
      "short" in ids(new), False)
check("...and the playlist can still close at the target: discovery gets "
      "the 93.73 min left", round(240 - mins(new), 2), 93.73)

# It takes time past the known budget, as the top-up does, so the top-up's
# rules hold: the floor on the track's own score, and no unknown lengths.
floored = [dict(r, score=0.15) if r["spotify_track_uri"] == "next" else r
           for r in ranked]
check("a next track under the top-up floor is passed over for the one behind it",
      ids(running.fill_known(floored, BUDGET, TARGET))[-1], "short")
unsized = [dict(r, duration_ms=None) if r["spotify_track_uri"] == "next" else r
           for r in ranked]
check("...and so is one with no known length",
      ids(running.fill_known(unsized, BUDGET, TARGET))[-1], "short")
tight = running.fill_known(ranked, BUDGET, int(145.5 * MIN))
check("it must fit the WHOLE target: at 145.5 min only the 1.98 fits",
      (ids(tight)[-1], mins(tight) <= 145.5), ("short", True))
check("...and at 145 min nothing does",
      ids(running.fill_known(ranked, BUDGET, 145 * MIN)), ids(old))

# No overshoot once the budget is met, or when the pool has nothing left.
exact = [{"spotify_track_uri": f"e{i}", "score": 1.0, "duration_ms": 180_000}
         for i in range(49)]                                     # 48 x 3 = 144
check("at exactly the fraction nothing more is taken",
      ids(running.fill_known(exact, BUDGET, TARGET)),
      ids(running.fill_to_target(exact, BUDGET)))
check("...which is 144 min", mins(running.fill_known(exact, BUDGET, TARGET)), 144.0)
few = ranked[:10]
check("a pool used up under the budget takes nothing extra",
      ids(running.fill_known(few, BUDGET, TARGET)), ids(few))

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
