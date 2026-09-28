"""tests/test_bracket_remix_credits.py — the remixer credit in brackets.

No network. Synthetic plays rows only.

"Falling (blackbear Remix)" is a blackbear record the export bills to Trevor
Daniel, exactly as "Die For Me - Ian Asher Remix" is an Ian Asher record billed
to Halsey — 28 tracks and 472 minutes here once the guards have had their say.
A naive bracket regex gets about a quarter of the real titles WRONG, so every
guard is pinned on the real title that needs it. The same rule lives twice —
credits.remix_credit for Stages 8 and 10, remixers_sql inside
build_track_credits — and the two are held to agreeing on every title here.
"""
import sys
sys.path.insert(0, ".")
import duckdb

import analyze
import config
import credits

failures = []
def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")

# (title, album artist, the remixer the title names)
CASES = [
    # --- the real remixers the dash rule missed --------------------------
    ("Falling (blackbear Remix)", "Trevor Daniel", "blackbear"),
    ("Drugs I Like (AVELLO Remix)", "nate band", "AVELLO"),
    ("High You Are (Branchez Remix)", "What So Not", "Branchez"),
    ("Fake ID (Coke & Rum Remix)", "Riton", "Coke & Rum"),
    ("Do It To It (Subtronics Remix)", "ACRAZE", "Subtronics"),
    ("Aarena (Knock2 Remix)", "ISOxo", "Knock2"),
    ("Facts (Charlie Heat Version)", "Kanye West", "Charlie Heat"),
    ("Undo (AWAY Remix)", "RL Grime", "AWAY"),
    ("Shady Intentions (REAPER Remix)", "NGHTMRE", "REAPER"),
    ("OMG (Young Money Remix)", "Jae Millz", "Young Money"),
    ("Lost My Mind (NGHTMRE Remix)", "Dillon Francis", "NGHTMRE"),
    ("Chlorine (Bodhi remix)", "George Daniel", "Bodhi"),
    # --- only the LAST group, only where it ends the title ---------------
    ("Bad Habits (feat. Tion Wayne & Central Cee) [Fumez The Engineer Remix]",
     "Ed Sheeran", "Fumez The Engineer"),
    ("DNA (Loving You) [feat. Hannah Boleyn] [Levity Remix]",
     "Billy Gillies", "Levity"),
    ("Mothership Reconnection (feat. Parliament/Funkadelic) - (Daft Punk Remix)",
     "Scott Grooves", "Daft Punk"),
    ("Body (Remix) [feat. ArrDee, E1 (3x3), ZT (3x3), Bugzy Malone]",
     "Tion Wayne", None),
    ("Hot (Remix) [feat. Gunna and Travis Scott]", "Young Thug", None),
    # --- stacked type words: the name is before all of them --------------
    ("Love Tonight (David Guetta Remix Edit)", "Shouse", "David Guetta"),
    # --- possessives -----------------------------------------------------
    ("Good Things Fall Apart (with Jon Bellion) [Tiësto's Big Room Remix]",
     "ILLENIUM", "Tiësto"),
    ("tell you straight (Louis The Child’s version)", "jigitz", "Louis The Child"),
    # --- a credit list is not a remix credit -----------------------------
    ("Heartbreak Anthem (with David Guetta & Little Mix)", "Galantis", None),
    ("Song (feat. Some Mix Crew)", "X", None),
    # --- a bare type names nobody ----------------------------------------
    ("Shotta Flow (feat. Blueface) [Remix]", "NLE Choppa", None),
    ("Party Girl (Remix)", "StaySolidRocky", None),
    # --- self-versions ---------------------------------------------------
    ("I Knew You Were Trouble (Taylor's Version)", "Taylor Swift", None),
    ("Love Story (Taylor’s Version)", "Taylor Swift", None),
    ("Main Hoon Hero Tera (Armaan Malik Version)", "Armaan Malik", None),
    ("I need your kiss (sadagain version)", "sadagain", None),
    # --- formats and descriptors -----------------------------------------
    ("Song (Radio Edit)", "X", None),
    ("Song (Extended Mix)", "X", None),
    ("Song (Original Mix)", "X", None),
    ("Song (Sped Up Version)", "X", None),
    ("Song (Slowed Version)", "X", None),
    ("Song (Male Version)", "X", None),
    ("Song (Female Version)", "X", None),
    ("Song (Film Version)", "X", None),
    ("Song (VIP Mix)", "X", None),
    ("Song (2019 Remix)", "X", None),
    ("DYKILY (2017 Demo Version)", "Joji", None),
    ("braincells (dnb edit)", "tomcbumpz", None),
    ("Song (garage edit)", "X", None),
    ("Song (house remix)", "X", None),
    ("Song (techno edit)", "X", None),
    ("Honesty - (Jersey Club Remix)", "Pink Sweat$", None),
    ("Main Rang Sharbaton Ka (Lofi Mix)", "Pritam", None),
    ("Theme from Superman (Official Trailer Version)", "John Murphy", None),
    ("The Young Weaver (Renaissance Piano Version)", "Matteo Myderwyk", None),
    ("Hiding [Original Mix] - Hiding (feat. The Nicholas) [Original Mix]",
     "San Holo", None),
    # --- both forms: the dash rule first, the bracket only where it found
    # no remixer, and then followed by nothing but that final segment -----
    ("Song (Bracket Guy Remix) - Dash Guy Remix", "X", "Dash Guy"),
    ("Doses & Mimosas (Vintage Culture & Zerky Remix) - Radio Edit",
     "Vintage Culture", "Vintage Culture & Zerky"),
    ("I KNOW I KNOW (SAiiLOR Remix) - TikTok Version", "SAiiLOR", None),
    ("Song (Bracket Guy Remix) - 2012 Remaster", "X", "Bracket Guy"),
    # --- the dash rule as it was -----------------------------------------
    ("Die For Me - Ian Asher Remix", "Halsey", "Ian Asher"),
    ("Feel So Close - Radio Edit", "Calvin Harris", None),
    ("War Pigs - 2012 - Remaster", "Black Sabbath", None),
    ("Song - 2019 Remix", "X", None),
    ("ROCKSTAR - BLM REMIX", "DaBaby", "BLM"),
    # the new descriptor words reach the dash form too
    ("Patha Patha Gugu - Drill Remix", "Trinix Remix", None),
    ("I Remember - Vocal Mix", "deadmau5", None),
    ("Silhouettes - Original Radio Edit", "Avicii", None),
    ("Plain Title", "Nobody", None),
    ("", "Nobody", None),
]

