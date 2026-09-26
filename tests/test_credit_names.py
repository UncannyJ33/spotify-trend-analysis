"""tests/test_credit_names.py — names the credit splitter must not cut.

No network. Synthetic plays rows only.

The title regex splits a "(feat. …)" blob on its separators, and a separator is
also a character real names carry. "(feat. Tyler, The Creator & Billie Essco)"
became the performers "Tyler" and "The Creator", neither of whom exists, and the
all-caps "GRAVITY (FEAT. TYLER, THE CREATOR)" made two more. An inner "ft."
inside a "(with …)" list was never split at all, so "21 Savage ft. Project Pat"
was one performer holding 5 hours, and the capital-X separator turned
"X Ambassadors" into "Ambassadors", which then resolved to an unrelated act.
Separately, `¥$` was dropped as an album artist for containing no ASCII letter,
leaving 11 hours credited to nobody under `album_artist_only`.

The report surface cannot see any of this — a wrong split still produces the
right NUMBER of names — so each shape is pinned here with its exact performer
set, which also catches the junk fragments a split leaves behind.
"""
import sys
sys.path.insert(0, ".")
import duckdb
import credits

con = duckdb.connect()
con.execute("""
CREATE TABLE plays AS SELECT * FROM (VALUES
  -- Album-artist plays that put the separator-bearing names in the library.
  ('uri:earf',  'EARFQUAKE',  'Tyler, The Creator', 300.0, NULL),
  ('uri:cs',    'Blind Faith', 'Chase & Status',    300.0, NULL),
  ('uri:ren',   'Renegades',   'X Ambassadors',     300.0, NULL),
  ('uri:sob',   'Anti',        'SOB X RBE',         300.0, NULL),
  ('uri:cxv',   'Filler',      'Calle x Vida',      300.0, NULL),
  ('uri:ac',    'Filler',      'A & C',             300.0, NULL),
  -- A quote inside a protected name must not break the SQL it is spliced into.
  ('uri:obr',   'Filler',      'O''Brien & Sons',   300.0, NULL),
  -- The comma name, in title case and in an all-caps title.
  ('uri:327',   '327 (feat. Tyler, The Creator & Billie Essco)', 'Westside Gunn', 500.0, NULL),
  ('uri:grav',  'GRAVITY (FEAT. TYLER, THE CREATOR)', 'Brent Faiyaz', 250.0, NULL),
  -- "A & C" is protected and sits inside "MIA & Cool" — the boundaries must
  -- keep it from matching mid-word.
  ('uri:mia',   'Paper (feat. MIA & Cool)', 'Someone', 200.0, NULL),
  ('uri:with',  'Pressure (with Chase & Status)', 'Other', 200.0, NULL),
  ('uri:obrf',  'Reel (feat. O''Brien & Sons)', 'Other2', 200.0, NULL),
  -- An inner feature marker inside the list.
  ('uri:knife', 'Knife Talk (with 21 Savage ft. Project Pat)', 'Drake', 900.0, NULL),
  ('uri:rvs',   'Ritmo (feat. Rvssian featuring Clever)', 'Farruko', 700.0, NULL),
  ('uri:stack', 'Tune (with Anna, feat.Bea)', 'Lead', 100.0, NULL),
  -- The X that is an initial, not a separator.
  ('uri:sfp',   'Sucker for Pain (with Wiz Khalifa, Logic & Ty Dolla $ign feat. X Ambassadors)',
                'Lil Wayne', 400.0, NULL),
  ('uri:axb',   'Beat (feat. Ann x Bob)', 'Lead2', 100.0, NULL),
  ('uri:sobf',  'Tune (feat. SOB X RBE)', 'Lead3', 100.0, NULL),
  -- A lowercase x IS a separator, so a name carrying one needs protecting.
  ('uri:cxvf',  'Noche (feat. Calle x Vida)', 'Lead7', 100.0, NULL),
  -- Two protected names with no space between them: a match's trailing
  -- boundary consumes the comma the next match needs as its leading one.
  ('uri:adj',   'Tune (feat. Chase & Status,Tyler, The Creator)', 'Lead4', 100.0, NULL),
  -- A name only the poller knows still protects export-only titles.
  ('uri:pol',   'Cecilia', 'Lead5', 100.0, 'Lead5' || chr(31) || 'Simon & Garfunkel'),
  ('uri:sg',    'Echo (feat. Simon & Garfunkel)', 'Lead6', 100.0, NULL),
  -- A non-ASCII album artist is the export's own field, not a regex fragment.
  ('uri:yen',   'CARNIVAL - HOOLIGANS VERSION', '¥$', 660.0, NULL)
) t(spotify_track_uri, track_name, artist_name, played_seconds, track_artists)""")
# Typed explicitly: a column of mostly-NULL VALUES can land as INTEGER.
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

