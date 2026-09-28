"""Stage 1b — recover performer credits that the export throws away.

The export carries exactly one artist field, `master_metadata_album_artist_name`,
which is the *album* artist. Featured performers are credited nowhere. In this
history that hides ~28% of listening time: 666 distinct performers appear only
inside track titles, and 389 of them never show up as an album artist at all.

This stage parses `(feat. X)` / `(with X)` out of the title and emits one row
per (track, performer). It also parses the remixer out of a `- X Remix` suffix,
which the export hides the same way: Spotify bills a remix to the ORIGINAL
artist, so an Ian Asher speed-garage rework of a Halsey song reads as pop. That
is 334 tracks and 5,810 minutes here, concentrated in dance music where remixes
and edits are how most of the material arrives. The bracketed `(X Remix)` /
`[X Remix]` is the same credit and is read where the dash form names nobody —
28 more tracks and 472 minutes, behind the guards at REMIX_BRACKET_RE.

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
  - A name carrying a separator ("Tyler, The Creator", "Chase & Status") is
    kept whole only if the library already knows it, as an album artist or
    from the poller. One it has never seen is still split.
  - Parsing is fuzzy; run with --review to eyeball what was extracted.
  - A remix suffix that names no person ("- BLM REMIX") yields a name that
    MusicBrainz cannot resolve, so it lands on Stage 2's review list and is
    answered with IGNORE. Junk degrades to a review row, never to bad data.

Output: data/track_credits.parquet
"""

from __future__ import annotations

import argparse
import re

import duckdb

import config

# A featured-credit parenthetical: "(feat. X)", "[featuring X]", "(with X)".
# Anchored on the keyword so "(Remastered)" and "(Live)" are ignored.
CREDIT_RE = r'[\(\[](?:feat\.?|featuring|ft\.?|with)\s+([^\)\]]+)[\)\]]'

# Separators inside a credit list: "A, B & C", "A and B", "A x B", and a feature
# marker INSIDE the list. "(with 21 Savage ft. Project Pat)" is one blob, and
# LEADING_NOISE_RE strips a marker only at the start of a fragment, so without
# this "21 Savage ft. Project Pat" was a single performer holding 5 hours — 11
# such names and 11 hours here. Case-insensitive inline, since the split call
# passes no flags.
#
# A capital X is NOT a separator. It cut "Ty Dolla $ign feat. X Ambassadors" at
# " X ", and "Ambassadors" resolved to an unrelated act; inside a credit blob in
# this library ` X ` had that one use and no true ones. Lowercase ` x ` stays,
# and the names that carry one ("A1 x J1", "Calle x Vida") are protected below.
SPLIT_RE = (r',\s*|\s+&\s+|\s+and\s+|\s+x\s+|\s*\+\s*'
            r'|(?i:\s+(?:feat\.?|ft\.?|featuring)\s+)')

# Characters RE2 reads as syntax outside a character class.
RE2_SPECIAL = frozenset(r'\.^$|?*+()[]{}')

# A pattern that matches nothing: the protected-name alternation when the
# library holds no name the splitter would cut.
RE2_NEVER = r'[^\s\S]'

# Trailing noise that rides along inside the parenthetical.
TRAILING_NOISE_RE = r'\s*(?:remix|cover|version|edit|mix|remaster(?:ed)?)\s*$'

# A nested credit marker survives the split when a title stacks them, e.g.
# "(with A, feat.B)" yields the fragment "feat.B". Strip it off the name.
LEADING_NOISE_RE = r'^\s*(?:feat\.?|ft\.?|featuring|with)\s*'

# Unbalanced brackets left behind when a name itself contains one, e.g. the
# artist "Suspect (AGB)" is cut to "Suspect (AGB" by the outer match.
STRAY_BRACKET_RE = r'[\(\)\[\]]'

