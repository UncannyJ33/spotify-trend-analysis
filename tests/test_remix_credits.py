"""tests/test_remix_credits.py — the remixer credit the export throws away.

No network. Synthetic plays rows only.

Spotify bills a remix to the ORIGINAL artist, so `Halsey - Ian Asher Remix`
reads as pop. In this library that hides 329 tracks and 89 hours, concentrated
in dance music, where remixes and edits are how most of the material arrives.
The report surface cannot verify this: it prints counts, and a regex that
extracts the right NUMBER of credits while extracting the wrong ones looks
identical in it. So the cases that matter are pinned here — the format
descriptors that must never become artists, and the invariant that fails
silently.
"""
import sys
sys.path.insert(0, ".")
import duckdb
import credits

con = duckdb.connect()
con.execute("""
CREATE TABLE plays AS SELECT * FROM (VALUES
  -- The whole point: a pop record remixed by a speed-garage producer.
  ('uri:asher', 'Die For Me - Ian Asher Remix', 'Halsey', 600.0, NULL),
  -- Format descriptors. "Radio" is not a person; unchecked it would be
  -- credited with 352 minutes of this library's real listening.
  ('uri:radio', 'Feel So Close - Radio Edit', 'Calvin Harris', 500.0, NULL),
  ('uri:ext',   'Like A Bitch - Extended Mix', 'Zomboy', 400.0, NULL),
  ('uri:orig',  'Levels - Original Version', 'Avicii', 300.0, NULL),
  ('uri:club',  'My Best Life - Club Mix', 'KSHMR', 200.0, NULL),
  -- A remaster has no remixer. Including `remaster` as a type captured
  -- "2012 -" out of this exact shape.
  ('uri:rmst',  'War Pigs - 2012 - Remaster', 'Black Sabbath', 700.0, NULL),
  -- Self-remix: one performer, not two.
  ('uri:self',  'Bad Habit - Gravagerz Remix', 'Gravagerz', 100.0, NULL),
  -- A remix suffix naming no person. It must still be EXTRACTED (the regex
  -- cannot know), and then fail to resolve upstream in Stage 2.
  ('uri:blm',   'ROCKSTAR - BLM REMIX', 'DaBaby', 900.0, NULL),
  -- Feature and remixer on the same track: three distinct performers.
  ('uri:both',  'Song (feat. Guest) - Someone Remix', 'Primary', 800.0, NULL),
  -- No suffix at all.
  ('uri:plain', 'Just A Song', 'Nobody', 50.0, NULL)
) t(spotify_track_uri, track_name, artist_name, played_seconds, track_artists)""")
# The poller has never run on this checkout, so track_artists is NULL for every
# row. Typed explicitly: a bare NULL in VALUES lands as INTEGER and string_split
# refuses it.
con.execute("ALTER TABLE plays ALTER track_artists TYPE VARCHAR")

credits.build_track_credits(con)

failures = []
def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")

def performers(uri, ctype=None):
    sql = "SELECT artist_name FROM track_credits WHERE spotify_track_uri = ?"
    args = [uri]
    if ctype:
        sql += " AND credit_type = ?"
        args.append(ctype)
    return sorted(r[0] for r in con.execute(sql + " ORDER BY 1", args).fetchall())

# --- the case the whole change exists for -------------------------------
check("remixer extracted from the title suffix",
      performers("uri:asher", "remixer"), ["Ian Asher"])
check("original artist still credited",
      performers("uri:asher", "album_artist"), ["Halsey"])

# --- format descriptors must never become artists -----------------------
for uri, junk in [("uri:radio", "Radio"), ("uri:ext", "Extended"),
                  ("uri:orig", "Original"), ("uri:club", "Club")]:
    check(f"{junk!r} is a format, not a performer",
          performers(uri, "remixer"), [])
check("a remaster yields no remixer", performers("uri:rmst", "remixer"), [])
check("no year fragment leaks in as an artist",
      any("2012" in a for a in performers("uri:rmst")), False)

# --- dedupe -------------------------------------------------------------
check("self-remix is one performer, not two",
      performers("uri:self"), ["Gravagerz"])
check("feature and remixer coexist",
      performers("uri:both"), ["Guest", "Primary", "Someone"])
check("plain title gets only the album artist",
      performers("uri:plain"), ["Nobody"])

# --- junk degrades to a review row, never to bad data -------------------
# 'BLM' is extracted because the regex cannot tell a person from a slogan.
# It then fails to resolve in MusicBrainz and is answered with IGNORE. The
# alternative — refusing anything unrecognised — would drop real remixers.
check("an unrecognisable remix name is still extracted",
      performers("uri:blm", "remixer"), ["BLM"])

# --- the invariant that fails silently ----------------------------------
# Stage 3 splits each play's time across that play's performers. A performer
# listed twice on one track would be handed that track's time twice.
dupes = con.execute("""
    SELECT count(*) FROM (
        SELECT spotify_track_uri, artist_name
        FROM track_credits GROUP BY 1, 2 HAVING count(*) > 1)
""").fetchone()[0]
check("no performer is credited twice on one track", dupes, 0)

# n_performers must match the rows actually written, or the weighting
# denominator disagrees with the numerator.
mismatch = con.execute("""
    SELECT count(*) FROM (
        SELECT spotify_track_uri, any_value(n_performers) AS claimed,
               count(*) AS actual
        FROM track_credits GROUP BY 1 HAVING claimed <> actual)
""").fetchone()[0]
check("n_performers matches the rows written", mismatch, 0)

# Every track that had a play must still be represented: recrediting must not
# lose a track, only add performers to it.
lost = con.execute("""
    SELECT count(*) FROM (
        SELECT DISTINCT spotify_track_uri FROM plays
        EXCEPT SELECT DISTINCT spotify_track_uri FROM track_credits)
""").fetchone()[0]
check("no track lost by recrediting", lost, 0)

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
