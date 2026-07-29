# Spotify Listening History — Trend Analysis

Ingests a Spotify Extended Streaming History export into a queryable DuckDB +
Parquet dataset and measures how genre taste has shifted over roughly a decade.

Built for **quarterly re-runs by hand**, not continuous operation. Every stage is
a re-runnable script; nothing holds state in a notebook.

## Your data never enters git

`.gitignore` excludes the raw export, the zip it came in, every derived Parquet,
the enrichment cache, and `.env`. Code and configuration are tracked; listening
history is not.

The export's `ip_addr` field is dropped during Stage 1 and does not exist in any
derived artifact. Stage 1 asserts this and prints a privacy check on every run.

Verify at any time:

```bash
git status --short                              # nothing from the export
git check-ignore -v "Spotify Extended Streaming History/"
```

## Setup

Everything runs through a project-local virtualenv.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Place the export at `./Spotify Extended Streaming History/`, or point
`SPOTIFY_EXPORT_DIR` somewhere else in a `.env` file.

## Stages

| Stage | Command | Output |
|-------|---------|--------|
| 1. Ingest | `.venv/bin/python ingest.py` | `data/plays_raw.parquet`, `data/plays.parquet` |
| 1b. Credits | `.venv/bin/python credits.py` | `data/track_credits.parquet` |
| 2. Enrich | `.venv/bin/python enrich.py` | `data/artist_tags.parquet`, `.cache/` |
| 3. Analyse | `.venv/bin/python analyze.py` | `data/tag_trends.parquet` |
| 4. Dashboard | `.venv/bin/streamlit run app.py --server.address 127.0.0.1` | interactive |

Run them in that order. Only Stage 2 touches the network.

**Bind the dashboard to localhost.** Streamlit listens on every interface by
default and prints an external URL on your public IP. This page renders your
personal listening history, so `--server.address 127.0.0.1` is part of the
command rather than an optional extra. Drop it only if you actually want the
page reachable from your LAN.

### Stage 1 — Ingest and normalize

DuckDB reads the export JSON directly and writes Parquet. Nothing is staged
through pandas.

Two tables:

- **`plays_raw`** — every row, unfiltered. Skip behaviour is interesting on its
  own, so nothing is thrown away here.
- **`plays`** — music only (`spotify_track_uri IS NOT NULL`, which excludes
  podcasts and audiobooks) and real listens only (`ms_played >= 30000`).

**Idempotent.** The export is re-read in full on every run and the Parquet is
rewritten, so a re-run against the same export produces byte-identical files and
a run against a newer export simply picks up the new plays. There is no
incremental state to drift. This is enforced by writing under a total row order
(`ORDER BY ALL`) — a partial sort key leaves ties for DuckDB's parallel sort to
break arbitrarily and idempotency silently stops holding.

**De-duplication.** The export lists some events more than once. Since `ts` is
the play *end* time, an identical `(ts, ms_played, track)` triple cannot be two
distinct listens, so full-row duplicates are collapsed. Rows sharing that
identity but differing in other metadata are kept, and the count is reported.

### Stage 1b — Track credits

The export's only artist field is the **album** artist, so featured performers
are credited nowhere. That hides about **28% of listening time**: 681 performers
appear solely inside track titles and 407 never appear as an album artist at all.
`credits.py` parses `(feat. X)` / `(with X)` out of the title and records one row
per performer with a `credit_type`.

No weights are stored. Stage 3 decides what a feature is worth at query time, so
album-artist-only is simply the weight-0 case and remains available.

It only catches features named in the *title* — features living solely in
Spotify's track metadata stay invisible. This is a floor, not a fix. Run
`credits.py --review` to eyeball what the regex extracted.

### Stage 2 — Genre enrichment

One MusicBrainz search per artist yields both the MBID and a tag vector. Tags are
filtered against MusicBrainz's canonical genre vocabulary (~2,180 terms, fetched
once) because raw search tags carry noise like `usa`, `english`, `2010s` and
`gen z` alongside real genres.

Two deviations from the original plan, both forced by the APIs:

- ListenBrainz's `bulk-tag-lookup` is keyed on **recording** MBIDs, not artist
  MBIDs, so it cannot consume an artist resolution. Using it would mean resolving
  every track to a recording MBID first — the per-track explosion this design set
  out to avoid.
- The search must query `artist:"X" OR alias:"X"`, and rank the candidates
  rather than taking the first exact match. Searching the name alone silently
  loses every renamed artist — "Kanye West" returns a tribute band, because the
  entry is now "Ye" with the old name demoted to an alias. And an obscure
  artist holding the name as an *alias* can outrank the artist whose name it
  actually is: "Wale" returned a percussionist at score 100 ahead of the US
  rapper at 82. A primary-name match therefore beats an alias match.

Artists that resolve to an MBID but carry no tags are backfilled from their
**release-group** tags, which are frequently populated even when the artist
page is not.

**Spotify is not used as a fallback**, despite being the obvious candidate. Its
artist endpoint used to return a `genres` array and no longer does:
`/v1/artists/{id}` answers 200 with `genres`, `popularity` and `followers` all
absent (verified against Drake, Taylor Swift, Metallica and ArrDee), batch
`/v1/artists` and `/top-tracks` answer 403, and search never carried genres.
There is no genre data left there to fall back to. ListenBrainz's metadata
lookup would serve, but returns 401 without an Authorization header — the
no-auth guarantee covers only the `labs` host.

Resolution is strict — an exact normalised match against name or alias, never a
guess — and everything else lands on a review list. Names are folded on accents,
case and stylisation so `A$AP Rocky` and `ASAP Rocky` collapse together.

**Resumable by design.** Each resolution is appended to `.cache/*.jsonl` and
fsynced, so an interrupted run resumes exactly where it stopped and a quarterly
re-run only spends requests on artists it has never seen. MusicBrainz is
throttled to roughly 1 req/sec with a descriptive User-Agent; a 503 is answered
with a real backoff, since MusicBrainz sends `Retry-After: 0` and trusting it
means no backoff at all.

### Stage 3 — Analysis

Share of listening time per tag per month, weighted by `ms_played`, under two
normalised weightings: a play's time splits across its performers, then each
artist's share splits across their genres by MusicBrainz vote count, capped at 8.

The cap matters. Ye carries 58 tags; splitting evenly would give each 1/58 while
a two-tag artist gives each a half, systematically burying well-tagged artists.

Trend classification gates on absolute size before computing relative change.
Without that floor a genre sitting at a fraction of a percent posts "+540% a
year" off a 0.3pp move and swamps the rankings.

### Stage 4 — Output surfaces

Every chart lives in `figures.py` as a function returning a Plotly figure; the
dashboard and the report both import from it, so chart code is never written
twice. The static HTML report is deliberately deferred until the dashboard has
been used and it is clear which charts are worth keeping.

Colour is anchored to the canonical global genre ranking rather than to list
position, so filtering out one genre never repaints the ones still on screen.

## Known data flaws

- `master_metadata_album_artist_name` is the **album** artist, not the track
  artist. On compilations, features, and soundtracks it misattributes the play.
  Accepted at the artist level for now; it will skew some per-artist totals.
- `ts` is UTC. Monthly buckets are therefore UTC months.
- Rows from the Video history files that carry a track URI are eligible for
  `plays`, but in practice all are short snippets that the 30-second floor
  removes.

## Out of scope

No recommendation engine yet, and no poller to keep history current. The
`tag_trends` table is the contract a recommendation layer would consume later.
