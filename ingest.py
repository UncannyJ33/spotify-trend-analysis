"""Stage 1 — ingest the Spotify Extended Streaming History export into Parquet.

Re-runnable and idempotent: the export is read in full on every run and the
Parquet files are rewritten, so running twice against the same export produces
byte-identical output, and running against a newer export simply picks up the
new plays. There is no incremental state to get out of sync.

DuckDB reads the JSON directly and writes the Parquet. Nothing is staged
through pandas.

    .venv/bin/python ingest.py

Outputs:
    data/plays_raw.parquet  — every row, unfiltered (skip behaviour included)
    data/plays.parquet      — music only, real listens only (>= 30s)
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

import config

# Columns that define a listening event. Used for de-duplication: `source_file`
# and `source_kind` are deliberately excluded so the same event appearing in two
# chunk files collapses to one row.
CONTENT_COLUMNS = [
    "ts",
    "platform",
    "ms_played",
    "conn_country",
    "track_name",
    "artist_name",
    "album_name",
    "spotify_track_uri",
    "episode_name",
    "episode_show_name",
    "spotify_episode_uri",
    "audiobook_title",
    "audiobook_uri",
    "audiobook_chapter_title",
    "audiobook_chapter_uri",
    "reason_start",
    "reason_end",
    "shuffle",
    "skipped",
    "offline",
    "offline_timestamp",
    "incognito_mode",
]

# Fields the export is known to provide. Anything outside this set in a future
# export is surfaced as schema drift rather than silently dropped.
KNOWN_EXPORT_FIELDS = {
    "ts",
    "platform",
    "ms_played",
    "conn_country",
    "ip_addr",
    "master_metadata_track_name",
    "master_metadata_album_artist_name",
    "master_metadata_album_album_name",
    "spotify_track_uri",
    "episode_name",
    "episode_show_name",
    "spotify_episode_uri",
    "audiobook_title",
    "audiobook_uri",
    "audiobook_chapter_uri",
    "audiobook_chapter_title",
    "reason_start",
    "reason_end",
    "shuffle",
    "skipped",
    "offline",
    "offline_timestamp",
    "incognito_mode",
}


def discover_files(export_dir: Path) -> list[Path]:
    """Every streaming-history JSON in the export, whatever its flavour.

    The directory is inspected rather than assumed: audio, video, and any
    future file kind are all picked up.
    """
    if not export_dir.is_dir():
        sys.exit(f"Export directory not found: {export_dir}")
    files = sorted(export_dir.glob("Streaming_History_*.json"))
    if not files:
        sys.exit(f"No Streaming_History_*.json files found in {export_dir}")
    return files


def check_parseable(con: duckdb.DuckDBPyConnection, files: list[Path]):
    """Read each file on its own so one bad file does not sink the whole run."""
    good: list[Path] = []
    failed: list[tuple[Path, str]] = []
    for f in files:
        try:
            con.execute(
                "SELECT count(*) FROM read_json(?, union_by_name = true)", [str(f)]
            ).fetchone()
            good.append(f)
        except Exception as exc:  # noqa: BLE001 - we want to report any failure
            failed.append((f, str(exc).strip().splitlines()[0]))
    return good, failed


def detect_schema_drift(con: duckdb.DuckDBPyConnection, files: list[Path]) -> set[str]:
    """Fields present in the export that this script does not know about."""
    cols = con.execute(
        "SELECT column_name FROM (DESCRIBE SELECT * FROM read_json(?, union_by_name = true))",
        [[str(f) for f in files]],
    ).fetchall()
    return {c[0] for c in cols} - KNOWN_EXPORT_FIELDS


def build_plays_raw(con: duckdb.DuckDBPyConnection, files: list[Path]) -> None:
    """Normalise every row of the export into a single de-duplicated table.

    `ip_addr` is dropped here and never reaches any derived artifact.
    """
    partition_by = ", ".join(CONTENT_COLUMNS)
    con.execute(
        f"""
        CREATE OR REPLACE TABLE plays_raw AS
        WITH source AS (
            SELECT
                ts,
                platform,
                ms_played,
                conn_country,
                -- NOTE: this is the *album* artist, not the track artist. On
                -- compilations, features and soundtracks it misattributes the
                -- play. Accepted at the artist level for now.
                master_metadata_album_artist_name  AS artist_name,
                master_metadata_track_name         AS track_name,
                master_metadata_album_album_name   AS album_name,
                spotify_track_uri,
                episode_name,
                episode_show_name,
                spotify_episode_uri,
                CAST(audiobook_title         AS VARCHAR) AS audiobook_title,
                CAST(audiobook_uri           AS VARCHAR) AS audiobook_uri,
                CAST(audiobook_chapter_title AS VARCHAR) AS audiobook_chapter_title,
                CAST(audiobook_chapter_uri   AS VARCHAR) AS audiobook_chapter_uri,
                reason_start,
                reason_end,
                shuffle,
                skipped,
                offline,
                offline_timestamp,
                incognito_mode,
                regexp_extract(filename, '[^/]+$')       AS source_file,
                CASE
                    WHEN filename ILIKE '%_Video_%' THEN 'video'
                    ELSE 'audio'
                END                                      AS source_kind
                -- ip_addr is intentionally absent.
            FROM read_json(?, union_by_name = true, filename = true)
        )
        SELECT
            *,
            CASE
                WHEN spotify_track_uri   IS NOT NULL THEN 'music'
                WHEN spotify_episode_uri IS NOT NULL THEN 'podcast'
                WHEN audiobook_uri       IS NOT NULL THEN 'audiobook'
                ELSE 'unknown'
            END                                   AS content_type,
            ms_played / 1000.0                    AS played_seconds,
            date_trunc('month', ts)::DATE         AS month
        FROM source
        -- Collapse events the export lists more than once. `ts` is the play
        -- *end* time, so an identical (ts, ms_played, track) triple cannot be
        -- two distinct listens. Ordering by source_file keeps the choice
        -- deterministic across runs.
        QUALIFY row_number() OVER (
            PARTITION BY {partition_by}
            ORDER BY source_file
        ) = 1
        """,
        [[str(f) for f in files]],
    )


def build_plays(con: duckdb.DuckDBPyConnection) -> None:
    """Music-only, real-listens-only view of the history.

    `spotify_track_uri IS NOT NULL` excludes podcasts and audiobooks; the
    30-second floor excludes skips and scrubbing.
    """
    con.execute(
        f"""
        CREATE OR REPLACE TABLE plays AS
        SELECT *
        FROM plays_raw
        WHERE spotify_track_uri IS NOT NULL
          AND ms_played >= {config.MIN_MS_PLAYED}
        """
    )


def write_parquet(con: duckdb.DuckDBPyConnection, table: str, path: Path) -> None:
    """Write a table to Parquet under a *total* row order.

    `ORDER BY ALL` sorts on every column, which matters more than it looks:
    rows are unique across the content columns after de-duplication, so this is
    a total order and the output is byte-identical between runs. A partial sort
    key (say `ts` alone) leaves ties for DuckDB's parallel sort to break
    arbitrarily, and idempotency quietly stops holding.
    """
    con.execute(
        f"COPY (SELECT * FROM {table} ORDER BY ALL) "
        f"TO '{path}' (FORMAT PARQUET, COMPRESSION ZSTD)"
    )


def _fmt(n: int) -> str:
    return f"{n:,}"


def report(
    con: duckdb.DuckDBPyConnection,
    files: list[Path],
    failed: list[tuple[Path, str]],
    drift: set[str],
    rows_read: int,
) -> None:
    """Print the Stage 1 summary the spec asks for before anything is built on top."""
    q = lambda sql: con.execute(sql).fetchone()  # noqa: E731

    raw_rows = q("SELECT count(*) FROM plays_raw")[0]
    lo, hi = q("SELECT min(ts), max(ts) FROM plays_raw")
    music_rows = q("SELECT count(*) FROM plays_raw WHERE content_type = 'music'")[0]
    plays_rows = q("SELECT count(*) FROM plays")[0]

    print()
    print("=" * 74)
    print("STAGE 1 — INGEST & NORMALIZE")
    print("=" * 74)

    print(f"\nFiles found          : {len(files)}")
    print(f"Files parsed OK      : {len(files) - len(failed)}")
    if failed:
        print(f"Files FAILED to parse: {len(failed)}")
        for f, err in failed:
            print(f"    ✗ {f.name}: {err}")
    else:
        print("Files failed to parse: 0")

    if drift:
        print(f"\n⚠ Unrecognised fields in export (schema drift): {sorted(drift)}")

    print(f"\nRows read from JSON  : {_fmt(rows_read)}")
    print(
        f"Duplicate rows dropped: {_fmt(rows_read - raw_rows)}"
        "   (same event listed twice in the export)"
    )
    print(f"plays_raw rows       : {_fmt(raw_rows)}")
    print(f"Date range           : {lo:%Y-%m-%d} -> {hi:%Y-%m-%d}")

    print("\n--- Content split (plays_raw) ---")
    for ctype, n, secs in con.execute(
        """
        SELECT content_type, count(*), sum(played_seconds)
        FROM plays_raw GROUP BY content_type ORDER BY count(*) DESC
        """
    ).fetchall():
        print(f"  {ctype:<10} {_fmt(n):>10} rows   {(secs or 0)/3600:>10,.1f} h")
    print(
        f"  {'MUSIC':<10} {_fmt(music_rows):>10} rows"
        f"   vs non-music {_fmt(raw_rows - music_rows)}"
    )

    print("\n--- The 30-second filter ---")
    dropped = music_rows - plays_rows
    pct = (dropped / music_rows * 100) if music_rows else 0
    print(f"  music rows before    : {_fmt(music_rows)}")
    print(f"  dropped (< 30s)      : {_fmt(dropped)}  ({pct:.1f}% of music rows)")
    print(f"  plays rows           : {_fmt(plays_rows)}")

    print("\n--- plays (music, >= 30s) ---")
    lo2, hi2, artists, tracks, hours = q(
        """
        SELECT min(ts), max(ts),
               count(DISTINCT artist_name),
               count(DISTINCT spotify_track_uri),
               sum(played_seconds) / 3600.0
        FROM plays
        """
    )
    print(f"  date range           : {lo2:%Y-%m-%d} -> {hi2:%Y-%m-%d}")
    print(f"  unique artists       : {_fmt(artists)}")
    print(f"  unique tracks        : {_fmt(tracks)}")
    print(f"  total listening      : {hours:,.0f} hours ({hours/24:,.0f} days)")
    print(f"  months covered       : {_fmt(q('SELECT count(DISTINCT month) FROM plays')[0])}")

    null_artist = q("SELECT count(*) FROM plays WHERE artist_name IS NULL")[0]
    if null_artist:
        print(f"  ⚠ rows with NULL artist_name: {_fmt(null_artist)}")

    from_video = q("SELECT count(*) FROM plays WHERE source_kind = 'video'")[0]
    if from_video:
        print(
            f"  note: {_fmt(from_video)} rows come from the Video history files"
            " (music videos carrying a track URI)"
        )

    # Residual near-duplicates: same event identity but differing metadata, so
    # full-row de-duplication legitimately kept them. Small enough to ignore,
    # but worth surfacing rather than hiding.
    residual = q(
        """
        SELECT coalesce(sum(n - 1), 0) FROM (
            SELECT count(*) AS n FROM plays
            GROUP BY ts, ms_played, spotify_track_uri
            HAVING count(*) > 1
        )
        """
    )[0]
    if residual:
        print(
            f"  note: {_fmt(residual)} rows share an (end-time, duration, track)"
            " identity but differ in other metadata — kept"
        )

    print("\n--- Top 10 artists by listening time ---")
    for i, (a, h, n) in enumerate(
        con.execute(
            """
            SELECT artist_name, sum(played_seconds)/3600.0 AS hours, count(*)
            FROM plays GROUP BY artist_name ORDER BY hours DESC LIMIT 10
            """
        ).fetchall(),
        start=1,
    ):
        print(f"  {i:>2}. {a[:44]:<44} {h:>8,.1f} h  {_fmt(n):>7} plays")

    print("\n--- Listening by year ---")
    for yr, h, n, a in con.execute(
        """
        SELECT year(ts) AS yr, sum(played_seconds)/3600.0, count(*),
               count(DISTINCT artist_name)
        FROM plays GROUP BY yr ORDER BY yr
        """
    ).fetchall():
        print(f"  {yr}   {h:>8,.1f} h   {_fmt(n):>7} plays   {_fmt(a):>6} artists")

    print("\n--- Privacy check ---")
    cols = {
        c[0]
        for c in con.execute("DESCRIBE SELECT * FROM plays_raw").fetchall()
    }
    for field in config.DROPPED_FIELDS:
        status = "✗ STILL PRESENT" if field in cols else "✓ absent"
        print(f"  {field:<12} {status}")

    print(f"\nWrote {config.PLAYS_RAW_PARQUET}")
    print(f"Wrote {config.PLAYS_PARQUET}")
    print("=" * 74)


def main() -> None:
    config.ensure_dirs()
    con = duckdb.connect()

    files = discover_files(config.EXPORT_DIR)
    print(f"Found {len(files)} streaming-history files in {config.EXPORT_DIR}")

    good, failed = check_parseable(con, files)
    if not good:
        sys.exit("Every file failed to parse; nothing to ingest.")

    drift = detect_schema_drift(con, good)
    rows_read = con.execute(
        "SELECT count(*) FROM read_json(?, union_by_name = true)",
        [[str(f) for f in good]],
    ).fetchone()[0]

    print("Building plays_raw ...")
    build_plays_raw(con, good)
    print("Building plays ...")
    build_plays(con)

    write_parquet(con, "plays_raw", config.PLAYS_RAW_PARQUET)
    write_parquet(con, "plays", config.PLAYS_PARQUET)

    report(con, good, failed, drift, rows_read)


if __name__ == "__main__":
    main()
