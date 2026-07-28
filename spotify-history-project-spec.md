# Spotify Listening History — Trend Analysis

Standalone project. Ingest ~151 MB of Spotify Extended Streaming History, build a queryable dataset, and measure how genre taste has shifted over roughly a decade.

Source data: `./Spotify Extended Streaming History/`

Stack: DuckDB + Parquet for storage and querying, Plotly for figures, Streamlit for the dashboard.

## Non-negotiable: keep the data out of git

Before writing any code, create `.gitignore` with at minimum:

```
Spotify Extended Streaming History/
data/
*.parquet
*.duckdb
*.json
.cache/
.env
```

Code, configs, and the app are tracked. The raw export, every derived data artifact, and API credentials are not. Verify with `git status` that nothing from the export directory is staged before the first commit.

The export contains an `ip_addr` field. Drop it during normalization — it should not exist in any derived artifact.

## Design constraint: quarterly re-runs

I'll refresh this every few months, not continuously. Build for that:

- Every stage is a re-runnable script, not a notebook holding state.
- The enrichment cache persists across runs and is never rebuilt from scratch. A re-run only resolves artists it hasn't seen.
- Ingestion is idempotent. Re-running against the same export produces the same output, and against a newer export picks up only new plays.

Don't build a scheduler. I'll run it by hand.

## Stage 1: Ingest and normalize

Files are `Streaming_History_Audio_YYYY-YYYY_N.json`. There may also be video or podcast files — inspect the directory rather than assuming.

Read the JSON with DuckDB and write a normalized Parquet file. Don't stage this through pandas.

Per-play fields include: `ts`, `platform`, `ms_played`, `conn_country`, `master_metadata_track_name`, `master_metadata_album_artist_name`, `master_metadata_album_album_name`, `spotify_track_uri`, `episode_name`, `episode_show_name`, `reason_start`, `reason_end`, `shuffle`, `skipped`, `offline`, `incognito_mode`. Newer exports may carry audiobook fields. Confirm the real schema rather than trusting this list.

Two tables:

- `plays_raw` — everything, unfiltered. Skip behavior is interesting on its own.
- `plays` — music only (`spotify_track_uri IS NOT NULL`, which excludes podcasts and audiobooks) and real listens only (`ms_played >= 30000`).

**Known data flaw:** `master_metadata_album_artist_name` is the *album* artist, not the track artist. On compilations, features, and soundtracks it's wrong. Accept it at the artist level for now, but note that it will misattribute some plays.

Report before continuing: total rows, date range, unique artists, unique tracks, music vs non-music split, rows dropped by the 30s filter, files that failed to parse.

## Stage 2: Genre enrichment

Goal is a tag vector per artist. This is the expensive stage — cache aggressively, make it resumable, assume it won't finish cleanly on the first attempt.

**Do not iterate Spotify's track endpoint.** Batch lookups for tracks, artists, and albums were removed from the Web API in February 2026, so a per-track loop is one request per unique track against a rate limit. Wrong order of magnitude.

Work from distinct artist names instead:

1. Extract unique artist names from `plays`.
2. Resolve each to a MusicBrainz MBID — ListenBrainz's MBID mapping service where possible, MusicBrainz search by name as fallback. Ambiguous or unresolved names go to a review list. Don't guess.
3. Pull tags in bulk via ListenBrainz's `bulk-tag-lookup` (labs API, no auth required).
4. Backfill remaining gaps with Spotify's artist endpoint, which still returns `genres`. Fallback only, not the primary path.

Cache every resolution to disk keyed by artist name. Throttle MusicBrainz to 1 req/sec with a descriptive User-Agent or they'll block you.

Report the resolution rate weighted by listening time. If a large share of my listening attaches to unresolved artists, the trend analysis is unreliable and I need to know that before seeing a single chart.

## Stage 3: Analysis layer

Core metric: **share of listening time per tag, bucketed by month.**

- Weight by `ms_played`, not play count.
- Artists carry multiple tags — distribute each play's time across all of an artist's tags rather than picking one.
- Apply a 3-month rolling mean before anything gets plotted. Raw monthly shares are unreadable.
- Compute trailing-12-month slope per tag and classify rising / flat / declining.

Write the result as a `tag_trends` table (tag, month, share, smoothed_share, slope) in Parquet. This is the contract a recommendation layer will consume later, so keep it clean.

Secondary metrics, all cheap:

- Discovery rate: distinct new artists per month
- Repeat concentration: share of listening time in the top 1% of artists, by year
- Skip rate by tag, derived from `reason_end` and `skipped`

## Stage 4: Output surfaces

**One figure module, two renderers.** Put every chart in `figures.py` as a function returning a Plotly figure. The dashboard and the report both import from it. Do not write chart code twice.

### Streamlit dashboard (build this first)

Interactive exploration. Minimum viable set of controls:

- Date range selector
- Tag multi-select for the trajectory view
- Toggle for smoothed vs raw
- Minimum listening-time threshold to filter out long-tail tag noise

Views: stacked area of top tags over time, individual tag trajectories as small multiples, rising vs declining ranking for the trailing year, and the secondary metrics on a second page.

Query Parquet directly through DuckDB. Wrap loads in `@st.cache_data` so the app isn't re-reading the dataset on every widget interaction.

### Static HTML report (build second)

Defer this until I've used the dashboard and know which charts I actually return to. Then render those same figure functions into a single self-contained HTML file with `include_plotlyjs='inline'` so it works offline. Write it to an `output/` directory.

## Out of scope

No recommendation engine yet. No poller for keeping history current. Both come after this works.

## Working style

Stop and show me results after Stage 1 and again after Stage 2 before building on top of them. I want to see the shape of the data and the enrichment coverage before any analysis exists.

Prefer scripts I can re-run over notebooks. Prefer SQL in DuckDB over dataframe manipulation where the two are equivalent — I'm using this project to get familiar with DuckDB, so lean into it.
