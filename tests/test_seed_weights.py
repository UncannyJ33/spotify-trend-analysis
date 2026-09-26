"""tests/test_seed_weights.py — Stage 5 seeds on RECENT listening time.

No network. Synthetic plays, track_credits and artist_resolution tables only.

build_seeds used to sum track_credits.played_seconds — each track's ALL-TIME
total — once per windowed play, so a seed weighed all-time seconds x recent
plays. One recent listen to a 2019 favourite outranked a month of what is on
now, which is the opposite of what the module doc, the docstring and
config.SEED_WINDOW_MONTHS all say seeding is for. Pinned here:

  * windowed seconds, not all-time seconds, decide the ranking;
  * a play's time is split across its performers the way Stage 3 splits it
    (album artist 1.0, anyone else CREDIT_VARIANTS[DEFAULT_VARIANT]), so a
    featured guest gets 0.5 / 1.5 of the play and no time is created;
  * the window anchors on the export horizon, so a polled play in a later
    month does not drag the window forward past older export listening.
"""
import sys
sys.path.insert(0, ".")
import duckdb

import config
import recommend

failures = []
def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")

def close(a, b, tol=1e-9):
    return a is not None and b is not None and abs(a - b) <= tol

assert config.DEFAULT_VARIANT == "with_features"
assert config.CREDIT_VARIANTS[config.DEFAULT_VARIANT] == 0.5
assert config.SEED_WINDOW_MONTHS == 18   # the window edge below is 2025-01

con = duckdb.connect()
# Export plays run through 2026-07, so the horizon is 2026-07 and the window's
# lower edge is 2025-01. One polled play sits in 2026-09.
rows = [
    # Old A: a 2019 favourite, 100 h all-time (see track_credits), played once
    # recently. Its old plays fall outside the window.
    ("2020-03-01 12:00:00", "uri:a", 180.0, "2020-03-01", False),
    ("2020-04-01 12:00:00", "uri:a", 180.0, "2020-04-01", False),
    ("2026-07-10 12:00:00", "uri:a", 180.0, "2026-07-01", False),
    # A lead with a featured guest, one recent 300 s play.
    ("2026-06-01 12:00:00", "uri:f", 300.0, "2026-06-01", False),
    # Edge: exactly on the window's lower bound, horizon-anchored.
    ("2025-01-15 12:00:00", "uri:e", 60.0, "2025-01-01", False),
    # Outside: one month before the lower bound.
    ("2024-12-15 12:00:00", "uri:o", 999.0, "2024-12-01", False),
    # The polled play. Estimated ms_played, past the horizon.
    ("2026-09-25 12:00:00", "uri:p", 200.0, "2026-09-01", True),
]
# New B: ten 3-minute plays this summer.
rows += [(f"2026-07-{d:02d} 09:00:00", "uri:b", 180.0, "2026-07-01", False)
         for d in range(1, 11)]
con.execute("CREATE TABLE plays (ts TIMESTAMP, spotify_track_uri VARCHAR, "
            "played_seconds DOUBLE, month DATE, ms_played_estimated BOOLEAN)")
con.executemany("INSERT INTO plays VALUES (?, ?, ?, ?, ?)", rows)

# played_seconds here is the track's ALL-TIME total, as credits.py writes it.
# The old query multiplied this by the number of windowed plays.
con.execute("""
CREATE TABLE track_credits AS
SELECT spotify_track_uri, artist_name, credit_type, played_seconds::DOUBLE AS played_seconds
FROM (VALUES
  ('uri:a', 'Old A',   'album_artist', 360000.0),
  ('uri:b', 'New B',   'album_artist',   1800.0),
  ('uri:f', 'Lead',    'album_artist',    300.0),
  ('uri:f', 'Guest',   'featured',        300.0),
  ('uri:e', 'Edge',    'album_artist',     60.0),
  ('uri:o', 'Outside', 'album_artist',    999.0),
  ('uri:p', 'Polled',  'album_artist',    200.0)
) t(spotify_track_uri, artist_name, credit_type, played_seconds)""")
con.execute("""
CREATE TABLE artist_resolution AS SELECT * FROM (VALUES
  ('Old A',   'mbid-a', 'resolved'),
  ('New B',   'mbid-b', 'resolved'),
  ('Lead',    'mbid-l', 'resolved'),
  ('Guest',   'mbid-g', 'resolved'),
  ('Edge',    'mbid-e', 'resolved'),
  ('Outside', 'mbid-o', 'resolved'),
  ('Polled',  'mbid-p', 'resolved')
) t(artist_name, mbid, status)""")

seeds = recommend.build_seeds(con)
w = {name: weight for name, _, weight in seeds}
order = [name for name, _, _ in seeds]

check("ten recent plays outrank one recent play of a 100 h favourite",
      order.index("New B") < order.index("Old A"), True)
check("seed weight is windowed seconds: B / A = 1800 / 180",
      close(w["New B"] / w["Old A"], 10.0), True)
check("weights sum to 1", close(sum(w.values()), 1.0), True)
check("a featured credit gets 0.5 / 1.5 of the play",
      close(w["Guest"] / (w["Guest"] + w["Lead"]), 1 / 3), True)
check("a play's time is split, not duplicated: Lead + Guest = one 300 s play",
      close((w["Lead"] + w["Guest"]) / w["Old A"], 300 / 180), True)
check("a play on the horizon-anchored lower bound is still a seed",
      "Edge" in w, True)
check("a play a month before the lower bound is not", "Outside" in w, False)
# Same rule as Stage 10's known pool (Task 5): the window has a lower bound
# only, so a polled play past the horizon is recent listening and counts. What
# it must not do is anchor the window — that is the Edge check above.
check("a polled play past the horizon still counts as recent listening",
      "Polled" in w, True)
check("mbids carried through", dict((n, m) for n, m, _ in seeds)["New B"], "mbid-b")

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
