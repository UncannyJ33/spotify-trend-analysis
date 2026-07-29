"""Stage 7 — project the trend lines forward, and find the under-explored ground.

    .venv/bin/python forecast.py
    .venv/bin/python forecast.py --horizon 6

No network calls; everything comes from `tag_trends`.

Two questions:

  FORECAST — where does taste land in HORIZON months if the trailing slope
  holds? Straight-line extrapolation of a share is naive and would happily
  predict 140% of listening, so the projection is damped and renormalised.
  Read it as direction and rough magnitude, not as a prediction.

  GAPS — which genres are climbing while being served by only one or two
  artists? Those are the places the listening is already moving toward but has
  barely explored, which makes them the most useful thing to recommend into.
"""

from __future__ import annotations

import argparse

import duckdb

import config

FORECAST_PARQUET = config.DATA_DIR / "forecast.parquet"
GAPS_PARQUET = config.DATA_DIR / "genre_gaps.parquet"

# A trend that has run for a year rarely continues at full strength for another.
# Each projected month keeps this fraction of the previous month's momentum, so
# the extrapolation decays instead of running away.
MOMENTUM_DECAY = 0.92


def build_forecast(con: duckdb.DuckDBPyConnection, horizon: int) -> None:
    """Damped projection of each genre's share, renormalised to sum to 1."""
    # Geometric sum of the decaying monthly momentum: 1 + d + d^2 + ...
    damped_months = sum(MOMENTUM_DECAY ** i for i in range(horizon))

    con.execute(
        f"""
        CREATE OR REPLACE TABLE forecast AS
        WITH latest AS (
            SELECT tag, smoothed_share AS share_now,
                   coalesce(slope, 0) AS slope,
                   trend_class
            FROM tag_trends
            WHERE variant = '{config.DEFAULT_VARIANT}'
              AND month = (SELECT max(month) FROM tag_trends)
        ),
        projected AS (
            SELECT *,
                   -- Never let a projection go negative; a genre can fade to
                   -- zero but not below it.
                   greatest(share_now + slope * {damped_months}, 0) AS raw_future
            FROM latest
        )
        SELECT
            tag,
            share_now,
            raw_future / nullif(sum(raw_future) OVER (), 0) AS share_future,
            trend_class,
            slope,
            {horizon} AS horizon_months
        FROM projected
        WHERE share_now > 0 OR raw_future > 0
        ORDER BY share_future DESC
        """
    )
    con.execute(
        "ALTER TABLE forecast ADD COLUMN change DOUBLE"
    )
    con.execute("UPDATE forecast SET change = share_future - share_now")


