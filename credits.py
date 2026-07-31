"""Stage 1b — recover performer credits that the export throws away.

The export carries exactly one artist field, `master_metadata_album_artist_name`,
which is the *album* artist. Featured performers are credited nowhere. In this
history that hides ~28% of listening time: 666 distinct performers appear only
inside track titles, and 389 of them never show up as an album artist at all.

This stage parses `(feat. X)` / `(with X)` out of the title and emits one row
per (track, performer).

    .venv/bin/python credits.py

Deliberately stores NO weights. `credit_type` is recorded and Stage 3 decides
what a feature is worth at query time, so "album artist only" is simply the
weight-0 case and nothing is baked in.

Two sources, and the better one wins per track:

  - `poller`  — the real performer list from Spotify's recently-played endpoint,
    recorded by Stage 6. Applies to EVERY play of that track, export rows
    included, so one polled listen repairs the whole history of that track.
  - `export`  — the album artist plus whatever the title regex admits. Used only
    for tracks the poller has never seen.

Caveats on the regex path, by construction:
  - Only catches features named in the *title*. Features that live solely in
    Spotify's track metadata stay invisible. This is a floor, not a fix.
  - `(with <producer>)` credits a producer as a performer. Accepted.
  - Parsing is fuzzy; run with --review to eyeball what was extracted.

Output: data/track_credits.parquet
"""

from __future__ import annotations

import argparse

import duckdb

import config

# A featured-credit parenthetical: "(feat. X)", "[featuring X]", "(with X)".
# Anchored on the keyword so "(Remastered)" and "(Live)" are ignored.
CREDIT_RE = r'[\(\[](?:feat\.?|featuring|ft\.?|with)\s+([^\)\]]+)[\)\]]'

# Separators inside a credit list: "A, B & C", "A and B", "A x B".
SPLIT_RE = r',\s*|\s+&\s+|\s+and\s+|\s+x\s+|\s+X\s+|\s*\+\s*'

# Trailing noise that rides along inside the parenthetical.
TRAILING_NOISE_RE = r'\s*(?:remix|cover|version|edit|mix|remaster(?:ed)?)\s*$'

# A nested credit marker survives the split when a title stacks them, e.g.
# "(with A, feat.B)" yields the fragment "feat.B". Strip it off the name.
LEADING_NOISE_RE = r'^\s*(?:feat\.?|ft\.?|featuring|with)\s*'

# Unbalanced brackets left behind when a name itself contains one, e.g. the
# artist "Suspect (AGB)" is cut to "Suspect (AGB" by the outer match.
STRAY_BRACKET_RE = r'[\(\)\[\]]'


