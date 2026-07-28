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
| 2. Enrich | `.venv/bin/python enrich.py` | `data/artist_tags.parquet`, `.cache/` |
| 3. Analyse | `.venv/bin/python analyze.py` | `data/tag_trends.parquet` |
| 4. Dashboard | `.venv/bin/streamlit run app.py` | interactive |

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