def build_gaps(con: duckdb.DuckDBPyConnection) -> None:
    """Rising genres served by very few artists — where exploration pays."""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE genre_gaps AS
        WITH per_tag AS (
            SELECT
                w.tag,
                count(DISTINCT c.artist_name)                AS n_artists,
                sum(c.played_seconds * w.tag_weight) / 3600.0 AS hours
            FROM track_credits c
            JOIN artist_tag_weights w ON w.artist_name = c.artist_name
            GROUP BY 1
        ),
        latest AS (
            SELECT tag, smoothed_share, trend_class,
                   coalesce(rel_change_per_year, 0) AS rel
            FROM tag_trends
            WHERE variant = '{config.DEFAULT_VARIANT}'
              AND month = (SELECT max(month) FROM tag_trends)
        )
        SELECT
            l.tag,
            l.smoothed_share AS share_now,
            l.rel            AS rel_change_per_year,
            l.trend_class,
            p.n_artists,
            p.hours,
            -- Rising hard while thinly served scores highest. Dividing by the
            -- artist count is what surfaces "you like this and know almost
            -- nobody in it" over "you like this and already know everyone".
            l.rel * l.smoothed_share / p.n_artists AS gap_score,
            p.hours / p.n_artists                  AS hours_per_artist
        FROM latest l
        JOIN per_tag p USING (tag)
        WHERE l.trend_class = 'rising'
        ORDER BY gap_score DESC
        """
    )


def report(con: duckdb.DuckDBPyConnection, horizon: int) -> None:
    print()
    print("=" * 78)
    print(f"STAGE 7 — FORECAST ({horizon} months) AND GAPS")
    print("=" * 78)

    print(f"\n--- Projected genre mix in {horizon} months ---")
    print(f"{'genre':<26} {'now':>7} {'then':>7} {'change':>9}")
    print("-" * 78)
    for tag, now, then, chg in con.execute(
        "SELECT tag, share_now, share_future, change FROM forecast "
        "ORDER BY share_future DESC LIMIT 12"
    ).fetchall():
        arrow = "▲" if chg > 0.002 else ("▼" if chg < -0.002 else "─")
        print(f"{tag:<26} {100*(now or 0):>6.1f}% {100*(then or 0):>6.1f}% "
              f"{arrow} {100*chg:>+6.1f} pp")

    print("\n--- Biggest projected movers ---")
    for tag, now, then, chg in con.execute(
        "SELECT tag, share_now, share_future, change FROM forecast "
        "WHERE share_now > 0.005 OR share_future > 0.005 "
        "ORDER BY abs(change) DESC LIMIT 8"
    ).fetchall():
        print(f"   {tag:<26} {100*(now or 0):>5.1f}% -> {100*(then or 0):>5.1f}%  "
              f"({100*chg:+.1f} pp)")

    n_rising = con.execute("SELECT count(*) FROM genre_gaps").fetchone()[0]
    print(f"\n--- UNDER-EXPLORED: rising genres you barely know "
          f"({n_rising} rising in total) ---")
    print("    (climbing fast, but served by few artists relative to the pull)")
    print(f"{'genre':<26} {'share':>7} {'growth':>9} {'artists':>8} {'hours':>8}")
    print("-" * 78)
    # Ranked rather than cut at a fixed artist count: the thinnest-served rising
    # genre here still has 13 artists, so any hard threshold either excludes
    # everything or admits everything.
    rows = con.execute(
        "SELECT tag, share_now, rel_change_per_year, n_artists, hours "
        "FROM genre_gaps ORDER BY gap_score DESC LIMIT 10"
    ).fetchall()
    for tag, share, rel, n, h in rows:
        print(f"{tag:<26} {100*share:>6.1f}% {100*rel:>+8.0f}% {n:>8} {h:>7.0f}h")
    if not rows:
        print("   (no rising genres classified)")

    # The payoff: connect the gaps to Stage 5's candidates.
    if config.RECOMMENDATIONS_PARQUET.exists():
        print("\n--- Recommended artists that fill those gaps ---")
        for tag, artist, score in con.execute(
            f"""
            WITH gaps AS (
                SELECT tag FROM genre_gaps ORDER BY gap_score DESC LIMIT 8
            )
            SELECT g.tag, r.artist_name, r.score
            FROM gaps g
            JOIN '{config.RECOMMENDATIONS_PARQUET}' r
              ON list_contains(str_split(r.matched_genres, ', '), g.tag)
            QUALIFY row_number() OVER (PARTITION BY g.tag ORDER BY r.score DESC) <= 2
            ORDER BY r.score DESC
            LIMIT 14
            """
        ).fetchall():
            print(f"   {tag:<24} -> {artist[:34]:<34} (score {score:.3f})")

    print(f"\nWrote {FORECAST_PARQUET}")
    print(f"Wrote {GAPS_PARQUET}")
    print("=" * 78)
    print("\nThe forecast is a damped straight line, not a model. Read it as "
          "direction\nand rough magnitude — a trend that ran for a year rarely "
          "runs another at\nfull strength, which is what the decay accounts for.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Stage 7 forecast and gap analysis")
    ap.add_argument("--horizon", type=int, default=12,
                    help="months to project forward (default 12)")
    args = ap.parse_args()

    config.ensure_dirs()
    con = duckdb.connect()
    for name, path in (
        ("tag_trends", config.TAG_TRENDS_PARQUET),
        ("track_credits", config.DATA_DIR / "track_credits.parquet"),
        ("artist_tags", config.ARTIST_TAGS_PARQUET),
    ):
        if not path.exists():
            raise SystemExit(f"{path} not found — run the earlier stages first.")
        con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM '{path}'")

    # Reuse Stage 3's exact weighting so the two stages cannot disagree.
    import analyze
    analyze.build_tag_weights(con)

    build_forecast(con, args.horizon)
    build_gaps(con)

    for table, path in (("forecast", FORECAST_PARQUET), ("genre_gaps", GAPS_PARQUET)):
        con.execute(f"COPY (SELECT * FROM {table} ORDER BY ALL) TO '{path}' "
                    f"(FORMAT PARQUET, COMPRESSION ZSTD)")
    report(con, args.horizon)


if __name__ == "__main__":
    main()
