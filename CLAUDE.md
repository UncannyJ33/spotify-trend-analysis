# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment

Everything runs through the project-local venv (Python 3.12). Never invoke the system interpreter.

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## Commands

```bash
.venv/bin/python ingest.py                 # Stage 1  → data/plays_raw.parquet, data/plays.parquet
.venv/bin/python credits.py                # Stage 1b → data/track_credits.parquet
.venv/bin/python credits.py --review       #            eyeball what the feat./with regex extracted
.venv/bin/python enrich.py                 # Stage 2  → data/artist_tags.parquet (network, resumable)
.venv/bin/python enrich.py --limit 50      #            resolve at most N new artists
.venv/bin/python enrich.py --report        #            coverage stats only, no network
.venv/bin/python analyze.py                # Stage 3  → data/tag_trends.parquet, data/secondary/*
.venv/bin/streamlit run app.py --server.address 127.0.0.1   # Stage 4 dashboard
.venv/bin/python recommend.py --lambda 2.0 # Stage 5  → data/recommendations.parquet (network)
.venv/bin/python poll.py                   # Stage 6  → data/polled_plays.parquet (network, OAuth)
.venv/bin/python poll.py --status          #            local state only, no network
.venv/bin/python forecast.py --horizon 12  # Stage 7  → data/forecast.parquet, data/genre_gaps.parquet
.venv/bin/python report.py --open          # Stage 4b → output/report.html
```

Run 1 → 1b → 2 → 3 in order; 4–7 consume Stage 3's output. Stages 2, 5, 6 touch the network; the rest
are local and cheap to re-run. There is no test suite — each stage ends in a `report()` that prints
counts, coverage and sanity checks to stdout, and that output is the verification surface. Read it
before claiming a stage worked.

## Architecture

**Scripts, not a library.** Each stage is a standalone module with `main()`, argparse flags, and a
`report()`. Nothing imports another stage except `recommend.py`, which reuses `enrich.py`'s
`Throttled`, `normalise` and `load_genre_vocabulary` rather than growing a second copy.

**`config.py` is the only place paths and tuning constants are defined.** Every stage imports it.
Paths are overridable via same-named environment variables (`SPOTIFY_EXPORT_DIR`, `SPOTIFY_DATA_DIR`,
…) loaded from `.env`. Never hardcode a path or a threshold in a stage.

**Parquet is the interface between stages; DuckDB is the engine.** There is no persistent `.duckdb`
file — every stage opens an in-memory connection and registers the Parquet files it needs as views
(`analyze.register_sources`, `report.connect`, the `@st.cache_data` loaders in `app.py`). Consumers
must tolerate optional inputs being absent: `report.py` splits sources into required and optional and
gates sections on `has(con, view)`.

Prefer SQL in DuckDB over pandas wherever the two are equivalent — that is a deliberate project goal,
not incidental. Pandas appears only at the presentation boundary (figures, Streamlit, HTML tables).

**`tag_trends.parquet` is the contract.** Long format keyed by `(variant, tag, month)` carrying
`share`, `smoothed_share`, `tag_seconds`, `n_artists`, `slope`, `slope_pp_per_year`,
`rel_change_per_year`, `trend_class`. The `variant` column holds *both* credit weightings side by
side (`with_features`, `album_artist_only` from `config.CREDIT_VARIANTS`), so the dashboard toggles
attribution without recomputing anything. Every consumer must filter on `variant`.

**One figure module, two renderers.** Every chart is a function in `figures.py` returning a Plotly
figure and taking `mode="light"|"dark"`. `app.py` and `report.py` both import it. Do not write chart
code in either renderer.

## Invariants that break quietly if violated

- **Total sort order on every Parquet write.** All writes go through `ORDER BY ALL`
  (`ingest.write_parquet`, `analyze.write_outputs`). A partial sort key leaves ties for DuckDB's
  parallel sort to break arbitrarily and byte-identical re-runs silently stop holding.
- **Colour is anchored to the global genre ranking.** `figures.build_color_map(ranked_tags, mode,
  display_tags=...)` must receive the ranking over the *whole* dataset as `ranked_tags` and the
  currently-visible subset as `display_tags`. Passing the filtered list as `ranked_tags` repaints
  every remaining genre whenever one is filtered out.
- **Network stages are resumable via append-only JSONL in `.cache/`,** fsynced per record. A
  quarterly re-run must only spend requests on artists it has never seen. Do not add a step that
  rebuilds a cache from scratch.
- **MusicBrainz throttling.** `MB_MIN_INTERVAL = 1.1s` with a descriptive User-Agent, and a floored
  backoff on 503 — MusicBrainz sends `Retry-After: 0`, so trusting it means no backoff at all.
- **Non-finite values are refused, not written.** `analyze.assert_no_nan` raises before
  `write_outputs`. MusicBrainz tag counts go negative on downvotes; they are clamped at 0 in
  `build_tag_weights` because an artist tagged `[-1, -1]` sums to zero and the resulting NaN spreads
  through three months of the rolling mean.
- **Weights are normalised twice and neither creates nor destroys listening time.** Credit weight
  partitions by the *play's identity* (`variant, ts, spotify_track_uri`) — partitioning by artist
  hands every performer a full 1.0 and multiplies total time by the credit-list size.
- **Trend classification gates on absolute size before relative change** (`MIN_SHARE_FOR_TREND`), or
  a genre at a fraction of a percent posts "+540%/yr" off a 0.3pp move and swamps the rankings.
- **Poller reconciliation is a coverage cut, not a dedup.** `ingest.merge_polled` keeps polled rows
  only where `ts > max(export ts)`; the export is authoritative for everything it covers and
  retroactively replaces the poller's estimated `ms_played`. Matching on `(ts, track)` instead would
  leave stragglers whose timestamps drifted by a second.

## Privacy constraints

The raw export, every derived Parquet, `output/`, the cache and `.env` are gitignored; only code and
config are tracked. Before changing anything here, understand why it is the way it is:

- `.gitignore` blankets `*.json` to keep the export out, then re-includes tracked config **by name**
  (`!.claude/settings.json`). Adding a tracked JSON file requires a new explicit `!` line — never a
  broad pattern, or the export starts leaking back in.
- `ip_addr` (`config.DROPPED_FIELDS`) must not exist in any derived artifact. Stage 1 asserts this and
  prints a privacy check on every run.
- The Streamlit command includes `--server.address 127.0.0.1` deliberately: Streamlit otherwise binds
  every interface and advertises personal listening history on the LAN.
- `.claude/settings.json` denies reads of `.env` and `env`/`printenv`. Leave it in place.

## Gotchas

- `.env.example` states that `SPOTIFY_CLIENT_ID` is unused. That was true before Stage 6 — `poll.py`
  now requires it (Authorization Code + PKCE, no client secret, redirect URI exactly
  `http://127.0.0.1:3000`). The template is stale; the pipeline still runs with no `.env` at all
  unless you poll.
- Spotify's Web API is a dead end for enrichment and recommendations, and the code says so in
  several places: `/v1/artists/{id}` returns 200 with `genres` absent, batch endpoints and
  `related-artists`/`top-tracks`/`new-releases` return 403, `/v1/recommendations` returns 404. Genre
  data comes from MusicBrainz, candidates from ListenBrainz `similar-artists`. Do not "restore" a
  Spotify fallback without re-verifying the endpoints.
- The export's artist field is the **album** artist. `credits.py` recovers featured performers from
  track titles only — features living solely in Spotify metadata stay invisible. It is a floor, not a
  fix, and per-artist totals remain skewed.
- `ts` is UTC, so monthly buckets are UTC months.