# --- Python: every case ----------------------------------------------------
for title, artist, want in CASES:
    check(f"remix_credit({title!r})", credits.remix_credit(title, artist), want)
check("remix_credit(None) is None", credits.remix_credit(None), None)
# Without the album artist the self-version guard cannot run; callers pass it.
check("...the album artist is what refuses 'Taylor'",
      credits.remix_credit("Love Story (Taylor's Version)"), "Taylor")

# --- Python and SQL agree, title for title ----------------------------------
con = duckdb.connect()
con.execute("CREATE TABLE titles (track_name VARCHAR, album_artist VARCHAR)")
con.executemany("INSERT INTO titles VALUES (?, ?)", [(t, a) for t, a, _ in CASES])
con.execute("INSERT INTO titles VALUES (NULL, NULL), ('Falling (blackbear Remix)', NULL)")
rows = con.execute(
    f"SELECT track_name, album_artist, remixer FROM ({credits.remixers_sql('titles')})"
).fetchall()
check("remixers_sql keeps every row", len(rows), len(CASES) + 2)
disagree = [(t, a, s, credits.remix_credit(t, a)) for t, a, s in rows
            if s != credits.remix_credit(t, a)]
check("Python and SQL agree on every title", disagree, [])
check("remixers_sql adds only `remixer`",
      [d[0] for d in con.execute(
          f"DESCRIBE SELECT * FROM ({credits.remixers_sql('titles')})").fetchall()],
      ["track_name", "album_artist", "remixer"])

# --- through build_track_credits --------------------------------------------
# One track per case, then the invariants that fail silently.
plays = [(f"uri:{i}", t, a, 60.0 * (i + 1), None) for i, (t, a, _) in enumerate(CASES)
         if t]
# The poller's credits replace the regex for a track it has seen, bracket
# form included: no export remixer row beside them.
plays.append(("uri:polled", "Rushing Back (Petit Biscuit Remix)", "Flume", 300.0,
              "Flume\x1fPetit Biscuit"))
tc = duckdb.connect()
tc.execute("""CREATE TABLE plays (spotify_track_uri VARCHAR, track_name VARCHAR,
              artist_name VARCHAR, played_seconds DOUBLE, track_artists VARCHAR)""")
tc.executemany("INSERT INTO plays VALUES (?, ?, ?, ?, ?)", plays)
credits.build_track_credits(tc)

