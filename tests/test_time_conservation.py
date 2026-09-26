"""tests/test_time_conservation.py — credit weighting neither creates nor loses time.

No network. Synthetic plays and track_credits tables only.

The 2026-09-25 re-run attributed 3,938.7 of 3,941.0 credited hours, and the
report could not show it: shares are ratios, so time lost evenly looks like no
loss at all. Two causes, both pinned here.

  * 97 plays share (ts, spotify_track_uri) with a DIFFERENT play — ingest
    dedupes on every content column, and these differ in ms_played. Credit
    weight was partitioned by (variant, ts, uri), so each play of a pair got
    half weight and half the pair's time vanished.
  * "CARNIVAL - HOOLIGANS VERSION" has no album-artist credit (its album
    artist is `¥$`, which Stage 1b dropped). Under album_artist_only every
    remaining credit weighs 0.0, 0/0 is NULL, and the play disappeared.
"""
import sys
sys.path.insert(0, ".")
import duckdb

import analyze
import config

failures = []
def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")

def close(a, b, tol=1e-9):
    return a is not None and b is not None and abs(a - b) <= tol

con = duckdb.connect()
con.execute("""
CREATE TABLE plays AS SELECT * FROM (VALUES
  -- The pair: same end time, same track, different ms_played. Two listens.
  (TIMESTAMP '2026-03-01 12:00:00', 'uri:pair',  180000, DATE '2026-03-01'),
  (TIMESTAMP '2026-03-01 12:00:00', 'uri:pair',   60000, DATE '2026-03-01'),
  -- A featured track: album artist 1.0, feature 0.5 under with_features.
  (TIMESTAMP '2026-03-02 12:00:00', 'uri:feat',  200000, DATE '2026-03-01'),
  -- No album-artist credit at all, only two featured performers.
  (TIMESTAMP '2026-03-03 12:00:00', 'uri:carn',  240000, DATE '2026-03-01'),
  (TIMESTAMP '2026-03-04 12:00:00', 'uri:solo',  120000, DATE '2026-03-01'),
  -- A track with no credits at all is not credited listening; it is outside
  -- the balance on both sides rather than counted in and lost.
  (TIMESTAMP '2026-03-05 12:00:00', 'uri:none',   90000, DATE '2026-03-01')
) t(ts, spotify_track_uri, ms_played, month)""")
con.execute("""
CREATE TABLE track_credits AS SELECT * FROM (VALUES
  ('uri:pair', 'Pairer',   'album_artist'),
  ('uri:feat', 'Lead',     'album_artist'),
  ('uri:feat', 'Guest',    'featured'),
  ('uri:carn', 'Kanye',    'featured'),
  ('uri:carn', 'Ty',       'featured'),
  ('uri:solo', 'Lead',     'album_artist')
) t(spotify_track_uri, artist_name, credit_type)""")
con.execute("""
CREATE TABLE artist_tags AS SELECT * FROM (VALUES
  ('Pairer', 'house', 2, TRUE), ('Lead', 'dubstep', 2, TRUE),
  ('Guest', 'hip hop', 2, TRUE), ('Kanye', 'hip hop', 2, TRUE),
  ('Ty', 'r&b', 2, TRUE)
) t(artist_name, tag, tag_count, is_genre)""")

analyze.build_tag_weights(con)
analyze.build_credit_weights(con)
analyze.build_tag_trends(con)

CREDITED = (180 + 60 + 200 + 240 + 120)   # seconds; uri:none is uncredited

for variant in config.CREDIT_VARIANTS:
    attributed = con.execute(
        "SELECT sum(played_seconds * credit_w) FROM credit_weights "
        "WHERE variant = ?", [variant]).fetchone()[0]
    check(f"{variant}: attributed equals credited",
          close(attributed, CREDITED), True)

    pair = con.execute(
        "SELECT sum(played_seconds * credit_w) FROM credit_weights c "
        "JOIN (SELECT DISTINCT play_id FROM credit_weights cw "
        "      WHERE cw.variant = ? AND cw.artist_name = 'Pairer') p USING (play_id) "
        "WHERE c.variant = ?", [variant, variant]).fetchone()[0]
    check(f"{variant}: the (ts, uri) pair contributes its full seconds",
          close(pair, 180 + 60), True)

    carn = con.execute(
        "SELECT artist_name, credit_w FROM credit_weights "
        "WHERE variant = ? AND artist_name IN ('Kanye', 'Ty') ORDER BY 1",
        [variant]).fetchall()
    check(f"{variant}: a play with no album artist splits evenly",
          [(a, round(w, 12)) for a, w in carn], [("Kanye", 0.5), ("Ty", 0.5)])

    # Every artist here is tagged, so tag time must equal credited time too:
    # build_tag_trends has to read the conserved weights, not re-derive them.
    tagged = con.execute("SELECT sum(tag_seconds) FROM tag_trends WHERE variant = ?",
                         [variant]).fetchone()[0]
    check(f"{variant}: tag time equals credited time", close(tagged, CREDITED), True)

guest = {v: con.execute(
    "SELECT credit_w FROM credit_weights WHERE variant = ? AND artist_name = 'Guest'",
    [v]).fetchone()[0] for v in config.CREDIT_VARIANTS}
check("a featured credit gets 0.5 / 1.5 of the play",
      close(guest["with_features"], 1 / 3), True)
check("a featured credit gets nothing under album_artist_only",
      close(guest["album_artist_only"], 0.0), True)

# --- the guard itself --------------------------------------------------------
try:
    analyze.assert_time_conserved(con)
    raised = False
except SystemExit:
    raised = True
check("assert_time_conserved passes on conserved weights", raised, False)

# Reproduce the old bug by hand: halve the pair, as the (ts, uri) partition did.
con.execute("UPDATE credit_weights SET credit_w = credit_w / 2 "
            "WHERE artist_name = 'Pairer' AND variant = 'with_features'")
try:
    analyze.assert_time_conserved(con)
    raised = False
except SystemExit as e:
    raised = True
    print(f"        ({str(e).splitlines()[0]})")
check("assert_time_conserved refuses a corrupted credit_weights", raised, True)

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