# A remixer credit in the title suffix: "Halsey - Ian Asher Remix". Spotify
# bills the ORIGINAL artist as album artist, so the person who actually made
# the record is credited nowhere — 334 tracks and 5,810 minutes of this
# library, concentrated in dance music, where remix and edit culture is how
# most of the good material arrives. Without this, an Ian Asher speed-garage
# rework of a Halsey song is filed under pop and classified as pop.
#
# `remaster` is deliberately NOT a type here: a remaster has no remixer, and
# including it captured "2012 -" out of "War Pigs - 2012 - Remaster".
REMIX_TYPE_RE = r'(?:remix|edit|bootleg|flip|vip|rework|refix|mix|dub|version)'
REMIX_CREDIT_RE = r'\s-\s(.+?)\s' + REMIX_TYPE_RE + r'$'

# The same credit in brackets: "Falling (blackbear Remix)", "Bad Habits (feat.
# Tion Wayne & Central Cee) [Fumez The Engineer Remix]". 28 tracks and 472
# minutes here credited only to the original artist. The DASH rule is tried
# first; this one is read only when the dash rule found no remixer, and it
# reads only the LAST bracketed group, which must end the title or be followed
# by nothing but a final " - " segment — one the dash rule has just found no
# remixer in ("(Vintage Culture & Zerky Remix) - Radio Edit"). The group may
# hold no bracket of its own, so a nested one is refused, never guessed at.
REMIX_BRACKET_RE = r'[\(\[]([^\(\)\[\]]*)[\)\]](?:\s-\s[^\(\)\[\]]*)?$'
# Inside the group: the name, then one or more type words — "David Guetta
# Remix Edit" is David Guetta's, not "David Guetta Remix"'s. A bare "(Remix)"
# names nobody.
REMIX_BRACKET_NAME_RE = r'^(.+?)(?:\s+' + REMIX_TYPE_RE + r')+$'
# A group that is a credit list is not a remix credit: in "(with David Guetta
# & Little Mix)" the Mix is part of a name.
REMIX_CREDIT_LIST_RE = r'^(?:feat|ft|featuring|with)(?:[\s.]|$)'
# A possessive names its owner and then describes the record: "Tiësto's Big
# Room Remix" is Tiësto's, "Louis The Child’s version" Louis The Child's.
REMIX_POSSESSIVE_RE = r"^(.+?)['’]s(?:\s.*)?$"

# Words that describe a FORMAT rather than a person, refused whether they are
# the whole name or every word of it ("2017 Demo", "Live Orchestral", "Full
# Vocal", a number counting as such a word). Applied to BOTH forms. "Bounce -
# Radio Edit" yields "Radio", which is not an artist; left unchecked it would
# invent a performer holding 352 minutes of listening. The bracket form brought
# the voice, film and genre versions — "(Male Version)", "(Film Version)",
# "(dnb edit)", "(Jersey Club Remix)" — and the same words were already
# reaching the dash form as performers: "- Drill Remix" credited "Drill" with
# 102 minutes, "- TikTok Version" credited "TikTok". Every word here comes from
# a real title in this library, bar the obvious pairs (female, lo-fi) and the
# garage and techno edits a dance library will meet; a type word alone ("VIP
# Mix") names nobody. Not "angel", nor any other name-shaped word: "(Angel
# Mix)" is a named version, "(Hobbs & Shaw Remix)" a film, and a stoplist of
# such words never ends. They are answered in artist_overrides.csv, and they
# must be: Stage 2's review list will NOT catch a common word, because its
# exact-name rule resolves "Angel" to whichever tagged namesake ranks first, as
# it already resolved "- AWAY Remix" to a shoegaze band.
REMIX_FORMAT_STOPLIST = frozenset({
    "radio", "extended", "club", "original", "instrumental", "album", "single",
    "acoustic", "live", "dance", "main", "bonus", "deluxe", "sped up", "slowed",
    "remastered", "remaster", "mono", "stereo", "short", "long", "full",
    "clean", "dirty", "explicit", "edit", "remix", "alternate", "reprise",
    "demo", "new", "special", "super", "ultra", "hd", "hq",
    # the type words themselves
    "mix", "version", "vip", "dub", "bootleg", "flip", "rework", "refix",
    # voice, language and arrangement versions
    "male", "female", "film", "piano", "orchestral", "solo", "vocal",
    "stripped", "cinematic", "tiktok", "lofi", "lo-fi", "official", "trailer",
    "brass", "band", "renaissance", "summer", "spanish", "spanglish",
    # genre versions
    "dnb", "garage", "house", "techno", "trap", "drill", "reggae", "jersey",
})