def build_track_credits(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        f"""
        CREATE OR REPLACE TABLE track_credits AS
        WITH tracks AS (
            -- One row per distinct track, carrying its listening weight so the
            -- review output can be ordered by what actually matters.
            SELECT
                spotify_track_uri,
                any_value(track_name)  AS track_name,
                any_value(artist_name) AS album_artist,
                sum(played_seconds)    AS played_seconds,
                count(*)               AS n_plays,
                -- The poller records the true performer list for tracks it has
                -- seen. max() ignores NULLs and is deterministic, so a track
                -- the poller has seen even once gets its real credits applied
                -- to EVERY play of it, including years-old export rows.
                max(track_artists)     AS polled_artists
            FROM plays
            GROUP BY spotify_track_uri
        ),

        -- ---- Poller path: real credits, no guessing --------------------------
        polled AS (
            SELECT
                spotify_track_uri, track_name, album_artist,
                played_seconds, n_plays,
                trim(a.name) AS artist_name,
                -- Spotify lists the primary performer first; the rest are
                -- features. Mapping onto the existing two credit types keeps
                -- Stage 3's weighting untouched.
                CASE WHEN a.pos = 1 THEN 'album_artist' ELSE 'featured' END
                    AS credit_type
            FROM (
                SELECT
                    *,
                    unnest(list_transform(
                        string_split(polled_artists, chr(31)),
                        (x, i) -> {{'name': x, 'pos': i}}
                    )) AS a
                FROM tracks
                WHERE polled_artists IS NOT NULL AND polled_artists <> ''
            )
            -- A track billing the same artist twice must not be counted twice.
            QUALIFY row_number() OVER (
                PARTITION BY spotify_track_uri, trim(a.name) ORDER BY a.pos
            ) = 1
        ),

        -- ---- Export path: album artist + whatever the title admits ----------
        -- Only for tracks the poller has never seen. Where it has, its answer
        -- replaces this entirely rather than being merged with it.
        unseen AS (
            SELECT * FROM tracks WHERE polled_artists IS NULL OR polled_artists = ''
        ),
        parsed AS (
            SELECT
                *,
                regexp_extract(track_name, '{CREDIT_RE}', 1, 'i') AS credit_blob
            FROM unseen
        ),
        featured AS (
            SELECT
                spotify_track_uri, track_name, album_artist,
                played_seconds, n_plays,
                trim(
                    regexp_replace(
                        regexp_replace(
                            regexp_replace(
                                unnest(regexp_split_to_array(credit_blob, '{SPLIT_RE}')),
                                '{LEADING_NOISE_RE}', '', 'i'
                            ),
                            '{TRAILING_NOISE_RE}', '', 'i'
                        ),
                        '{STRAY_BRACKET_RE}', '', 'g'
                    )
                ) AS artist_name
            FROM parsed
            WHERE credit_blob IS NOT NULL AND credit_blob <> ''
        ),
        album_artists AS (
            SELECT
                spotify_track_uri, track_name, album_artist,
                played_seconds, n_plays,
                album_artist AS artist_name
            FROM unseen
            WHERE album_artist IS NOT NULL
        ),
        unioned AS (
            SELECT spotify_track_uri, track_name, album_artist, played_seconds,
                   n_plays, artist_name,
                   'album_artist' AS credit_type, 'export' AS credit_source
            FROM album_artists
            UNION ALL
            SELECT spotify_track_uri, track_name, album_artist, played_seconds,
                   n_plays, artist_name,
                   'featured' AS credit_type, 'export' AS credit_source
            FROM featured
            -- A performer who is both the album artist and named in the title
            -- must not be counted twice.
            WHERE artist_name IS DISTINCT FROM album_artist
            UNION ALL
            SELECT spotify_track_uri, track_name, album_artist, played_seconds,
                   n_plays, artist_name, credit_type, 'poller' AS credit_source
            FROM polled
        )
        SELECT
            spotify_track_uri,
            track_name,
            album_artist,
            artist_name,
            credit_type,
            credit_source,
            played_seconds,
            n_plays,
            count(*) OVER (PARTITION BY spotify_track_uri) AS n_performers
        FROM unioned
        WHERE artist_name IS NOT NULL
          AND length(artist_name) BETWEEN 2 AND 60
          -- Drop fragments that are punctuation or stray words, not names.
          AND regexp_matches(artist_name, '[A-Za-z0-9]')
        ORDER BY ALL
        """
    )