def remixers(uri):
    return [r[0] for r in tc.execute(
        "SELECT artist_name FROM track_credits WHERE spotify_track_uri = ? "
        "AND credit_type = 'remixer' ORDER BY 1", [uri]).fetchall()]

wrong = []
for i, (title, artist, want) in enumerate(CASES):
    if not title:
        continue
    expect = [] if want is None or want == artist else [want]
    if remixers(f"uri:{i}") != expect:
        wrong.append((title, remixers(f"uri:{i}"), expect))
check("track_credits carries exactly the remixer each title names", wrong, [])
check("the remixer is typed 'remixer', from the export",
      tc.execute("SELECT DISTINCT credit_source FROM track_credits "
                 "WHERE credit_type = 'remixer'").fetchall(), [("export",)])
check("the original artist keeps the album-artist credit",
      tc.execute("SELECT artist_name FROM track_credits WHERE spotify_track_uri = "
                 "'uri:0' AND credit_type = 'album_artist'").fetchall(),
      [("Trevor Daniel",)])
check("a feature and a bracket remixer coexist on one track",
      sorted(r[0] for r in tc.execute(
          "SELECT artist_name FROM track_credits WHERE spotify_track_uri = 'uri:12'"
      ).fetchall()), ["Central Cee", "Ed Sheeran", "Fumez The Engineer", "Tion Wayne"])
check("a polled track keeps the poller's credits and no export remixer",
      tc.execute("SELECT artist_name, credit_type, credit_source FROM track_credits "
                 "WHERE spotify_track_uri = 'uri:polled' ORDER BY 1").fetchall(),
      [("Flume", "album_artist", "poller"), ("Petit Biscuit", "featured", "poller")])
dupes = tc.execute("""SELECT count(*) FROM (SELECT spotify_track_uri, artist_name
                      FROM track_credits GROUP BY 1, 2 HAVING count(*) > 1)""").fetchone()[0]
check("no performer is credited twice on one track", dupes, 0)
mismatch = tc.execute("""SELECT count(*) FROM (
    SELECT spotify_track_uri, any_value(n_performers) AS claimed, count(*) AS actual
    FROM track_credits GROUP BY 1 HAVING claimed <> actual)""").fetchone()[0]
check("n_performers matches the rows written", mismatch, 0)
lost = tc.execute("""SELECT count(*) FROM (
    SELECT DISTINCT spotify_track_uri FROM plays
    EXCEPT SELECT DISTINCT spotify_track_uri FROM track_credits)""").fetchone()[0]
check("no track lost by recrediting", lost, 0)

# --- no listening time created or destroyed --------------------------------
# A bracket remixer is credit_type 'remixer', which analyze.py weights through
# its existing feature_weight branch. Every play's time must still sum to the
# play's time, under both variants, and a remixer must get the feature share.
tc.execute("""CREATE TABLE plays_t AS
    SELECT spotify_track_uri, played_seconds,
           TIMESTAMP '2026-03-01 12:00:00' + INTERVAL (row_number() OVER ()) MINUTE AS ts,
           CAST(played_seconds * 1000 AS BIGINT) AS ms_played,
           DATE '2026-03-01' AS month
    FROM plays""")
tc.execute("ALTER TABLE plays RENAME TO plays_src")
tc.execute("ALTER TABLE plays_t RENAME TO plays")
tc.execute("""CREATE TABLE artist_tags AS
    SELECT DISTINCT artist_name, 'house' AS tag, 2 AS tag_count, TRUE AS is_genre
    FROM track_credits""")
analyze.build_tag_weights(tc)
analyze.build_credit_weights(tc)
for variant, secs_in, secs_out in analyze.time_balance(tc):
    check(f"{variant}: attributed equals credited",
          abs(secs_in - secs_out) <= 1e-6 * secs_in, True)
try:
    analyze.assert_time_conserved(tc)
    conserved = True
except SystemExit:
    conserved = False
check("assert_time_conserved passes", conserved, True)
fw = config.CREDIT_VARIANTS["with_features"]
w = dict(tc.execute("""SELECT artist_name, credit_w FROM credit_weights c
    JOIN (SELECT DISTINCT play_id FROM credit_weights WHERE artist_name = 'blackbear') p
    USING (play_id) WHERE variant = 'with_features'""").fetchall())
check("with_features: blackbear takes the feature share of 'Falling'",
      abs(w.get("blackbear", -1) - fw / (1 + fw)) < 1e-12, True)
check("...and Trevor Daniel the rest",
      abs(w.get("Trevor Daniel", -1) - 1 / (1 + fw)) < 1e-12, True)

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