# A capture that is only digits and punctuation is a year or a catalogue
# fragment, not a name.
REMIX_NON_NAME_RE = r'^[0-9\s\-\.,:]+$'
# The same, for one word of a name.
REMIX_NUMBER_WORD_RE = r'[0-9\-\.,:]+'


def _remix_name_ok(name: str) -> bool:
    """The guards both forms share, as build_track_credits applies them."""
    if not name:
        return False
    low = name.lower()
    return not (
        low in REMIX_FORMAT_STOPLIST
        or all(w in REMIX_FORMAT_STOPLIST or re.fullmatch(REMIX_NUMBER_WORD_RE, w)
               for w in low.split(" "))
        or re.match(REMIX_NON_NAME_RE, name)
        # A capture spanning another " - " means the title has more structure
        # than the pattern models; refuse rather than guess.
        or " - " in name)


def _is_album_artist(name: str, album_artist: str | None) -> bool:
    """A bracket version named for the album artist is theirs, not a remix:
    "(Armaan Malik Version)" on Armaan Malik, and "(Taylor's Version)" on
    Taylor Swift, whose possessive leaves only her first word."""
    if not album_artist:
        return False
    a, n = album_artist.strip(" ").lower(), name.lower()
    return n == a or a.startswith(n + " ")


def remix_credit(title: str | None, album_artist: str | None = None) -> str | None:
    """The remixer a title names, by Stage 1b's own rule, for Python callers.

    The rule remixers_sql applies in build_track_credits, step for step, so a
    title parses the same way in Stage 8's anchor judge and Stage 10's routing
    as it does in track_credits (a test runs titles through both). The dash
    form first — "Die For Me - Ian Asher Remix" — then the bracketed one —
    "Falling (blackbear Remix)". Every pattern sits inside what both RE2 and
    Python accept, and names are trimmed of spaces only, as DuckDB's trim() is.

    `album_artist` is needed for the bracket form's self-versions: without it,
    "Love Story (Taylor's Version)" names "Taylor". Pass the album artist, or
    for a Spotify search result its first-credited artist.

    It lives here rather than in either consumer because Stage 10 imports
    Stage 8: a copy in running.py could not be reached from playlists.py
    without a circular import.
    """
    title = title or ""
    m = re.search(REMIX_CREDIT_RE, title, re.IGNORECASE)
    if m and _remix_name_ok(m.group(1).strip(" ")):
        return m.group(1).strip(" ")
    m = re.search(REMIX_BRACKET_RE, title)
    if not m:
        return None
    body = m.group(1).strip(" ")
    if re.search(REMIX_CREDIT_LIST_RE, body, re.IGNORECASE):
        return None
    m = re.search(REMIX_BRACKET_NAME_RE, body, re.IGNORECASE)
    if not m:
        return None
    name = m.group(1).strip(" ")
    p = re.search(REMIX_POSSESSIVE_RE, name, re.IGNORECASE)
    if p:
        name = p.group(1).strip(" ")
    if not _remix_name_ok(name) or _is_album_artist(name, album_artist):
        return None
    return name


def _sql_str(pattern: str) -> str:
    """A pattern as the body of a single-quoted SQL literal."""
    return pattern.replace("'", "''")