# --- comma names --------------------------------------------------------
check("'Tyler, The Creator' survives the comma split",
      performers("uri:327"), ["Billie Essco", "Tyler, The Creator", "Westside Gunn"])
check("an all-caps title maps to the canonical spelling",
      performers("uri:grav"), ["Brent Faiyaz", "Tyler, The Creator"])
fake = con.execute("""
    SELECT count(*) FROM track_credits
    WHERE lower(artist_name) IN ('tyler', 'the creator')
""").fetchone()[0]
check("no 'Tyler' / 'The Creator' fragment in any spelling", fake, 0)

# --- boundaries ---------------------------------------------------------
check("a protected name does not match mid-word",
      performers("uri:mia"), ["Cool", "MIA", "Someone"])
check("'Chase & Status' is one performer",
      performers("uri:with"), ["Chase & Status", "Other"])
check("a quoted protected name is kept whole",
      performers("uri:obrf"), ["O'Brien & Sons", "Other2"])
check("adjacent protected names are both kept whole",
      performers("uri:adj"), ["Chase & Status", "Lead4", "Tyler, The Creator"])
check("a poller-only name protects export titles",
      performers("uri:sg"), ["Lead6", "Simon & Garfunkel"])

# --- the inner feature marker -------------------------------------------
check("'ft.' inside a with-list splits",
      performers("uri:knife"), ["21 Savage", "Drake", "Project Pat"])
check("'featuring' inside a feat-list splits",
      performers("uri:rvs"), ["Clever", "Farruko", "Rvssian"])
check("'(with Anna, feat.Bea)' still yields both",
      performers("uri:stack"), ["Anna", "Bea", "Lead"])

# --- X is an initial, x is a separator ----------------------------------
check("'X Ambassadors' is not cut at the X",
      performers("uri:sfp"),
      ["Lil Wayne", "Logic", "Ty Dolla $ign", "Wiz Khalifa", "X Ambassadors"])
check("lowercase x still separates",
      performers("uri:axb"), ["Ann", "Bob", "Lead2"])
check("'SOB X RBE' is one performer",
      performers("uri:sobf"), ["Lead3", "SOB X RBE"])
check("a protected name carrying lowercase x is one performer",
      performers("uri:cxvf"), ["Calle x Vida", "Lead7"])

# --- the non-ASCII album artist -----------------------------------------
check("'¥$' is credited as album artist",
      performers("uri:yen", "album_artist"), ["¥$"])

# --- the invariants that fail silently ----------------------------------
masked = con.execute(
    "SELECT count(*) FROM track_credits WHERE contains(artist_name, chr(31))"
).fetchone()[0]
check("the internal mask never reaches output", masked, 0)

dupes = con.execute("""
    SELECT count(*) FROM (
        SELECT spotify_track_uri, artist_name
        FROM track_credits GROUP BY 1, 2 HAVING count(*) > 1)
""").fetchone()[0]
check("no performer is credited twice on one track", dupes, 0)

# Every track keeps an album-artist credit, or album_artist_only loses its
# listening time — the CARNIVAL shape.
orphans = con.execute("""
    SELECT count(*) FROM (
        SELECT DISTINCT spotify_track_uri FROM plays
        EXCEPT SELECT spotify_track_uri FROM track_credits
               WHERE credit_type = 'album_artist')
""").fetchone()[0]
check("every track has an album-artist credit", orphans, 0)

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