def report(con: duckdb.DuckDBPyConnection, review: bool) -> None:
    q = lambda sql: con.execute(sql).fetchone()  # noqa: E731

    total_secs = q("SELECT sum(played_seconds) FROM plays")[0]
    n_rows, n_tracks, n_artists = q(
        "SELECT count(*), count(DISTINCT spotify_track_uri), "
        "count(DISTINCT artist_name) FROM track_credits"
    )
    n_album = q("SELECT count(DISTINCT artist_name) FROM track_credits "
                "WHERE credit_type = 'album_artist'")[0]
    n_feat = q("SELECT count(DISTINCT artist_name) FROM track_credits "
               "WHERE credit_type = 'featured'")[0]
    n_new = q(
        """
        SELECT count(*) FROM (
            SELECT DISTINCT artist_name FROM track_credits WHERE credit_type = 'featured'
            EXCEPT
            SELECT DISTINCT artist_name FROM track_credits WHERE credit_type = 'album_artist'
        )
        """
    )[0]
    feat_secs = q(
        "SELECT coalesce(sum(played_seconds), 0) FROM ("
        "  SELECT DISTINCT spotify_track_uri, played_seconds FROM track_credits"
        "  WHERE credit_type = 'featured')"
    )[0]

    print()
    print("=" * 74)
    print("STAGE 1b — TRACK CREDITS")
    print("=" * 74)
    print(f"\ncredit rows                 : {n_rows:,}")
    print(f"tracks covered              : {n_tracks:,}")
    print(f"distinct performers         : {n_artists:,}")
    print(f"  as album artist           : {n_album:,}")
    print(f"  as featured performer     : {n_feat:,}")
    print(f"  featured-ONLY (new)       : {n_new:,}   <- invisible in plays today")
    print(
        f"\nlistening time on tracks with a parsed feature: "
        f"{feat_secs/3600:,.1f} h ({100*feat_secs/total_secs:.1f}% of total)"
    )
    print(f"\nartists Stage 2 must resolve: {n_artists:,}")

    # ---- What the poller repaired -----------------------------------------
    n_rep_tracks, rep_secs = q(
        """
        SELECT count(*), coalesce(sum(played_seconds), 0) FROM (
            SELECT DISTINCT spotify_track_uri, played_seconds
            FROM track_credits WHERE credit_source = 'poller')
        """
    )
    print("\n--- Credits repaired from the poller ---")
    if not n_rep_tracks:
        print("   none — the poller has not seen any of these tracks yet.")
        print("   Run poll.py, then ingest.py, then this stage again; every")
        print("   track it sees gets true credits on ALL of its plays.")
    else:
        rep_only = q(
            """
            SELECT count(*) FROM (
                SELECT DISTINCT artist_name FROM track_credits
                WHERE credit_source = 'poller'
                EXCEPT
                SELECT DISTINCT artist_name FROM track_credits
                WHERE credit_source = 'export')
            """
        )[0]
        print(f"   tracks with true credits  : {n_rep_tracks:,}")
        print(
            f"   listening time repaired   : {rep_secs/3600:,.1f} h "
            f"({100*rep_secs/total_secs:.1f}% of total)"
        )
        print(f"   performers only the poller found: {rep_only:,}")
        print("   (applied to every play of those tracks, export rows included)")

    print("\n--- Top featured performers (credited to someone else today) ---")
    for a, h, n in con.execute(
        """
        SELECT artist_name, sum(played_seconds)/3600.0 AS h, count(*) AS n
        FROM track_credits WHERE credit_type = 'featured'
        GROUP BY 1 ORDER BY h DESC LIMIT 12
        """
    ).fetchall():
        print(f"   {a[:38]:<38} {h:>6,.1f} h  {n:>4} tracks")

    if review:
        print("\n--- REVIEW: every parsed featured name, rarest first ---")
        print("    (scan for regex junk: fragments, producers, non-names)")
        for a, h, n in con.execute(
            """
            SELECT artist_name, sum(played_seconds)/3600.0 AS h, count(*) AS n
            FROM track_credits WHERE credit_type = 'featured'
            GROUP BY 1 ORDER BY h ASC
            """
        ).fetchall():
            print(f"   {h:>6.2f} h  {n:>3}x  {a}")

    print(f"\nWrote {config.DATA_DIR / 'track_credits.parquet'}")
    print("=" * 74)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--review", action="store_true",
        help="print every parsed featured name so regex junk can be spotted",
    )
    args = ap.parse_args()

    config.ensure_dirs()
    if not config.PLAYS_PARQUET.exists():
        raise SystemExit(f"{config.PLAYS_PARQUET} not found — run ingest.py first.")

    con = duckdb.connect()
    con.execute(f"CREATE VIEW plays AS SELECT * FROM '{config.PLAYS_PARQUET}'")
    # A plays.parquet written before the poller existed has no track_artists
    # column. Synthesise it so the repair path is simply empty rather than a
    # hard error on an otherwise valid dataset.
    cols = {r[0] for r in con.execute("DESCRIBE plays").fetchall()}
    if "track_artists" not in cols:
        con.execute(
            "CREATE OR REPLACE VIEW plays AS SELECT *, "
            f"CAST(NULL AS VARCHAR) AS track_artists FROM '{config.PLAYS_PARQUET}'")
    build_track_credits(con)
    con.execute(
        f"COPY (SELECT * FROM track_credits ORDER BY ALL) "
        f"TO '{config.DATA_DIR / 'track_credits.parquet'}' "
        f"(FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    report(con, args.review)


if __name__ == "__main__":
    main()