def remixers_sql(source: str, title: str = "track_name",
                 album_artist: str = "album_artist") -> str:
    """remix_credit in SQL: `source`'s rows plus a `remixer` column, NULL
    where the title names none. The same patterns and guards, in the same
    order; build_track_credits reads it, and a test holds the two to agreeing.
    """
    stop = ", ".join(f"'{_sql_str(w)}'" for w in sorted(REMIX_FORMAT_STOPLIST))

    def ok(n: str) -> str:
        return f"""coalesce(
                {n} <> ''
                AND lower({n}) NOT IN ({stop})
                AND NOT list_bool_and(list_transform(
                        string_split(lower({n}), ' '),
                        w -> list_contains([{stop}], w)
                             OR regexp_full_match(w, '{REMIX_NUMBER_WORD_RE}')))
                AND NOT regexp_matches({n}, '{REMIX_NON_NAME_RE}')
                AND NOT contains({n}, ' - '), false)"""

    body = f"trim(regexp_extract({title}, '{REMIX_BRACKET_RE}', 1))"
    poss = _sql_str(REMIX_POSSESSIVE_RE)
    return f"""
        SELECT * EXCLUDE (_rc_dash, _rc_body, _rc_bracket),
            CASE
                WHEN {ok('_rc_dash')} THEN _rc_dash
                WHEN {ok('_rc_bracket')}
                     AND NOT coalesce(
                         lower(_rc_bracket) = lower(trim({album_artist}))
                         OR starts_with(lower(trim({album_artist})),
                                        lower(_rc_bracket) || ' '), false)
                    THEN _rc_bracket
            END AS remixer
        FROM (
            SELECT *,
                trim(CASE WHEN regexp_matches(_rc_body, '{poss}', 'i')
                          THEN regexp_extract(_rc_body, '{poss}', 1, 'i')
                          ELSE _rc_body END) AS _rc_bracket
            FROM (
                SELECT *,
                    trim(regexp_extract({title}, '{REMIX_CREDIT_RE}', 1, 'i'))
                        AS _rc_dash,
                    CASE WHEN regexp_matches({body}, '{REMIX_CREDIT_LIST_RE}', 'i')
                         THEN ''
                         ELSE trim(regexp_extract(
                             {body}, '{REMIX_BRACKET_NAME_RE}', 1, 'i'))
                    END AS _rc_body
                FROM {source}
            )
        )"""


def _re2_escape(name: str) -> str:
    """Escape a literal name for RE2: its own syntax characters, not
    re.escape's list, which is Python's and has changed between versions."""
    return "".join("\\" + c if c in RE2_SPECIAL else c for c in name)


