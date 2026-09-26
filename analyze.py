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
     toggles between them without recomputing. It is normalised per play ROW,
     and assert_time_conserved refuses to write if the split did not keep
     every credited second.
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


# Stage 3 reads EXPORT rows only. Polled plays past the export are provisional:
# ms_played is the track's length rather than the time played, skipped and
# reason_end are NULL, and a 50-item page can drop plays without saying so. The
# 2026-09-25 re-run showed what one day of them does — 50 polled plays made a
# one-day September that became the highest-leverage point in every 12-month
# slope and flipped 10 of 11 trend classes. This is ingest.merge_polled's
# coverage cut applied to time: the export decides what the trends cover.
# Polled rows still repair credits (Stage 1b) and still count as Stage 10 hours;
# they never enter a trend, a secondary metric or a window anchor. The skip rate
# needs it as much as the trends do: a NULL `skipped` makes is_skip NULL, which
# counts in weighted_plays and never in weighted_skips, so every polled row was
# being scored as a play that was not skipped.
EXPORT_ONLY = "WHERE NOT ms_played_estimated"


def register_sources(con: duckdb.DuckDBPyConnection) -> None:
    for name, path, where in (
        ("plays", config.PLAYS_PARQUET, EXPORT_ONLY),
        ("plays_raw", config.PLAYS_RAW_PARQUET, EXPORT_ONLY),
        ("track_credits", config.DATA_DIR / "track_credits.parquet", ""),
        ("artist_tags", config.ARTIST_TAGS_PARQUET, ""),
    ):
        if not path.exists():
            raise SystemExit(f"{path} not found — run the earlier stages first.")
        con.execute(
            f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM '{path}' {where}")


def build_tag_weights(con: duckdb.DuckDBPyConnection) -> None:
    """Per-artist genre vector, vote-weighted and capped."""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE artist_tag_weights AS
        WITH ranked AS (
            SELECT artist_name, tag,
                   tag_count AS raw_count,
                   -- MusicBrainz tag counts go NEGATIVE when the community
                   -- downvotes a tag (as low as -6 here). Left unclamped they
                   -- poison the weighting: an artist tagged [-1, -1] sums to
                   -- exactly zero, 0/0 yields NaN, and the NaN then spreads
                   -- across three months of every affected genre through the
                   -- rolling mean. Clamping keeps disputed tags at minimum
                   -- weight instead of deleting the artist outright.
                   greatest(tag_count, 0) AS tag_count,
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
            -- +1 so artists whose tags all have zero votes still split evenly.
            -- With the clamp above the denominator is at least the tag count,
            -- so it can never reach zero.
            (tag_count + 1.0)
                / nullif(sum(tag_count + 1.0) OVER (PARTITION BY artist_name), 0)
                AS tag_weight
        FROM kept
        """
    )


def build_credit_weights(con: duckdb.DuckDBPyConnection) -> None:
    """A play's time split across its performers, per variant.

    Materialised, rather than a CTE inside build_tag_trends, so that
    assert_time_conserved can check it before anything is written.
    """
    variant_sql = " UNION ALL ".join(
        f"SELECT '{name}' AS variant, {w} AS feature_weight"
        for name, w in config.CREDIT_VARIANTS.items()
    )

    con.execute(
        f"""
        CREATE OR REPLACE TABLE credit_weights AS

        WITH variants AS ({variant_sql}),

        -- The play's identity is its ROW, not (ts, track). 97 plays here
        -- share (ts, spotify_track_uri) with a different play — ingest
        -- dedupes on every content column and these differ in ms_played —
        -- and partitioning on (ts, track) gave each of a pair half weight,
        -- losing half the pair's time (4.26 h). The id only has to be unique
        -- per row; its value never matters, since every output aggregates
        -- over it.
        numbered AS (
            SELECT row_number() OVER () AS play_id, * FROM plays
        ),
        play_credits AS (
            SELECT
                v.variant,
                p.play_id,
                p.month,
                c.artist_name,
                p.ms_played / 1000.0 AS played_seconds,
                CASE c.credit_type WHEN 'album_artist' THEN 1.0
                                   ELSE v.feature_weight END AS raw_w
            FROM numbered p
            JOIN track_credits c USING (spotify_track_uri)
            CROSS JOIN variants v
        )
        SELECT
            variant, play_id, month, artist_name, played_seconds,
            -- Normalise across the performers of ONE play; partitioning by
            -- artist instead would hand every performer a full 1.0 and
            -- multiply the listening time by the size of the credit list.
            -- The fallback is for a play with no album-artist credit under
            -- album_artist_only, where every weight is 0.0 and 0/0 is NULL:
            -- "CARNIVAL - HOOLIGANS VERSION" lost its album artist `¥$` in
            -- Stage 1b and vanished from that variant outright. An even split
            -- keeps its time; restoring the missing credit is Stage 1b's job.
            coalesce(raw_w / nullif(sum(raw_w) OVER w, 0),
                     1.0 / count(*) OVER w) AS credit_w
        FROM play_credits
        WINDOW w AS (PARTITION BY variant, play_id)
        """
    )


def build_tag_trends(con: duckdb.DuckDBPyConnection) -> None:
    """Monthly tag share, smoothed, with a trailing-window slope per tag."""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE tag_trends AS

        -- Time attributed to each tag. Two normalised weights multiplied, so
        -- the total across tags equals the total tagged listening time.
        WITH tag_month AS (
            SELECT
                n.variant,
                n.month,
                w.tag,
                sum(n.played_seconds * n.credit_w * w.tag_weight) AS tag_seconds,
                count(DISTINCT n.artist_name)                     AS n_artists
            FROM credit_weights n
            JOIN artist_tag_weights w USING (artist_name)
            GROUP BY 1, 2, 3
        ),
        -- A dense month x tag grid. Without it the rolling mean would average
        -- over whatever rows happen to exist and silently skip gap months.
        -- The months come from the CALENDAR, not from DISTINCT month: a month
        -- nobody listened in must still be a row, or the months either side
        -- of it become neighbours. That is how a one-day September after an
        -- empty August was averaged with June and July and treated as the
        -- month after July. Such a month gets a NULL share (0/0 through the
        -- nullif below), which avg and regr_slope skip rather than read as a
        -- month of zero listening in every genre. The export has no gap
        -- months today, so this is a guard, not a change.
        grid AS (
            SELECT v.variant, m.month, t.tag
            FROM (SELECT DISTINCT variant FROM tag_month) v
            CROSS JOIN (
                SELECT unnest(generate_series(
                           min(month), max(month), INTERVAL 1 MONTH))::DATE
                       AS month
                FROM plays
            ) m
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
                -- Calendar months since the first, so the slope's x-axis is
                -- time. dense_rank counted distinct months present and would
                -- put a two-month gap one step apart if the grid ever lost it.
                date_diff('month', (SELECT min(month) FROM dense), month)
                    AS month_idx
            FROM dense
        ),
        -- An empty month's smoothed share is NULL too. avg() over the window
        -- would otherwise carry the previous months' shares into it: plays in
        -- Jan–Apr then Jul–Aug (tests/test_trend_horizon.py) gave house a
        -- smoothed 0.625 in May and 0.5 in June, months with no listening at
        -- all. Both points entered the slope and the mean_recent_share gate
        -- below, and the dashboard drew a smoothed line through the hole. The
        -- window still reads the raw shares, so the month after a gap averages
        -- only the months that had plays.
        smoothed AS (
            SELECT
                *,
                CASE WHEN share IS NULL THEN NULL ELSE avg(share) OVER (
                    PARTITION BY variant, tag ORDER BY month
                    ROWS BETWEEN {config.ROLLING_WINDOW_MONTHS - 1} PRECEDING
                             AND CURRENT ROW
                ) END AS smoothed_share
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


def assert_no_nan(con: duckdb.DuckDBPyConnection) -> None:
    """Fail loudly on non-finite values rather than writing them to Parquet.

    A NaN here does not announce itself — it silently blanks a genre for three
    months via the rolling mean and shows up much later as a puzzling gap.
    """
    checks = [
        ("artist_tag_weights", "tag_weight"),
        ("tag_trends", "share"),
        ("tag_trends", "smoothed_share"),
        ("tag_trends", "tag_seconds"),
        ("skip_rate_by_tag", "skip_rate"),
    ]
    problems = []
    for table, col in checks:
        n = con.execute(
            f"SELECT count(*) FROM {table} "
            f"WHERE isnan({col}) OR isinf({col})"
        ).fetchone()[0]
        if n:
            problems.append(f"{table}.{col}: {n:,} non-finite values")
    if problems:
        raise SystemExit("Refusing to write non-finite values:\n  "
                         + "\n  ".join(problems))


def time_balance(con: duckdb.DuckDBPyConnection) -> list[tuple[str, float, float]]:
    """(variant, credited seconds in, seconds attributed out), per variant.

    "In" is measured from `plays` directly, never from credit_weights, so a
    fault in the weighting cannot also hide itself in the thing it is checked
    against. Every configured variant gets a row, even one absent from
    credit_weights, so a variant that lost everything still reports.
    """
    return con.execute(
        """
        WITH credited AS (
            SELECT coalesce(sum(ms_played / 1000.0), 0) AS secs
            FROM plays
            WHERE spotify_track_uri IN (SELECT spotify_track_uri FROM track_credits)
        ),
        attributed AS (
            SELECT variant, sum(played_seconds * credit_w) AS secs
            FROM credit_weights GROUP BY 1
        )
        SELECT v.variant, c.secs, coalesce(a.secs, 0)
        FROM (SELECT unnest(?::VARCHAR[]) AS variant) v
        CROSS JOIN credited c
        LEFT JOIN attributed a USING (variant)
        ORDER BY 1
        """,
        [list(config.CREDIT_VARIANTS)],
    ).fetchall()


def assert_time_conserved(con: duckdb.DuckDBPyConnection) -> None:
    """Refuse to write trends whose credit weighting created or lost time.

    Shares are ratios, so time lost evenly moves no share at all and nothing
    on the report surface shows it. The 2026-09-25 re-run attributed 3,938.7
    of 3,941.0 credited hours — a (ts, track) partition halving 97 paired
    plays, and one track with no album artist vanishing under
    album_artist_only — and every chart looked fine.
    """
    problems = []
    for variant, secs_in, secs_out in time_balance(con):
        if abs(secs_out - secs_in) > 1e-6 * secs_in:
            problems.append(f"{variant}: {secs_in / 3600:,.4f} h credited, "
                            f"{secs_out / 3600:,.4f} h attributed")
    if problems:
        raise SystemExit("Refusing to write: credit weighting did not conserve "
                         "listening time:\n  " + "\n  ".join(problems))


def keep_previous_trends(con: duckdb.DuckDBPyConnection) -> None:
    """Hold the last run's shares in memory before write_outputs replaces them.

    Only so the report can say how far this run moved them: a fix meant to
    change nothing but rounding should show a rounding-sized number, and a
    byte-identical re-run should show zero.
    """
    if config.TAG_TRENDS_PARQUET.exists():
        con.execute(
            f"CREATE OR REPLACE TABLE previous_trends AS "
            f"SELECT variant, tag, month, share FROM '{config.TAG_TRENDS_PARQUET}'")


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
    # The views are export-only, so the excluded rows are counted at source.
    horizon = q("SELECT max(month) FROM plays")[0]
    n_polled = q(f"SELECT count(*) FROM '{config.PLAYS_PARQUET}' "
                 f"WHERE ms_played_estimated")[0]
    print(f"horizon          : {horizon}   (export's last month; "
          f"{n_polled:,} polled plays after it excluded)")

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

    # Checked before the write by assert_time_conserved; printed so the
    # balance is on the verification surface and not only implied by silence.
    for variant, secs_in, secs_out in time_balance(con):
        print(f"credited listening conserved ({variant}): "
              f"{secs_in / 3600:,.1f} h in, {secs_out / 3600:,.1f} h attributed")

    # How far this run moved the shares the last run wrote, on the rows both
    # carry. Months or tags present on only one side are counted, not diffed.
    if q("SELECT count(*) FROM duckdb_tables() "
         "WHERE table_name = 'previous_trends'")[0]:
        d_max, d_var, d_tag, d_month, n_shared = q(
            """
            SELECT max(d), arg_max(variant, d), arg_max(tag, d),
                   arg_max(month, d), count(*)
            FROM (SELECT n.variant, n.tag, n.month, abs(n.share - o.share) AS d
                  FROM tag_trends n
                  JOIN previous_trends o USING (variant, tag, month)
                  WHERE n.share IS NOT NULL AND o.share IS NOT NULL)
            """)
        n_added, n_removed = q(
            """
            SELECT count(*) FILTER (WHERE o.variant IS NULL),
                   count(*) FILTER (WHERE n.variant IS NULL)
            FROM tag_trends n
            FULL JOIN previous_trends o
              ON n.variant = o.variant AND n.tag = o.tag AND n.month = o.month
            """)
        where = f"  ({d_var}, {d_tag}, {d_month})" if d_max else ""
        print(f"max |Δshare| vs previous run: {100 * (d_max or 0):.6f} pp over "
              f"{n_shared:,} shared rows{where}; "
              f"{n_added:,} rows new, {n_removed:,} gone")
    else:
        print("max |Δshare| vs previous run: (no previous tag_trends.parquet)")

    print(f"\n--- Top 12 genres, latest month "
          f"({config.ROLLING_WINDOW_MONTHS}-month smoothed, {v}) ---")
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
    # One thread, so every float sum adds in the same order. ORDER BY ALL fixes
    # which row lands where, not what is in it: parallel aggregates combine
    # partial sums in whatever order the threads finish, and two identical runs
    # differed in 70,638 of 89,080 rows by up to 4.4e-16. Byte-identical
    # re-runs need both a total order and a deterministic summation order. The
    # data is tens of thousands of rows per variant, so this costs seconds.
    con.execute("SET threads = 1")
    register_sources(con)
    print("Building artist tag weights ...")
    build_tag_weights(con)
    print("Building credit weights ...")
    build_credit_weights(con)
    print("Building tag trends ...")
    build_tag_trends(con)
    print("Building secondary metrics ...")
    build_secondary_metrics(con)
    assert_no_nan(con)
    assert_time_conserved(con)
    keep_previous_trends(con)
    write_outputs(con)
    report(con)


if __name__ == "__main__":
    main()
