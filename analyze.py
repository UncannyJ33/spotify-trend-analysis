"""Stage 3 — turn plays plus artist tags into monthly genre trends.

    .venv/bin/python analyze.py

Core metric: share of listening time per tag, bucketed by month, weighted by
`ms_played` rather than play count.

Two levels of weighting, both normalised so a play's time is neither created
nor destroyed:

  1. Credit weight — a play's time is split across its performers. The album
     artist is worth 1.0 and a featured performer `CREDIT_VARIANTS[variant]`.
     Both variants are computed and stored side by side, so `album_artist_only`
     (weight 0.0) reproduces the spec's original behaviour and the dashboard
     toggles between them without recomputing.
  2. Tag weight — an artist's share is split across their genres in proportion
     to MusicBrainz vote count, capped at TOP_N_TAGS_PER_ARTIST.

Outputs:
    data/tag_trends.parquet   (tag, month, share, smoothed_share, slope, ...)
    data/secondary_metrics/*  discovery, concentration, skip rate
"""

from __future__ import annotations

import duckdb

import config

SECONDARY_DIR = config.DATA_DIR / "secondary"


def register_sources(con: duckdb.DuckDBPyConnection) -> None:
    for name, path in (
        ("plays", config.PLAYS_PARQUET),
        ("plays_raw", config.PLAYS_RAW_PARQUET),
        ("track_credits", config.DATA_DIR / "track_credits.parquet"),
        ("artist_tags", config.ARTIST_TAGS_PARQUET),
    ):
        if not path.exists():
            raise SystemExit(f"{path} not found — run the earlier stages first.")
        con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM '{path}'")