def build_protected_names(con: duckdb.DuckDBPyConnection) -> str:
    """Names the splitter would cut, and the RE2 pattern that finds them.

    A separator is also a character real names carry. SPLIT_RE turned
    "(feat. Tyler, The Creator & Billie Essco)" into "Tyler" and "The Creator",
    and the all-caps "GRAVITY (FEAT. TYLER, THE CREATOR)" into two more — four
    performers who do not exist, 1.5 h between them. The library already knows
    the real spellings: every album artist, and every name the poller has seen.
    Those that SPLIT_RE would cut are taken out of a blob whole before it is
    split ("Chase & Status", "Earth, Wind & Fire", "Florence + The Machine").

    Creates table `protected_names` (`key` = lower(name), `name` = the
    canonical spelling) and returns
    `(?i)(^|[^[:alnum:]])(<names>)($|[^[:alnum:]])`. RE2 has no lookbehind, so
    the boundary characters are captured as groups 1 and 3 and put back.
    """
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE protected_names AS
        WITH names AS (
            SELECT trim(artist_name) AS name FROM plays
            WHERE artist_name IS NOT NULL
            UNION ALL
            SELECT trim(unnest(string_split(track_artists, chr(31)))) FROM plays
            WHERE track_artists IS NOT NULL AND track_artists <> ''
        ),
        cut AS (
            -- Padded: a name is cut where it sits INSIDE a blob, and there a
            -- separator word at its edge has the whitespace it needs.
            -- Case-insensitive, because the match below is: the library spells
            -- "Tones And I" and a title "(feat. Tones and I)", which the
            -- lowercase " and " cut into "Tones" and a dropped one-letter "I".
            SELECT name, count(*) AS n FROM names
            WHERE regexp_matches(' ' || name || ' ', '{SPLIT_RE}', 'i')
            GROUP BY name
        )
        -- One spelling per name however a title capitalises it: the one most
        -- plays carry, ties broken by the name itself so re-runs agree.
        SELECT lower(name) AS key, first(name ORDER BY n DESC, name) AS name
        FROM cut
        GROUP BY 1
        """
    )
    names = [r[0] for r in con.execute("SELECT name FROM protected_names").fetchall()]
    # Longest first: RE2 takes the first alternative that matches at a
    # position, so "A & B & C" has to be tried before "A & B". The name breaks
    # length ties so the pattern text is identical run to run.
    names.sort(key=lambda s: (-len(s), s))
    alts = "|".join(_re2_escape(n) for n in names) or RE2_NEVER
    return rf"(?i)(^|[^[:alnum:]])({alts})($|[^[:alnum:]])"


def build_track_credits(con: duckdb.DuckDBPyConnection) -> None:
    # Spliced into single-quoted SQL literals, so a name's own quote is doubled
    # (an "O'Brien & Sons" would otherwise end the string).
    protected = build_protected_names(con).replace("'", "''")
    # Swap each protected name for the 0x1f mask, keeping its boundaries. The
    # mask is only ever a split point and never reaches output.
    mask = r"'\1' || chr(31) || '\3'"
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
        -- Protected names come out of the blob whole, and only the rest is
        -- split. Two passes, because a match consumes its trailing boundary:
        -- in "Chase & Status,Tyler, The Creator" the comma ends the first
        -- match, and the second name has no leading boundary left until the
        -- first has been masked and the comma put back.
        masked AS (
            SELECT
                *,
                regexp_replace(credit_blob, '{protected}', {mask}, 'g') AS pass1
            FROM parsed
            WHERE credit_blob IS NOT NULL AND credit_blob <> ''
        ),
        blobs AS (
            SELECT
                spotify_track_uri, track_name, album_artist,
                played_seconds, n_plays,
                list_concat(
                    regexp_extract_all(credit_blob, '{protected}', 2),
                    regexp_extract_all(pass1, '{protected}', 2)
                ) AS protected_hits,
                regexp_replace(pass1, '{protected}', {mask}, 'g') AS remainder
            FROM masked
        ),
        featured AS (
            -- A hit carries the title's casing; the library's spelling wins, so
            -- "TYLER, THE CREATOR" is credited as "Tyler, The Creator". LEFT,
            -- so a hit survives where RE2's case folding and lower() disagree.
            SELECT
                h.spotify_track_uri, h.track_name, h.album_artist,
                h.played_seconds, h.n_plays,
                coalesce(p.name, h.hit) AS artist_name
            FROM (
                SELECT
                    spotify_track_uri, track_name, album_artist,
                    played_seconds, n_plays,
                    unnest(protected_hits) AS hit
                FROM blobs
            ) h
            LEFT JOIN protected_names p ON p.key = lower(h.hit)
            UNION ALL
            -- The mask is a split point too, written as RE2's hex escape.
            SELECT
                spotify_track_uri, track_name, album_artist,
                played_seconds, n_plays,
                trim(
                    regexp_replace(
                        regexp_replace(
                            regexp_replace(
                                unnest(regexp_split_to_array(
                                    remainder, '{SPLIT_RE}|\\x1f')),
                                '{LEADING_NOISE_RE}', '', 'i'
                            ),
                            '{TRAILING_NOISE_RE}', '', 'i'
                        ),
                        '{STRAY_BRACKET_RE}', '', 'g'
                    )
                ) AS artist_name
            FROM blobs
        ),
        -- The remixer the title names, dash form or bracketed (remixers_sql,
        -- which is remix_credit's rule). Same floor-not-fix caveat as
        -- `featured`: it only sees credits Spotify spelled into the title.
        remixers AS (
            SELECT
                spotify_track_uri, track_name, album_artist,
                played_seconds, n_plays,
                remixer AS artist_name
            FROM ({remixers_sql('unseen')})
            WHERE remixer IS NOT NULL
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
                   n_plays, artist_name,
                   'remixer' AS credit_type, 'export' AS credit_source
            FROM remixers
            -- An artist remixing their own record ("Gravagerz - Gravagerz
            -- Remix") is one performer, not two.
            WHERE artist_name IS DISTINCT FROM album_artist
            UNION ALL
            SELECT spotify_track_uri, track_name, album_artist, played_seconds,
                   n_plays, artist_name, credit_type, 'poller' AS credit_source
            FROM polled
        ),
        deduped AS (
            SELECT * FROM unioned
            WHERE artist_name IS NOT NULL
              AND artist_name <> ''
              AND (
                  -- The junk filter is for regex fragments. The album artist is
                  -- the export's own field and a poller name is Spotify's, so
                  -- neither faces it: it dropped the album artist `¥$` for
                  -- carrying no ASCII letter, and "CARNIVAL - HOOLIGANS
                  -- VERSION" (220 plays, 11 h) was credited only to its
                  -- remixer, and to nobody at all under album_artist_only.
                  credit_type = 'album_artist'
                  OR credit_source = 'poller'
                  OR (length(artist_name) BETWEEN 2 AND 60
                      -- Drop fragments that are punctuation or stray words.
                      AND regexp_matches(artist_name, '[A-Za-z0-9]'))
              )
            -- One performer, one row per track, whatever the title calls them.
            -- Stage 3 partitions credit weight by the play's identity, so a
            -- performer appearing twice would hand that track's listening time
            -- out twice over — the invariant that fails silently.
            QUALIFY row_number() OVER (
                PARTITION BY spotify_track_uri, artist_name
                ORDER BY CASE credit_type
                             WHEN 'album_artist' THEN 0
                             WHEN 'featured'     THEN 1
                             ELSE 2 END
            ) = 1
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
        FROM deduped
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
    n_remix = q("SELECT count(DISTINCT artist_name) FROM track_credits "
                "WHERE credit_type = 'remixer'")[0]
    n_remix_tracks, remix_secs = q(
        "SELECT count(*), coalesce(sum(played_seconds), 0) FROM ("
        "  SELECT DISTINCT spotify_track_uri, played_seconds FROM track_credits"
        "  WHERE credit_type = 'remixer')"
    )
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
    print(f"  as remixer                : {n_remix:,}")
    print(f"  featured-ONLY (new)       : {n_new:,}   <- invisible in plays today")
    print(
        f"\nremixer credits recovered   : {n_remix_tracks:,} tracks, "
        f"{remix_secs/3600:,.1f} h ({100*remix_secs/total_secs:.1f}% of total)"
    )
    print("   (the export bills these to the ORIGINAL artist, not the remixer)")
    print(
        f"\nlistening time on tracks with a parsed feature: "
        f"{feat_secs/3600:,.1f} h ({100*feat_secs/total_secs:.1f}% of total)"
    )
    # What the protection actually did, not how many names it could have done
    # it for: the library knows dozens of such names, and at the time of
    # writing a title used one ("Tyler, The Creator", 3 tracks). A featured
    # export credit can only carry a protected name by way of the protection,
    # since the splitter cuts every one of them.
    n_protected = q("SELECT count(*) FROM protected_names")[0]
    n_kept, n_kept_tracks = q(
        """
        SELECT count(DISTINCT t.artist_name), count(DISTINCT t.spotify_track_uri)
        FROM track_credits t JOIN protected_names p ON p.key = lower(t.artist_name)
        WHERE t.credit_type = 'featured' AND t.credit_source = 'export'
        """
    )
    print(
        f"separator-bearing names kept whole: {n_kept:,} on {n_kept_tracks:,} "
        f"tracks (of {n_protected:,} the library knows)"
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

    print("\n--- Top remixers (the export credits the original artist) ---")
    for a, h, n in con.execute(
        """
        SELECT artist_name, sum(played_seconds)/3600.0 AS h, count(*) AS n
        FROM track_credits WHERE credit_type = 'remixer'
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
    # played_seconds is a float sum, and a parallel aggregate adds floats in
    # whatever order the threads finish. ORDER BY ALL fixes the rows, not their
    # values; one thread fixes both, so re-runs stay byte-identical.
    con.execute("SET threads = 1")
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
