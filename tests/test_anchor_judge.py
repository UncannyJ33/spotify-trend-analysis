"""tests/test_anchor_judge.py — who judges a Stage 8 anchor, and on what.

No network. Synthetic plays and artist_tags only.

SJ, going through the frontier playlists: "how is the Ian Asher Colors remix
considered indie frontier? same for Lights by Ellie Goulding". Two faults, and
the shapes below are the real artists' tag vectors:
  - an artist qualified by carrying ANY spec tag. Halsey carries indie pop(3)
    in 32 votes of genre and Ellie Goulding indie pop + indie folk (5 of 31);
    heavily played, they took four of the six indie anchors;
  - a track was judged on the export's ALBUM artist, so an Ian Asher garage
    remix of Halsey was an indie track, and a Disco Lines remix of KISS a
    heavy metal one.
"""
import sys
sys.path.insert(0, ".")
import duckdb
import config
import credits
import playlists
import running

failures = []
def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")

INDIE = ["indie pop", "indie folk", "folk pop"]
GARAGE = ["speed garage", "stutter house", "uk garage"]

con = duckdb.connect()
con.execute("""
CREATE TABLE plays AS SELECT * FROM (VALUES
  -- Halsey-shaped and Ellie-shaped: the most-played tracks in the table.
  ('Pop Singer', 'Colors',                      'uri:colors',  9000.0),
  ('Pop Singer', 'Colors - Garage Guy Remix',   'uri:colors-r',8800.0),
  ('Pop Singer', 'Without Me - Melodic Guy Remix', 'uri:wm-r', 8600.0),
  ('Pop Belle',  'Lights',                      'uri:lights',  8400.0),
  ('Pop Belle',  'On My Mind',                  'uri:omm',     8200.0),
  -- genuinely indie
  ('Folk Act',   'Half of forever',             'uri:half',    3000.0),
  -- the KISS shape, with the album artist made to QUALIFY on its own so only
  -- the judge can keep the remix out
  ('Glam Band',  'Rock Night',                  'uri:rock',    2000.0),
  ('Glam Band',  'Made For Lovin - Disco Guy Remix', 'uri:disco', 2500.0),
  -- Metallica-shaped: heavy metal 42 split against thrash metal 67
  ('Metal Band', 'Enter Sandman',               'uri:sandman',  500.0),
  -- the polled pressing: Spotify's artists array makes Bass Guy a feature
  ('Dream Band', 'Black Out Days - Bass Guy Remix', 'uri:bod',  2800.0),
  -- a remixer nobody has tagged: the album artist is indie, the record is not
  ('Folk Act',   'Sweet Song - Nobody Known Remix', 'uri:nk',   2900.0)
) t(artist_name, track_name, spotify_track_uri, played_seconds)""")
con.execute("ALTER TABLE plays ADD COLUMN month DATE DEFAULT DATE '2026-06-01'")
con.execute("ALTER TABLE plays ADD COLUMN ms_played_estimated BOOLEAN DEFAULT FALSE")

con.execute("""
CREATE TABLE artist_tags AS SELECT * FROM (VALUES
  -- Halsey: indie pop 3 of 32 -> 0.09
  ('Pop Singer', 'pop', 6), ('Pop Singer', 'electropop', 4),
  ('Pop Singer', 'alternative pop', 4), ('Pop Singer', 'indie pop', 3),
  ('Pop Singer', 'dance-pop', 3), ('Pop Singer', 'synth-pop', 3),
  ('Pop Singer', 'alternative r&b', 3), ('Pop Singer', 'dark pop', 3),
  ('Pop Singer', 'electronic', 3),
  -- Ellie Goulding: indie pop + indie folk 5 of 31 -> 0.16
  ('Pop Belle', 'electropop', 7), ('Pop Belle', 'dance-pop', 5),
  ('Pop Belle', 'pop', 5), ('Pop Belle', 'synth-pop', 4),
  ('Pop Belle', 'indie pop', 3), ('Pop Belle', 'indie folk', 2),
  ('Pop Belle', 'electronic', 5),
  ('Folk Act', 'indie folk', 1), ('Folk Act', 'indie pop', 1),
  ('Folk Act', 'singer-songwriter', 1),
  ('Garage Guy', 'speed garage', 1), ('Garage Guy', 'stutter house', 1),
  ('Garage Guy', 'uk garage', 1),
  ('Melodic Guy', 'melodic dubstep', 3), ('Melodic Guy', 'future bass', 2),
  -- Glam Band: heavy metal 10 of 20 -> 0.5, so it anchors on its own
  ('Glam Band', 'heavy metal', 10), ('Glam Band', 'hard rock', 10),
  ('Disco Guy', 'house', 2), ('Disco Guy', 'dance', 2), ('Disco Guy', 'pop', 2),
  -- Metallica: 42 of 164 -> 0.256
  ('Metal Band', 'heavy metal', 42), ('Metal Band', 'thrash metal', 67),
  ('Metal Band', 'speed metal', 30), ('Metal Band', 'hard rock', 25),
  ('Dream Band', 'indie pop', 5), ('Dream Band', 'dream pop', 3),
  ('Bass Guy', 'dubstep', 5), ('Bass Guy', 'riddim', 2)
) t(artist_name, tag, tag_count)""")
con.execute("ALTER TABLE artist_tags ADD COLUMN is_genre BOOLEAN DEFAULT TRUE")
# Stage 8 reads the title, never credit_type — but the table is here so a
# future "use track_credits" change meets the case that breaks it: credits.py
# types every non-first poller artist as `featured`, remixers included.
con.execute("""
CREATE TABLE track_credits AS SELECT * FROM (VALUES
  ('uri:bod', 'Dream Band', 'album_artist', 'poller'),
  ('uri:bod', 'Bass Guy',   'featured',     'poller')
) t(spotify_track_uri, artist_name, credit_type, credit_source)""")