def build_tag_weights(con: duckdb.DuckDBPyConnection) -> None:
    """Per-artist genre vector, vote-weighted and capped."""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE artist_tag_weights AS
        WITH ranked AS (
            SELECT artist_name, tag, tag_count,
                   row_number() OVER (
                       PARTITION BY artist_name
                       ORDER BY tag_count DESC, tag
                   ) AS rn
            FROM artist_tags
            WHERE is_genre                      -- canonical genres only
        ),
        kept AS (
            SELECT * FROM ranked WHERE rn <= {config.TOP_N_TAGS_PER_ARTIST}
        )
        SELECT
            artist_name,
            tag,
            -- +1 so artists whose tags all have zero votes still split evenly
            -- rather than dividing by zero.
            (tag_count + 1.0) / sum(tag_count + 1.0) OVER (PARTITION BY artist_name)
                AS tag_weight
        FROM kept
        """
    )


def build_tag_trends(con: duckdb.DuckDBPyConnection) -> None:
    """Monthly tag share, smoothed, with a trailing-window slope per tag."""
    variant_sql = " UNION ALL ".join(
        f"SELECT '{name}' AS variant, {w} AS feature_weight"
        for name, w in config.CREDIT_VARIANTS.items()
    )

    con.execute(
        f"""
        CREATE OR REPLACE TABLE tag_trends AS

        WITH variants AS ({variant_sql}),

        -- A play's time split across its performers, per variant.
        play_credits AS (
            SELECT
                v.variant,
                p.ts,                      -- carried solely to identify the play
                p.spotify_track_uri,
                p.month,
                c.artist_name,
                p.ms_played / 1000.0 AS played_seconds,
                CASE c.credit_type WHEN 'album_artist' THEN 1.0
                                   ELSE v.feature_weight END AS raw_w
            FROM plays p
            JOIN track_credits c USING (spotify_track_uri)
            CROSS JOIN variants v
        ),
        normalised AS (
            SELECT
                variant, month, artist_name, played_seconds,
                -- Normalise across the performers of ONE play. The partition
                -- must be the play's identity (ts + track); partitioning by
                -- artist instead would hand every performer a full 1.0 and
                -- multiply the listening time by the size of the credit list.
                raw_w / nullif(sum(raw_w) OVER (
                    PARTITION BY variant, ts, spotify_track_uri
                ), 0) AS credit_w
            FROM play_credits
        ),
        -- Time attributed to each tag. Two normalised weights multiplied, so
        -- the total across tags equals the total tagged listening time.
        tag_month AS (
            SELECT
                n.variant,
                n.month,
                w.tag,
                sum(n.played_seconds * n.credit_w * w.tag_weight) AS tag_seconds,
                count(DISTINCT n.artist_name)                     AS n_artists
            FROM normalised n
            JOIN artist_tag_weights w USING (artist_name)
            GROUP BY 1, 2, 3
        ),
        -- A dense month x tag grid. Without it the rolling mean would average
        -- over whatever rows happen to exist and silently skip gap months.
        grid AS (
            SELECT v.variant, m.month, t.tag
            FROM (SELECT DISTINCT variant FROM tag_month) v
            CROSS JOIN (SELECT DISTINCT month FROM plays) m
            CROSS JOIN (SELECT DISTINCT tag FROM tag_month) t
        ),
        dense AS (
            SELECT
                g.variant, g.month, g.tag,
                coalesce(tm.tag_seconds, 0) AS tag_seconds,
                coalesce(tm.n_artists, 0)   AS n_artists
            FROM grid g
            LEFT JOIN tag_month tm USING (variant, month, tag)
        ),
        shared AS (
            SELECT
                *,
                tag_seconds / nullif(
                    sum(tag_seconds) OVER (PARTITION BY variant, month), 0
                ) AS share,
                dense_rank() OVER (ORDER BY month) AS month_idx
            FROM dense
        ),
        smoothed AS (
            SELECT
                *,
                avg(share) OVER (
                    PARTITION BY variant, tag ORDER BY month
                    ROWS BETWEEN {config.ROLLING_WINDOW_MONTHS - 1} PRECEDING
                             AND CURRENT ROW
                ) AS smoothed_share
            FROM shared
        ),
        -- Trailing-window slope, one value per tag, measured on the smoothed
        -- series so month-to-month noise does not drive the classification.
        slopes AS (
            SELECT
                variant, tag,
                regr_slope(smoothed_share, month_idx) AS slope,
                avg(smoothed_share)                   AS mean_recent_share
            FROM smoothed
            WHERE month_idx > (SELECT max(month_idx) FROM smoothed)
                             - {config.SLOPE_WINDOW_MONTHS}
            GROUP BY 1, 2
        )

        SELECT
            s.variant,
            s.tag,
            s.month,
            s.share,
            s.smoothed_share,
            s.tag_seconds,
            s.n_artists,
            sl.slope,
            sl.slope * 12 * 100                       AS slope_pp_per_year,
            sl.slope * 12 / nullif(sl.mean_recent_share, 0)
                                                      AS rel_change_per_year,
            sl.mean_recent_share,
            CASE
                WHEN sl.slope IS NULL OR sl.mean_recent_share IS NULL THEN 'flat'
                -- Gate on absolute size first. Without this a genre sitting at
                -- a fraction of a percent posts a huge relative change off a
                -- rounding-error move and dominates the rising/falling lists.
                WHEN sl.mean_recent_share < {config.MIN_SHARE_FOR_TREND}
                     THEN 'negligible'
                WHEN sl.slope * 12 / nullif(sl.mean_recent_share, 0)
                     >  {config.TREND_REL_THRESHOLD} THEN 'rising'
                WHEN sl.slope * 12 / nullif(sl.mean_recent_share, 0)
                     < -{config.TREND_REL_THRESHOLD} THEN 'declining'
                ELSE 'flat'
            END                                        AS trend_class
        FROM smoothed s
        LEFT JOIN slopes sl USING (variant, tag)
        ORDER BY ALL
        """
    )


def build_secondary_metrics(con: duckdb.DuckDBPyConnection) -> None:
    """Discovery rate, repeat concentration, and skip rate by tag."""

    # Distinct artists heard for the first time in each month.
    con.execute(
        """
        CREATE OR REPLACE TABLE discovery_rate AS
        WITH firsts AS (
            SELECT artist_name, min(month) AS first_month
            FROM plays WHERE artist_name IS NOT NULL
            GROUP BY artist_name
        )
        SELECT first_month AS month, count(*) AS new_artists
        FROM firsts GROUP BY 1 ORDER BY 1
        """
    )

    # Share of a year's listening time held by its top 1% of artists.
    con.execute(
        """
        CREATE OR REPLACE TABLE repeat_concentration AS
        WITH per_year AS (
            SELECT year(ts) AS yr, artist_name, sum(played_seconds) AS secs
            FROM plays WHERE artist_name IS NOT NULL
            GROUP BY 1, 2
        ),
        ranked AS (
            SELECT *,
                   row_number() OVER (PARTITION BY yr ORDER BY secs DESC) AS rn,
                   count(*)     OVER (PARTITION BY yr)                    AS n_artists
            FROM per_year
        )
        SELECT
            yr AS year,
            max(n_artists)                                        AS artists,
            greatest(1, cast(ceil(max(n_artists) * 0.01) AS INT)) AS top_1pct_size,
            sum(secs) FILTER (
                WHERE rn <= greatest(1, ceil(n_artists * 0.01))
            ) / sum(secs)                                          AS top_1pct_share,
            sum(secs) / 3600.0                                     AS total_hours
        FROM ranked GROUP BY 1 ORDER BY 1
        """
    )

    # Skip rate per tag. Derived from plays_raw, since `plays` has already had
    # the sub-30s rows removed and those are precisely the skips.
    con.execute(
        f"""
        CREATE OR REPLACE TABLE skip_rate_by_tag AS
        WITH music AS (
            SELECT r.spotify_track_uri, r.ms_played,
                   (r.skipped OR r.reason_end IN ('fwdbtn', 'endplay')) AS is_skip
            FROM plays_raw r
            WHERE r.content_type = 'music'
        ),
        attributed AS (
            SELECT m.is_skip, m.ms_played, w.tag, w.tag_weight
            FROM music m
            JOIN track_credits c USING (spotify_track_uri)
            JOIN artist_tag_weights w ON w.artist_name = c.artist_name
            WHERE c.credit_type = 'album_artist'
        )
        SELECT
            tag,
            sum(tag_weight)                                AS weighted_plays,
            sum(tag_weight) FILTER (WHERE is_skip)         AS weighted_skips,
            sum(tag_weight) FILTER (WHERE is_skip) / nullif(sum(tag_weight), 0)
                                                           AS skip_rate,
            sum(ms_played / 1000.0 * tag_weight) / 3600.0  AS hours
        FROM attributed
        GROUP BY 1
        HAVING sum(tag_weight) >= 50
        ORDER BY skip_rate DESC
        """
    )


def write_outputs(con: duckdb.DuckDBPyConnection) -> None:
    SECONDARY_DIR.mkdir(parents=True, exist_ok=True)
    con.execute(
        f"COPY (SELECT * FROM tag_trends ORDER BY ALL) "
        f"TO '{config.TAG_TRENDS_PARQUET}' (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    for table in ("discovery_rate", "repeat_concentration", "skip_rate_by_tag"):
        con.execute(
            f"COPY (SELECT * FROM {table} ORDER BY ALL) "
            f"TO '{SECONDARY_DIR / (table + '.parquet')}' "
            f"(FORMAT PARQUET, COMPRESSION ZSTD)"
        )


def report(con: duckdb.DuckDBPyConnection) -> None:
    v = config.DEFAULT_VARIANT
    q = lambda s: con.execute(s).fetchone()  # noqa: E731

    print()
    print("=" * 74)
    print("STAGE 3 — TAG TRENDS")
    print("=" * 74)

    n_tags, n_months = q(
        f"SELECT count(DISTINCT tag), count(DISTINCT month) FROM tag_trends "
        f"WHERE variant = '{v}'"
    )
    print(f"\nvariant reported : {v}   (both variants stored)")
    print(f"tags             : {n_tags:,}")
    print(f"months           : {n_months:,}")

    # How much listening time never reaches a tag at all — the honest ceiling
    # on everything downstream.
    covered = q(
        """
        WITH tagged AS (
            SELECT DISTINCT c.spotify_track_uri
            FROM track_credits c
            JOIN artist_tag_weights w ON w.artist_name = c.artist_name
        )
        SELECT 100.0 * sum(p.played_seconds) FILTER (
                   WHERE p.spotify_track_uri IN (SELECT spotify_track_uri FROM tagged))
             / sum(p.played_seconds)
        FROM plays p
        """
    )[0]
    print(f"listening time reaching at least one tag: {covered:.1f}%")

    print(f"\n--- Top 12 genres, trailing year ({v}) ---")
    for tag, sh, cls, pp in con.execute(
        f"""
        SELECT tag, smoothed_share, trend_class, slope_pp_per_year
        FROM tag_trends
        WHERE variant = '{v}' AND month = (SELECT max(month) FROM tag_trends)
        ORDER BY smoothed_share DESC NULLS LAST LIMIT 12
        """
    ).fetchall():
        arrow = {"rising": "▲", "declining": "▼"}.get(cls, "─")
        print(f"   {tag:<26} {100*(sh or 0):>5.1f}%  {arrow} {cls:<10} "
              f"{pp:+.2f} pp/yr")

    n_negligible = q(
        f"SELECT count(DISTINCT tag) FROM tag_trends "
        f"WHERE variant = '{v}' AND trend_class = 'negligible'"
    )[0]
    print(f"\n   ({n_negligible:,} tags below the "
          f"{100*config.MIN_SHARE_FOR_TREND:.1f}% share floor are unclassified)")

    for direction, label in (("rising", "RISING"), ("declining", "DECLINING")):
        print(f"\n--- {label} (trailing 12 months, by magnitude) ---")
        rows = con.execute(
            f"""
            SELECT DISTINCT tag, rel_change_per_year, slope_pp_per_year,
                   mean_recent_share
            FROM tag_trends
            WHERE variant = '{v}' AND trend_class = '{direction}'
            ORDER BY abs(rel_change_per_year) DESC LIMIT 10
            """
        ).fetchall()
        for tag, rel, pp, base in rows or []:
            print(f"   {tag:<26} {100*rel:+6.1f}% / yr   ({pp:+.2f} pp/yr, "
                  f"now {100*base:.1f}%)")
        if not rows:
            print("   (none)")

    print("\n--- Repeat concentration: share held by the top 1% of artists ---")
    for yr, n, sz, sh, h in con.execute(
        "SELECT year, artists, top_1pct_size, top_1pct_share, total_hours "
        "FROM repeat_concentration ORDER BY year"
    ).fetchall():
        print(f"   {yr}  top {sz:>2} of {n:>4} artists = {100*sh:>5.1f}% "
              f"of {h:>7,.0f} h")

    print("\n--- Discovery rate (new artists/month, by year) ---")
    first_year = q("SELECT year(min(month)) FROM discovery_rate")[0]
    for yr, avg_new, tot in con.execute(
        "SELECT year(month) AS yr, avg(new_artists), sum(new_artists) "
        "FROM discovery_rate GROUP BY 1 ORDER BY 1"
    ).fetchall():
        # In the first year of the export every artist is "new" by definition,
        # so that row is an artifact of where the data starts, not a discovery
        # spike.
        note = "  <- inflated: first year, everything is new" if yr == first_year else ""
        print(f"   {yr}   {avg_new:>5.1f} new artists/month   ({tot:,} total){note}")

    print("\n--- Skip rate by genre (highest and lowest, >= 20 h only) ---")
    hi = con.execute(
        "SELECT tag, skip_rate, hours FROM skip_rate_by_tag WHERE hours >= 20 "
        "ORDER BY skip_rate DESC LIMIT 5"
    ).fetchall()
    lo = con.execute(
        "SELECT tag, skip_rate, hours FROM skip_rate_by_tag WHERE hours >= 20 "
        "ORDER BY skip_rate ASC LIMIT 5"
    ).fetchall()
    for label, rows in (("most skipped", hi), ("least skipped", lo)):
        print(f"   {label}:")
        for tag, rate, h in rows:
            print(f"      {tag:<24} {100*rate:>5.1f}%   ({h:,.0f} h)")

    print(f"\nWrote {config.TAG_TRENDS_PARQUET}")
    print(f"Wrote {SECONDARY_DIR}/")
    print("=" * 74)


def main() -> None:
    config.ensure_dirs()
    con = duckdb.connect()
    register_sources(con)
    print("Building artist tag weights ...")
    build_tag_weights(con)
    print("Building tag trends ...")
    build_tag_trends(con)
    print("Building secondary metrics ...")
    build_secondary_metrics(con)
    write_outputs(con)
    report(con)


if __name__ == "__main__":
    main()
