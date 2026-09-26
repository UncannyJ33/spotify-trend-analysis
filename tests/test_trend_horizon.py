"""tests/test_trend_horizon.py — Stage 3 stops at the export's last month.

No network. Synthetic Parquet in a temp dir, read through analyze's own
register_sources, so the export-only filter is exercised where it lives.

The 2026-09-25 re-run is the case: 50 polled plays made a one-day September
after an August with no plays at all. The grid came from DISTINCT month and the
index from dense_rank, so September sat one step after July, its 3-month mean
was avg(Jun, Jul, Sep), and it was the highest-leverage point in the 12-month
slope — 10 of 11 trend-class flips came from one day of provisional data. The
export itself has no gap months, so none of this showed on the report surface
except as a story that quietly changed.
"""
import sys
sys.path.insert(0, ".")
import pathlib
import tempfile

import duckdb

import analyze
import config

failures = []
def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")

def close(a, b, tol=1e-12):
    return a is not None and b is not None and abs(a - b) <= tol

TAGS = [("Alpha", "house", 3, True), ("Beta", "dubstep", 3, True),
        ("Gamma", "house", 3, True)]


def run(rows):
    """Write rows as Stage 1/1b/2 output, then run Stage 3 up to its writes.

    rows: (ts, uri, artist, ms_played, skipped, reason_end, estimated).
    Every row is music; `plays` is `plays_raw` above the 30 s floor, exactly
    as ingest.build_plays derives it.
    """
    tmp = pathlib.Path(tempfile.mkdtemp())
    config.DATA_DIR = tmp
    config.PLAYS_RAW_PARQUET = tmp / "plays_raw.parquet"
    config.PLAYS_PARQUET = tmp / "plays.parquet"
    config.ARTIST_TAGS_PARQUET = tmp / "artist_tags.parquet"

    src = duckdb.connect()
    src.execute("""
        CREATE TABLE raw (ts TIMESTAMP, spotify_track_uri VARCHAR,
                          artist_name VARCHAR, ms_played BIGINT,
                          skipped BOOLEAN, reason_end VARCHAR,
                          ms_played_estimated BOOLEAN)""")
    src.executemany("INSERT INTO raw VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    src.execute("""
        CREATE TABLE plays_raw AS
        SELECT *, 'music' AS content_type,
               ms_played / 1000.0 AS played_seconds,
               date_trunc('month', ts)::DATE AS month
        FROM raw""")
    src.execute(f"COPY plays_raw TO '{config.PLAYS_RAW_PARQUET}' (FORMAT PARQUET)")
    src.execute(f"COPY (SELECT * FROM plays_raw WHERE ms_played >= "
                f"{config.MIN_MS_PLAYED}) TO '{config.PLAYS_PARQUET}' (FORMAT PARQUET)")
    src.execute(f"""
        COPY (SELECT DISTINCT spotify_track_uri, artist_name,
                     'album_artist' AS credit_type
              FROM plays_raw)
        TO '{tmp / "track_credits.parquet"}' (FORMAT PARQUET)""")
    src.execute("CREATE TABLE artist_tags (artist_name VARCHAR, tag VARCHAR, "
                "tag_count INT, is_genre BOOLEAN)")
    src.executemany("INSERT INTO artist_tags VALUES (?, ?, ?, ?)", TAGS)
    src.execute(f"COPY artist_tags TO '{config.ARTIST_TAGS_PARQUET}' (FORMAT PARQUET)")

    con = duckdb.connect()
    analyze.register_sources(con)
    analyze.build_tag_weights(con)
    analyze.build_credit_weights(con)
    analyze.build_tag_trends(con)
    analyze.build_secondary_metrics(con)
    analyze.assert_no_nan(con)
    analyze.assert_time_conserved(con)
    return con


def play(day, uri, artist, ms=200_000, skipped=False, reason="trackdone",
         estimated=False):
    return (day, uri, artist, ms, skipped, reason, estimated)


# --------------------------------------------------------------------------
# 1. Export Jan–Mar, then one POLLED play in September by an artist the export
#    never saw. The poller leaves skipped/reason_end NULL and estimates
#    ms_played, which is exactly why it must not reach a trend.
# --------------------------------------------------------------------------
rows = []
for month in ("2026-01", "2026-02", "2026-03"):
    rows += [play(f"{month}-{d:02d} 12:00", "uri:a", "Alpha") for d in range(1, 21)]
    rows += [play(f"{month}-{d:02d} 13:00", "uri:b", "Beta") for d in range(1, 11)]
# Five real skips, visible only in plays_raw (under the 30 s floor).
rows += [play(f"2026-01-{d:02d} 14:00", "uri:a", "Alpha", ms=10_000,
              skipped=True, reason="fwdbtn") for d in range(1, 6)]
rows.append(play("2026-09-24 21:57", "uri:g", "Gamma", ms=210_000,
                 skipped=None, reason=None, estimated=True))
con = run(rows)

months = [r[0].isoformat() for r in con.execute(
    "SELECT DISTINCT month FROM tag_trends ORDER BY 1").fetchall()]
check("trend months stop at the export's last month",
      months, ["2026-01-01", "2026-02-01", "2026-03-01"])
check("September is absent from tag_trends",
      con.execute("SELECT count(*) FROM tag_trends "
                  "WHERE month >= DATE '2026-04-01'").fetchone()[0], 0)
check("discovery_rate has no September row",
      con.execute("SELECT count(*) FROM discovery_rate "
                  "WHERE month >= DATE '2026-04-01'").fetchone()[0], 0)

# Gamma is tagged house. Admitted, its NULL skip flag would count in
# weighted_plays and never in weighted_skips: a polled row scored as a non-skip.
wp, ws = con.execute("SELECT weighted_plays, weighted_skips FROM skip_rate_by_tag "
                     "WHERE tag = 'house'").fetchone()
check("an estimated row does not enter skip_rate_by_tag (plays)", wp, 65.0)
check("an estimated row does not enter skip_rate_by_tag (skips)", ws, 5.0)

# --------------------------------------------------------------------------
# 2. Export plays in January and March only. February must exist as a row with
#    a NULL share (no listening that month, not zero listening in each genre),
#    March's 3-month mean must average January and March only, and the index
#    must count calendar months.
# --------------------------------------------------------------------------
rows = ([play(f"2026-01-{d:02d} 12:00", "uri:a", "Alpha") for d in (1, 2, 3)]
        + [play("2026-01-04 12:00", "uri:b", "Beta")]
        + [play("2026-03-01 12:00", "uri:a", "Alpha"),
           play("2026-03-02 12:00", "uri:b", "Beta")])
con = run(rows)
V = config.DEFAULT_VARIANT
house = {r[0].isoformat(): r[1:] for r in con.execute(
    "SELECT month, share, smoothed_share, slope FROM tag_trends "
    "WHERE variant = ? AND tag = 'house' ORDER BY month", [V]).fetchall()}

check("a February row exists", sorted(house), ["2026-01-01", "2026-02-01", "2026-03-01"])
check("February's share is NULL", house.get("2026-02-01", (0,))[0], None)
check("January's share", house["2026-01-01"][0], 0.75)
check("March's share", house["2026-03-01"][0], 0.5)
check("March's smoothed share averages January and March only",
      close(house["2026-03-01"][1], (0.75 + 0.5) / 2), True)
# Points (0, .75), (1, .75), (2, .625): slope = (sM - sJ) / 4. Treating March
# as the month after January — the dense_rank bug — gives (sM - sJ) / 2.
check("the slope measures a two-month gap, not one",
      close(house["2026-03-01"][2], (0.5 - 0.75) / 4), True)

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