def uris(rows):
    return [r["spotify_track_uri"] for r in rows]

indie = playlists.select_anchor_tracks(con, INDIE)
garage = playlists.select_anchor_tracks(con, GARAGE)
metal = playlists.select_anchor_tracks(con, ["heavy metal"])
bass = playlists.select_anchor_tracks(con, ["dubstep"])

# --- the share gate ------------------------------------------------------
check("Halsey-shaped artist (indie 0.09) no longer anchors indie",
      [r for r in indie if r["artist_name"] == "Pop Singer"], [])
check("Ellie-shaped artist (indie 0.16) no longer anchors indie",
      [r for r in indie if r["artist_name"] == "Pop Belle"], [])
check("a genuinely indie artist still does", "uri:half" in uris(indie), True)
check("Metallica-shaped (heavy metal 0.26) still anchors heavy metal",
      "uri:sandman" in uris(metal), True)

# --- the judge -----------------------------------------------------------
check("a garage remix of a pop artist anchors garage...",
      uris(garage), ["uri:colors-r"])
check("...judged on its remixer",
      [r["judge"] for r in garage], ["Garage Guy"])
check("...and is not indie", "uri:colors-r" in uris(indie), False)
check("a melodic remix of the pop artist anchors nothing here",
      "uri:wm-r" in uris(indie) + uris(garage), False)
check("a Disco Lines-shaped remix of KISS no longer anchors heavy metal",
      "uri:disco" in uris(metal), False)
check("while the band's own record still does", "uri:rock" in uris(metal), True)
check("a polled pressing with its remixer typed 'featured' is judged by the "
      "title's remixer", uris(bass), ["uri:bod"])
check("...so it no longer anchors its album artist's indie",
      "uri:bod" in uris(indie), False)
check("a remix whose remixer has no tags cannot anchor, even where the "
      "album artist qualifies", "uri:nk" in uris(indie), False)

# --- reuse, not copies ---------------------------------------------------
check("Stage 10 uses credits.remix_credit itself",
      running.remix_credit is credits.remix_credit, True)
check("Stage 10 uses playlists.register_song_key itself",
      running.register_song_key is playlists.register_song_key, True)
check("remix_credit in SQL agrees with Python (and gives NULL for no remix)",
      con.execute("SELECT remix_credit('Colors - Garage Guy Remix'), "
                  "remix_credit('Bounce - Radio Edit'), remix_credit(NULL)"
                  ).fetchone(), ("Garage Guy", None, None))

# --- one song, one anchor -------------------------------------------------
dd = duckdb.connect()
dd.execute("""
CREATE TABLE plays AS SELECT * FROM (VALUES
  -- two URIs, one song: the dry run anchored both
  ('Solo Act',   'cowboy killers',        'uri:ck1',  5000.0),
  ('Solo Act',   'cowboy killers',        'uri:ck2',  4000.0),
  ('Solo Act',   'second song',           'uri:ss',   1000.0),
  ('Solo Act',   'third song',            'uri:ts',    500.0),
  ('Solo Act',   'fourth song',           'uri:fs',    300.0),
  -- an original and its remix, both qualifying for garage
  ('Garage Diva', 'The Days',             'uri:days',   3000.0),
  ('Garage Diva', 'The Days - Garage Guy Remix', 'uri:days-r', 3500.0),
  -- an indie original whose garage remix belongs elsewhere
  ('Folk Act',   'Anthem',                'uri:anthem',   2000.0),
  ('Folk Act',   'Anthem - Garage Guy Remix', 'uri:anthem-r', 2500.0)
) t(artist_name, track_name, spotify_track_uri, played_seconds)""")
dd.execute("ALTER TABLE plays ADD COLUMN month DATE DEFAULT DATE '2026-06-01'")
dd.execute("ALTER TABLE plays ADD COLUMN ms_played_estimated BOOLEAN DEFAULT FALSE")
dd.execute("""
CREATE TABLE artist_tags AS SELECT * FROM (VALUES
  ('Solo Act', 'indie rock', 1), ('Solo Act', 'indie pop', 1),
  ('Solo Act', 'alternative rock', 1),
  ('Garage Diva', 'uk garage', 1), ('Garage Diva', 'pop', 1),
  ('Garage Guy', 'speed garage', 1), ('Garage Guy', 'uk garage', 1),
  ('Folk Act', 'indie folk', 2), ('Folk Act', 'indie pop', 1)
) t(artist_name, tag, tag_count)""")
dd.execute("ALTER TABLE artist_tags ADD COLUMN is_genre BOOLEAN DEFAULT TRUE")

rock = playlists.select_anchor_tracks(dd, ["indie rock"])
check("one song, one anchor: the second pressing is not anchored",
      "uri:ck2" in uris(rock), False)
check("...and the artist's second slot passes to a different song",
      uris(rock), ["uri:ck1", "uri:ss"])
g = playlists.select_anchor_tracks(dd, GARAGE)
check("an original and its remix do not both anchor one playlist",
      [u for u in uris(g) if u.startswith("uri:days")], ["uri:days-r"])

specs = [{"label": "speed garage", "tags": GARAGE},
         {"label": "indie", "tags": INDIE},
         {"label": "indie rock", "tags": ["indie rock"]}]
run = dict(zip([s["label"] for s in specs],
               playlists.select_run_anchors(dd, specs)))
check("across playlists: the first in build order keeps the song",
      "uri:ck1" in uris(run["indie"]), True)
check("...the later playlist does not anchor it again, under either pressing",
      {"uri:ck1", "uri:ck2"} & set(uris(run["indie rock"])), set())
check("...and takes the artist's next unclaimed songs instead",
      uris(run["indie rock"]), ["uri:ts", "uri:fs"])
check("a remix anchoring garage does not keep its original out of indie",
      ("uri:anthem-r" in uris(run["speed garage"]),
       "uri:anthem" in uris(run["indie"])), (True, True))

# --- discovery: a stranger clears at least the anchor bar ------------------
dc = duckdb.connect()
dc.execute("""CREATE TABLE plays AS SELECT * FROM (VALUES ('Known Band'))
              t(artist_name)""")
dc.execute("""
CREATE TABLE recommendations AS SELECT * FROM (VALUES
  ('Papa Shaped',  'm-papa', 0.9), ('Motor Shaped', 'm-motor', 0.8),
  ('Edge Shaped',  'm-edge', 0.7), ('Zero Shaped',  'm-zero',  0.6),
  ('Known Band',   'm-known', 0.95)
) t(artist_name, mbid, score)""")
TAGS = {
    # heavy metal 1 of 26: admitted by "any tag", refused by the share
    "m-papa":  {"tags": [{"tag": "nu metal", "count": 8},
                         {"tag": "hard rock", "count": 17},
                         {"tag": "heavy metal", "count": 1}]},
    "m-motor": {"tags": [{"tag": "heavy metal", "count": 20},
                         {"tag": "hard rock", "count": 39}]},
    # exactly on the bar: 11 of 44
    "m-edge":  {"tags": [{"tag": "heavy metal", "count": 11},
                         {"tag": "hard rock", "count": 33}]},
    # carries the tag with nobody behind it
    "m-zero":  {"tags": [{"tag": "heavy metal", "count": 0}]},
    "m-known": {"tags": [{"tag": "heavy metal", "count": 50}]},
}
cands = [c["artist_name"]
         for c in playlists.select_candidates(dc, ["heavy metal"], TAGS)]
check("discovery: a stranger at heavy metal 1 of 26 is refused",
      "Papa Shaped" in cands, False)
check("discovery: 0.34 and exactly 0.25 pass, in score order",
      cands, ["Motor Shaped", "Edge Shaped"])
check("discovery: a zero-count tag admits nobody", "Zero Shaped" in cands, False)
check("discovery: an empty spec admits nobody",
      playlists.select_candidates(dc, [], TAGS), [])
check("the bar is the anchors' bar", config.ANCHOR_MIN_TAG_SHARE, 0.25)

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
