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
.venv/bin/python enrich.py --report        #            coverage + review list, no network
.venv/bin/python analyze.py                # Stage 3  → data/tag_trends.parquet, data/secondary/*
.venv/bin/streamlit run app.py --server.address 127.0.0.1   # Stage 4 dashboard
.venv/bin/python recommend.py --lambda 2.0 # Stage 5  → data/recommendations.parquet (network)
.venv/bin/python poll.py                   # Stage 6  → data/polled_plays.parquet (network, OAuth)
.venv/bin/python poll.py --status          #            local state only, no network
.venv/bin/python forecast.py --horizon 12  # Stage 7  → data/forecast.parquet, data/genre_gaps.parquet
.venv/bin/python report.py --open          # Stage 4b → output/report.html
.venv/bin/python playlists.py --dry-run    # Stage 8 preview — no Spotify writes
.venv/bin/python playlists.py              # Stage 8 → 4 playlists + data/playlists.parquet
.venv/bin/python consolidate.py --keep-whole "A" --filter "B"    # Stage 9 dry run
.venv/bin/python consolidate.py --keep-whole "A" --filter "B" --write   # Stage 9 → new playlist
.venv/bin/python running.py                # Stage 10 dry run (default)
.venv/bin/python running.py --write        # Stage 10 → 2 playlists + data/running_state.json
.venv/bin/python capture.py --source "NAME"          # Stage 11 dry run → data/capture.parquet
.venv/bin/python capture.py --source "NAME" --write  # Stage 11 → Fresh · Claude + data/capture_state.json
```

Run 1 → 1b → 2 → 3 in order; 4–8 consume Stage 3's output (Stage 8 also needs
Stage 5's and Stage 7's). Stage 9 is independent of the gap analysis — it reads
playlists a person built and needs only Stage 2's tags. Stage 10 needs 1b and 2 (plus `plays_raw`,
so a 20-second skip counts as heard) and is likewise independent of the gap analysis — it no longer
reads Stage 5 at all. Stage 11 needs no earlier stage: it reads Spotify, and `plays.parquet` only
for one line of its report. Stages 2, 5, 6, 8, 9, 10, 11 touch the network; the rest are local and
cheap to re-run.

Verification is split. Stages 1–7 are verified mainly by their `report()`, which prints counts,
coverage and sanity checks to stdout; read it before claiming a stage worked. Tests exist where a
report cannot see the failure: the Spotify-writing stages, the override/auth plumbing, and targeted
Stage 1b/2/3/5 cases (credit names, override cache, the export horizon, time conservation, seed
weights, answer-only caches). They are standalone scripts in `tests/`, no pytest, all offline (fakes
and synthetic DuckDB tables, never the network). Run them from the repo root — they
`sys.path.insert(0, ".")` and fail from anywhere else:

```bash
for t in tests/test_*.py; do .venv/bin/python "$t" || break; done   # each exits 1 on failure
```

For changes to the credit or enrichment SQL, the report surface is too coarse to trust alone: build a
throwaway DuckDB table of synthetic `plays` rows and assert on the output, and fake the `Throttled`
object rather than hitting MusicBrainz — `tests/` is the pattern to copy. Check the invariants that
fail silently — no listening time created or lost, no double-counted performer.

## Architecture

**Scripts, not a library.** Each stage is a standalone module with `main()`, argparse flags, and a
`report()`. Stages import each other only to reuse, never to copy: `recommend` ← `enrich`;
`forecast` ← `analyze`; `playlists` ← `credits` (`remix_credit`), `enrich`, `recommend`, `report`,
`poll`; `consolidate` ← `enrich`, `playlists`, `poll`; `running` ← `consolidate`, `credits` (the
remix regex and `remix_credit`), `enrich`, `playlists` (among others the credited Spotify search
`sp_artist_tracks_credited`, its `SP_TRACKS_CREDITED_CACHE` and `pin_artist_id`, which live in
`playlists` because Stage 8 filters on the same credits), `recommend`, `report`; `capture` ←
`consolidate` (`read_playlist`, whose `duration_ms` key Stage 11 depends on), `playlists`, `poll`;
`app` ← `recommend` (the λ dial). A second copy is how two definitions of one idea drift apart —
`playlists.register_song_key` registers `playlists._title_key` itself as a DuckDB function rather
than re-spelling it in SQL, and Stage 10 imports it; `credits.remix_credit` is Stage 1b's remix rule
in Python, and Stages 8 and 10 both import it.

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

**Five places accept a human answer, and all are files rather than code.** `artist_overrides.csv`
answers Stage 2's review list (name → MBID, `IGNORE` for things that were never artists, `NONE`
for a real artist MusicBrainz lacks); `playlist_overrides.csv` answers Stage 8's "which genres
deserve a playlist"; `consolidate_overrides.csv` answers Stage 9's review list
(`data/consolidate_review.csv` is machine output, regenerated every run — copy a row across, fill in
`keep` or `drop`, and it stops coming back); `running_overrides.csv` carries Stage 10's pins,
vetoes, `discover` rows (a scene act MusicBrainz never tagged, offered as new music in the one
playlist the row names) and `prefer` rows (a hand-built playlist whose members get a
`RUN_PREFER_MARGIN` near-tie boost — not a pin, and a veto still wins);
`.env` carries `SPOTIFY_CLIENT_ID` for the Spotify stages (6, 8, 9, 10). All are gitignored with a
tracked `*.example.*` alongside documenting the format. When adding another, follow that pattern rather than introducing
a config format.

`playlist_overrides.csv` exists because the gap ranking weights by seconds listened, and seconds are
dominated by workout listening, where music is functional rather than chosen. The export records no
activity type — `platform` is ~95% mobile either way and `reason_start` cannot separate a restless
desk session from a skip-heavy run — so this is genuinely unrecoverable from the data, not a
modelling gap to close later. Do not try to infer listening mode; ask.

**Credits have two sources and the better one wins per track.** `track_credits.credit_source` is
`poller` where Stage 6 has seen the track and supplied its true performer list, `export` where only
the album artist and the title regex were available. The poller's answer *replaces* the regex output
for that track rather than merging with it — merging would re-introduce the bad guess it exists to
correct.

## Invariants that break quietly if violated

- **Total sort order on every Parquet write, and one thread for float sums.** Writes go through
  `ORDER BY ALL` (`ingest.write_parquet`, `analyze.write_outputs`) or a key unique per row where
  reading order matters (`recommend`'s `score DESC, mbid`, Stage 2's review list on hours then name).
  A partial sort key leaves ties for DuckDB's parallel sort to break arbitrarily and byte-identical
  re-runs silently stop holding. `ORDER BY ALL` fixes rows, not bits: parallel aggregates add floats
  in whatever order the threads finish, and two identical Stage 3 runs differed in 70,638 of 89,080
  rows by up to 4.4e-16. Stages 1b, 3 and 7 run `SET threads = 1`; it costs seconds.
- **Colour is anchored to the global genre ranking.** `figures.build_color_map(ranked_tags, mode,
  display_tags=...)` must receive the ranking over the *whole* dataset as `ranked_tags` and the
  currently-visible subset as `display_tags`. Passing the filtered list as `ranked_tags` repaints
  every remaining genre whenever one is filtered out.
- **Network caches are append-only JSONL in `.cache/`, fsynced per record, and hold only real
  answers.** A quarterly re-run must only spend requests on keys it has never seen; do not add a step
  that rebuilds a cache from scratch. A real answer is an HTTP 200, stamped `"status": 200` — an
  empty 200 ("nobody is similar", "untagged", "Spotify does not carry them") is as final as a full
  one. A 503, 429, error envelope or unreadable body is not an answer and is not written, so the next
  run asks again: writing failures froze ~30 of 150 Stage 5 seeds as having no neighbours, and one 503
  on a backfill would have left REAPER genreless for good. A legacy record with an empty list and no
  status may have been a failure, so it is re-asked once and the new record wins on reload
  (`recommend.cached_answer`, Stage 8's search cache, Stage 10's durations). Stage 2's release-group
  backfill and Stage 9's release-group lookups likewise cache nothing on a failed request.
- **MusicBrainz throttling.** `MB_MIN_INTERVAL = 1.1s` with a descriptive User-Agent, and a floored
  backoff on 503 — MusicBrainz sends `Retry-After: 0`, so trusting it means no backoff at all.
- **Non-finite values are refused, not written.** `analyze.assert_no_nan` raises before
  `write_outputs`. MusicBrainz tag counts go negative on downvotes; they are clamped at 0 in
  `build_tag_weights` because an artist tagged `[-1, -1]` sums to zero and the resulting NaN spreads
  through three months of the rolling mean.
- **Weights are normalised twice and neither creates nor destroys listening time — enforced.**
  Credit weight partitions by the play's ROW (`row_number()` as `play_id` in
  `analyze.build_credit_weights`), not by `(ts, spotify_track_uri)`: 97 plays share that pair with a
  different play (ingest dedupes on every content column, and these differ in `ms_played`), and the
  old partition gave each half weight, losing 4.26 h. Partitioning by artist would hand every
  performer a full 1.0 and multiply time by the credit-list size. A play with no album-artist credit
  splits evenly under `album_artist_only` rather than vanishing as 0/0. `assert_time_conserved`
  compares credited seconds in (measured from `plays`, never from the weights) with seconds out, per
  variant, and refuses to write — shares are ratios, so time lost evenly moves no chart and only this
  catches it. `recommend.build_seeds` splits a play the same way.
- **Trend classification gates on absolute size before relative change** (`MIN_SHARE_FOR_TREND`), or
  a genre at a fraction of a percent posts "+540%/yr" off a 0.3pp move and swamps the rankings.
- **Poller reconciliation is a coverage cut, not a dedup — in rows and in time.** `ingest.merge_polled`
  keeps polled rows only where `ts > max(export ts)`; the export is authoritative for everything it
  covers and retroactively replaces the poller's estimated `ms_played`. Matching on `(ts, track)`
  instead would leave stragglers whose timestamps drifted by a second. The same cut applies to time:
  `config.ANALYSIS_HORIZON_SQL` is the export's last month, and every "last N months" window (Stage 5
  seeds, Stage 8 anchors, Stage 10's known pool) anchors on it, never on `max(month) FROM plays`.
  Stage 3 reads export rows only (`analyze.EXPORT_ONLY`), and the report and dashboard headlines count
  polled plays separately. 50 polled plays made a one-day September that became the
  highest-leverage point in every 12-month slope and flipped 10 of 11 trend classes. Polled rows
  still repair credits and count as Stage 10 hours, but are never completion evidence: a NULL
  `reason_end` reads as a skip, and it pulled Rain off its 0.846.
- **Stage 3's month axis is the calendar.** The grid comes from `generate_series(min, max)`, not
  `DISTINCT month`, and `month_idx` is calendar months since the first. An empty month is a row with
  a NULL share and a NULL smoothed share, which `avg` and `regr_slope` skip — neither read as zero
  listening in every genre, nor carried forward from the months before, nor collapsed so that the
  months either side become neighbours.
- **Credit repair is keyed on track URI, not on the play.** `credits.build_track_credits` takes
  `max(track_artists)` per URI, so one polled sighting fixes every play of that track including
  export rows years older. This is deliberate and has a cost: credits depend on poll state as well as
  the export, so historical shares move as polling accumulates. Narrowing it to polled rows only
  would freeze the export period on credits known to be wrong.
- **`track_artists` is joined on `0x1f`, never a printable delimiter.** `", "` splits
  "Tyler, The Creator" into two artists who do not exist; every printable separator eventually
  collides with a real name ("AC/DC", "Simon & Garfunkel"). `poll.ARTIST_SEP` is the one definition.
  Stage 10's credited lists are DuckDB `LIST`s and JSON arrays for the same reason.
- **The title regex must not cut a name the library knows.** `credits.build_protected_names` takes
  every album-artist and poller name `SPLIT_RE` would split ("Tyler, The Creator", "Chase & Status",
  "Tones And I") and lifts it out of a `feat.` blob whole, in the library's spelling, before the
  rest is split — Tyler alone had become four performers who do not exist. A name the library has
  never seen is still split. `SPLIT_RE` also splits on an inner `feat.`/`ft.` ("21 Savage ft.
  Project Pat" was one performer holding 5 h), and a capital ` X ` is NOT a separator: it cut
  "Ty Dolla $ign feat. X Ambassadors" and "Ambassadors" resolved to an unrelated act. Lowercase ` x `
  stays. The fragment junk filter (2–60 chars, an ASCII letter or digit) applies to regex output
  only; it used to drop the album artist `¥$`, leaving "CARNIVAL - HOOLIGANS VERSION" (11 h)
  credited to its remixer alone.
- **MusicBrainz tag coverage tracks fame, not your listening, and the gap is measurable.** In this
  library 100% of artists with 50h+ carry a genre tag, 94% at 10–50h, 86% at 2–10h, 77% at 0.5–2h and
  64% below that — 331 hours sit on artists that resolved perfectly and contribute nothing to any
  genre share. Small and independent acts are hit hardest, which is exactly where a listener notices.
  `artist_overrides.csv`'s optional `tags` column answers it: hand-supplied genres, applied by
  `enrich.apply_override_tags` AFTER the lookup and the release-group backfill so a human answer beats
  both. They REPLACE rather than merge (a hand answer exists because the looked-up one was absent or
  wrong), are never written to the cache so an edit takes effect on the next run, must be canonical
  MusicBrainz genres or they are refused with a warning, and all carry `OVERRIDE_TAG_COUNT` equally —
  stating genres is not casting votes. `artist_tags.source` is `override` for these.
- **A high resolution score is not proof of the right artist.** `Henrik` matched *Henrik Irgens*, a
  Danish multi-instrumentalist, at score 88 out of 8 candidates, then the release-group backfill hung
  nine wrong genres on him (ambient, art rock, downtempo, experimental, leftfield…). Mis-resolution is
  worse than non-resolution: it invents listening in genres the user has never played. When an artist
  looks wrong, check the *collaborators* — the right Henrik is the one credited beside GRAHAM on the
  album the user actually played. A name whose fold discards as much as it keeps (`¥$` → `s`, `MØ` →
  `m`, an all-Cyrillic name → `""`) is refused as ambiguous rather than exact-matched
  (`enrich.fold_discards_name`); it needs an MBID or `NONE`.
- **The override file outranks the resolution cache, and must keep doing so.** `enrich.py` purges
  cached override answers the CSV no longer backs (`purge_stale_overrides`) before applying it.
  Without that, deleting a row would leave its answer frozen in the append-only cache forever.
  `IGNORE` and `NONE` entries cost no request, so they are recomputed every run and never written to
  the cache — deleting the row lets the cached record show again, by design. MBID entries are cached
  and re-fetched only when the pinned MBID changes; the release-group backfill keeps such a record's
  `source = 'override'` and marks `tags_from = 'release-group'` instead, because rewriting `source`
  broke `override_satisfied` and re-fetched seven pins (~14 requests) on every run. `NONE` exists
  because a blank `mbid` cannot un-resolve: a tags-only row leaves resolution alone, so a wrong
  auto-match (PLAT. on the vaporwave ＰＬＡＴ, Unconscious Mind on a black-metal band) kept its MBID —
  and the MBID is what seeds ListenBrainz. `NONE` gives status `no_entry`, no MBID, and keeps the hand
  tags. Stage 9 honours `IGNORE` and `NONE` too. An override value that is not a UUID, `IGNORE`,
  `NONE` or blank is rejected with a warning — a typo'd MBID would otherwise be pinned as fact, which
  is exactly the guessing Stage 2 refuses to do.
- **`ignored` and `no_entry` are resolution statuses, not absences.** Both are answers the override
  file gave. `enrich.REVIEW_PREDICATE` is the one definition both review sites (`write_outputs` and
  `report`) use; a status missing from it brings the answered names straight back.
- **Stage 8 never deletes or unfollows a playlist.** It writes only to IDs in
  `data/playlist_state.json` or an exact `PLAYLIST_NAME_TEMPLATE` name match — exact including case,
  because a near-miss is somebody's hand-made playlist and a duplicate is the far cheaper mistake.
  Before every replace it snapshots current contents into `data/playlists.parquet`
  (`kind = 'pre_replace_snapshot'`), so nothing it overwrites goes unrecorded. The `Spotify` client
  deliberately has no `delete` method, and a test asserts that — keep it that way. Stage 10 shares
  `ensure_playlist` and may also pass exact legacy names (`config.RUN_PLAYLIST_LEGACY_NAMES`), tried
  only when nothing carries the current name, so lost state after a rename finds the old playlist
  instead of creating a second; Stage 8 passes none.
- **Spotify scopes only ever widen.** `poll.access_token(client_id, scope)` re-consents with the
  union of granted + needed, so running Stage 8 never strips the poller's scope or vice versa. Two
  paths would silently narrow it and are guarded: a failed refresh re-authorises with what was held
  rather than the default, and a refresh response that omits `scope` keeps the stored value instead
  of erasing it and re-prompting every run.
- **Stage 8 orders on Spotify relevance and filters on MusicBrainz tags; neither can be swapped for
  the other.** Spotify supplies popularity order (and the URI); MusicBrainz `arid AND tag:` supplies
  which recordings are on-genre. MusicBrainz's own ordering is Lucene relevance and is useless for
  ranking — ask it for Aphex Twin's techno and a SAW:II bootleg fragment comes first. `choose_tracks`
  applies the genre flag as a *stable* sort key so relevance survives inside each group.
- **Stage 8 anchors and strangers clear one bar: a share, then a floor.** `playlists.serving_sql`
  admits an artist when the spec's tags hold at least `ANCHOR_MIN_TAG_SHARE` (0.25) of its whole genre
  weight, AND one spec tag reaches `MIN_TAG_COUNT_FOR_ANCHOR`. Its `greatest(tag_count, 0)` clamps are
  load-bearing — `artist_tags` keeps negative counts (163 genre rows, to -6) — and a test pins them.
  Carrying a tag is not serving a genre: under "any spec tag", Halsey (indie pop 3 of 32) and Ellie
  Goulding (indie pop + indie folk 5 of 31) — pop, and heavily played — took four of six indie
  anchors. The floor keeps its old job: 0 means "nobody stands behind this", and REAPER anchored a
  metal playlist on `heavy metal` at 0. 0.25 keeps Metallica (heavy metal 42 of 164, outvoted by
  thrash metal) and costs The Killers (0.17) and The White Stripes (0.19) their indie rock anchors —
  known and accepted. Discovery candidates run through the SAME SQL, so a stranger never clears less
  than a library artist. That cut the garage and heavy metal stranger pools from 18 to 5 and 28 to 5.
  Mostly off-label (Röyksopp, Pendulum, Papa Roach, Deftones), but Megadeth (0.23, outvoted by thrash
  metal), Pantera (0.21, groove metal) and Rainbow (0.19, hard rock) went too — the split Metallica
  survives at 0.26. Do not loosen the bar to refill them — widen the spec (`heavy metal|thrash
  metal|speed metal|groove metal` brings back Megadeth, Pantera and Slayer) or fix Stage 5's supply.
  The bar judges against the spec as written and cannot repair an adjacent tag in it: with `electro
  house` in the garage spec, four of five garage strangers (Boys Noize, the classic false "speed
  garage") and The Chainsmokers' anchors qualify on electro or tech house alone.
- **A Stage 8 anchor is judged on the remixer its title names, else the album artist.**
  `credits.remix_credit` is Stage 1b's rule in Python — one definition, which Stage 10 imports too —
  and it reads the TITLE, never `credit_type`, because credits.py types every non-first poller artist
  as `featured`, remixers included. "Colors - Ian Asher Remix" anchored indie as a Halsey track and
  "I Was Made For Lovin' You - Disco Lines Remix" anchored heavy metal as KISS. A remixer with no genre
  tags leaves the track unable to anchor; falling back to the original artist would have put "Sweet
  Disposition - John Summit & Silver Panda Remix" in indie rock. That also catches Stage 1b's
  mis-parses, silently: "Robin Schulz Radio" (from `- Robin Schulz Radio Edit`), "A-Trak Remix Radio",
  "Eric Prydz Private", "Vocal", "TikTok", "2024 Remastered" have no tags, so those tracks cannot
  anchor at all. The fix belongs in Stage 1b, SQL and `remix_credit` together (strip trailing
  Radio/Club/Remix/Private tokens, widen `REMIX_FORMAT_STOPLIST`), never a Stage 8 fallback. The
  per-artist cap stays keyed on the album artist, as in Stage 10.
- **One song, one anchor, within a playlist and across the run.** Within a playlist the key is
  (album artist, `song_key`), so two pressings — Solo Kei's "cowboy killers" is two URIs — or an
  original and its remix do not both take anchor slots. Across playlists `select_run_anchors` carries
  (judge, `song_key`) forward and the first playlist in build order keeps the song; "cowboy killers"
  otherwise anchors indie AND indie rock. The judge is in the cross-playlist key so a remix anchoring
  garage does not keep its original out of indie — the line Stage 10's `Placements` draws.
- **A Stage 8 discovery track must be the candidate's own record: they LEAD it or remixed it.**
  `playlists.candidate_led` keeps a search hit only when the candidate's pinned Spotify id
  (`pin_artist_id`, so DEM2 never stands in for Dem 2) is the FIRST credit, or the title names them
  as remixer (`credits.remix_credit`) with that id on the record — Spotify bills a remix to the
  original artist, so the second clause is what keeps their remixes. A feature is not their record:
  RUNN's search returned "Free Fall", an ILLENIUM record featuring RUNN (credits ILLENIUM, RUNN), and
  it put ILLENIUM in the indie frontier under RUNN's name. `discovery_eligible` then drops live
  recordings and remixes a candidate leads but someone else made ("Alive - Trivecta Remix"). The
  lead can only be read from Spotify's credit list, which is why Stage 8 now searches through
  `sp_artist_tracks_credited` — **one search and one cache (`spotify_artist_tracks_credited.jsonl`)
  shared by Stages 8 and 10**, defined in `playlists.py`. Stage 8's first cache,
  `spotify_artist_tracks.jsonl`, kept only name and URI; it is retired, not migrated — nothing reads
  or writes it, and it must not be rebuilt. Stage 10's `gate_discovery` draws its own line (a drag
  lead refused, the candidate's own record required only for a genre match); do not unify the two.
- **A playlist spec's `tags` is a list, and every consumer must treat it as one.** `serving_sql` sums
  the spec's tags per artist, so an artist carrying three of them stays one artist rather than three
  copies of its listening time; `mb_genre_recordings` sends one Lucene `OR` rather than one request per
  tag, and sorts the tags into its cache key so `a|b` and `b|a` do not fork the cache.
- **`public: false` does not work, and Stage 8's playlists are link-readable.** Spotify accepts the
  field on create, reports `public: true` regardless, and a later `PUT {"public": false}` returns 200
  without changing it. They stay off the server-rendered public profile page, but any playlist is
  fetchable by direct link. Do not claim these playlists are private in docs or descriptions.
- **The export credits a remix to the ORIGINAL artist, and Stage 1b now parses it back.**
  `- X Remix` in the title is a performer credit Spotify files under the person being remixed:
  Halsey holds 134 min that is an Ian Asher speed-garage record, Chrystal holds 205 min that is a
  NOTION bassline record. 334 tracks / 5,810 min here, only 2.6% of listening but concentrated
  almost entirely in dance music. `credit_type = 'remixer'` carries the same weight as `featured`
  through `analyze.py`'s existing `ELSE feature_weight` branch, so no weighting change was needed
  and the no-time-created-or-destroyed invariant still holds. Two guards matter: `REMIX_FORMAT_STOPLIST`
  (`- Radio Edit` yields "Radio", which would otherwise become an artist holding 352 min), and
  `remaster` is NOT a remix type (it captured `"2012 -"` out of `War Pigs - 2012 - Remaster`).
  A suffix naming no person is still extracted, then fails to resolve and is answered `IGNORE` —
  junk degrades to a review row, never to bad data.
- **Stage 10 gates discovery candidates harder than library artists, on purpose.** A weighted share
  alone is exploitable by a sparse tag vector: one `tech house` vote and no drag tags is 1/1, and
  Boys Noize and Mr. Oizo were duly offered as speed garage. `RUN_MIN_CANDIDATE_CLUSTER_WEIGHT`
  applies an absolute floor to strangers only — waived when a NARROW tag carries them, since
  `bass house(1)` states something `tech house(1)` does not. It must never apply to library artists:
  hand-supplied tags all carry `OVERRIDE_TAG_COUNT = 1`, so a floor of 2 would exclude every artist
  answered by hand, John Summit's 36 hours included. The share bar is higher for strangers too:
  `RUN_MIN_CANDIDATE_SHARE = 0.85`, while library classification stays on
  `RUN_MIN_INTENSITY_SHARE = 0.60`, where listening has already vouched for the artist. At 0.60
  Netsky (drum and bass 10 vs liquid funk 3: 0.77), Rusko, Modestep and Basement Jaxx got in; 0.85
  refuses them and keeps Pendulum 0.97, Bassnectar 0.92 and NERO 0.89.
- **Stage 10 judges discovery per TRACK, on Spotify's own credits.** An artist clearing the bar says
  nothing about a given record: MJ Cole's relevance page led with Tion Wayne's rap single, and every
  hit used to be relabelled as the candidate. `gate_discovery` reads each pick against the credited
  cache (`spotify_artist_tracks_credited.jsonl`, shared with Stage 8, pinned to the candidate's own
  Spotify id, since `DEM2` and `Dem 2` fold alike) and refuses live tracks, anything over
  `RUN_MAX_DISCOVERY_MS` (4.5 min; known tracks exempt), a vetoed credit, anything already started
  (`plays_raw`, so a 20-second skip counts), and a drag LEAD or drag remixer. The drag test must
  stay on the lead and the named remixer only: a featured vocalist can carry drag by hand (Inéz,
  `melodic dubstep`), and library-artist supply passes through this gate — an unplayed Subtronics
  track must not be refused for its singer.
- **A merely adjacent tag poisons discovery far beyond the tracks it admits.** `electro house` was
  in `RUN_GARAGE_TAGS` and let in 43 artists (MSTRKRFT, Benny Benassi, Justice, Digitalism). Because
  Stage 10 seeds ListenBrainz on the cluster's *own top artists*, seeding on Justice and Tiësto
  returned Mr. Oizo, Boys Noize and Basement Jaxx as "speed garage". Removing one tag fixed the
  whole discovery pool. Check what a tag admits before adding it. `breakbeat` and `breakbeat
  hardcore` left both lists on the same reasoning (they carried The Prodigy into dubstep), and the
  cost landed on library artists — jigitz (5.9 h) now classifies to neither run and comes back only
  as pins. `liquid funk`, `big beat`, `future garage` and `psytrance` joined `RUN_DRAG_TAGS`, each
  named in `config.py` for the act that proved it.
- **Stage 10 seeds are one per artist, actually listened to, and normalised.** `cluster_seed_artists`
  joins a one-row-per-name view of `artist_tags` and groups on MBID: a join per TAG row counted Todd
  Edwards eighteen times, made him garage seed #17 on 0.54 h, and his ListenBrainz tail supplied 13 of
  21 garage discovery tracks. `RUN_MIN_SEED_HOURS` is an hour, not a share. Similarity is normalised
  per seed before pooling, since ListenBrainz scores share no scale (Skrillex tops out at 3955,
  REAPER at 181) and a raw sum handed the pool to the hub. An artist-wide veto removes the seed
  under every name sharing its MBID.
- **Stage 10 fills discovery from the listener's own artists first; strangers never hold the
  quota.** Supply order inside one budget (`build_selections`): hand-named `discover` acts → the
  cluster's own artists' never-started tracks, one each, up to `RUN_LIBRARY_DISCOVERY_FRACTION` of
  the budget → gated strangers → more of his own artists → known tracks scoring at least
  `RUN_TOPUP_MIN_SCORE`. SJ's call (2026-09-26): when new music runs short, fill with more of his own,
  never a weak stranger kept to hold the quota — once the seed fix cut Todd Edwards' chain, garage
  discovery fell from 21 tracks to ~9, and an unplayed Sammy Virji record was a keep where the
  strangers were not. The top-up floor means a playlist can come in under 240 min; the report prints
  the shortfall rather than filling with Matt Sassari's 0.186 "Full Vocal Mix". There is no Stage 5
  top-up: after the seed and share fixes its only garage contribution was Basement Jaxx.
- **Stage 10 never admits a known track on a featured-only export credit, and a remix goes to its
  remixer's run.** Admitting credits (`build_known_pool`) are the album artist, a remixer, or a
  feature the POLLER saw. An export feature is the title regex's guess, and that guess (two Daft Punk
  edits) is how Todd Edwards reached the seeds at all. Not a blanket feature ban: `credits.py` types
  every non-first poller artist as `featured`, remixers included, so a ban would erode remixer keeps
  as polling grows. When the title's remix credit names a credited cluster artist, the track goes to
  that artist's cluster only — "The One - NGHTMRE Remix" is NGHTMRE's dubstep, not Habstrakt's
  garage. A featured credit with no cluster (Inéz) neither admits nor refuses; the known side has no
  drag test.
- **Stage 10's label is identity, its title is display.** `running.CLUSTERS` carries both. The label
  (`speed garage`, `dubstep`) is `artist_clusters.cluster`, the `running_state.json` key, the
  archive's `gap_tag` and the override file's `playlist` value, and never changes; the title only
  names the Spotify playlist (`RUN_PLAYLIST_NAME_TEMPLATE` formats `{title}`). The garage run became
  "garage & house run · Claude" on 2026-09-26 because tech house and John Summit stay in it, and
  nothing keyed on the label moved. A rename changes `title` and adds the old name to
  `RUN_PLAYLIST_LEGACY_NAMES`; `publish` renames in place (same ID, URL, followers). Override rows
  may use either spelling and are stored under the label.
- **Vetoing one track promotes the next one by the same artist.** `RUN_TRACKS_PER_ARTIST` is a cap,
  so dropping a track frees a slot rather than shrinking the artist's presence. Dropping SLANDER's
  `Superhuman` pulled in `Back To U` and `GUD VIBRATIONS` and left THREE SLANDER tracks where there
  had been two. This silently stopped holding once the cap ran in SQL before any filter — a vetoed,
  live or already-placed track kept its artist's slot — and is true again because
  `running.eligible_known` applies veto, live and fresh BEFORE `cap_per_artist`, lazily, so each row
  is judged after the one before was placed. `test_running_selection.py` pins it. When the artist is
  the problem rather than the track, the veto must be artist-wide (blank `track_name`); to keep one
  track by an otherwise-vetoed artist, pin it as well, since `resolve_pins` runs before the veto
  filter and a pin is the more specific statement.
- **Stage 10 refuses live recordings, matched structurally.** Crowd noise and a tempo chosen on the
  night break a run, and none of it is visible to a genre tag — the track is correctly classified and
  still wrong. `RUN_LIVE_TITLE_RE` anchors on where a pressing note sits (` - live`, `(live`,
  `live at/from/in`, `unplugged`); a bare `live` substring would take Zeds Dead's and Dustycloud's
  `Alive`, both of which are in these playlists and both of which belong. `playlists.is_live` is the
  one definition and Stage 8 uses it too: its discovery offered Motörhead's "Bomber - … Live at The
  Whisky" as heavy metal. Stage 8's `discovery_eligible` also refuses a remix whose title names
  someone other than the candidate — RUNN's results carried "Alive - Trivecta Remix", a melodic
  dubstep record, into the indie frontier as RUNN's work.
- **A veto matches every credit — and on the known side the credits are not always what Spotify
  shows.** `running.vetoed` tests the row's artist and every name in `credited`, artist-wide and as
  (name, title). Known rows take `credited` from `track_credits` over every pressing (an artist-wide
  ILLENIUM veto used to leave Dillon Francis' "Don't Let Me Let Go", where he is only featured);
  discovery rows take Spotify's own `artists` list from the credited cache. A known credit exists
  only if the export, the title regex or the poller supplied it: `GUD VIBRATIONS` is an NGHTMRE &
  SLANDER record, but its credits carry NGHTMRE alone until a polled play repairs them, so today an
  artist-wide SLANDER veto does not touch it. It is pinned to dubstep deliberately — once polling
  credits SLANDER, the veto would take it, and the pin is the more specific statement.
- **Stage 10 must not use `playlists.assemble`, and the reason only appears at length.** `assemble`
  spaces anchors every `size // len(anchors)` slots — correct for Stage 8 (six anchors, twenty-five
  slots, step 4) and silently degenerate once anchors are the MAJORITY, where the step becomes 1 and
  every discovery track lands at the end. At 33 tracks nobody notices. At the 4-hour length, on a
  30-45 minute run, the listener never reaches the discovery half at all — the exact repetition the
  length exists to fix. `running.interleave` takes from whichever pool has consumed less of itself,
  so the ratio holds all the way down. Do not "unify" the two.
- **Stage 10 uses MEDIAN ms_played for duration, not max — known tracks and pins alike.** A completed
  play's `ms_played` is the track's length, but the odd play reports far more than the track runs.
  `max()` put SLANDER's "Wish I Could Forget" at 9.6 minutes and let one track eat a tenth of the
  playlist, and in `resolve_pins` it overcharged the garage pins 4.8 minutes. The median is over
  completed EXPORT plays; failing that, a polled `ms_played`, which the poller records as the whole
  track's length.
- **Dedupe on the folded title, not the URI — and Stage 10 keeps two keys.** Spotify presses the
  album cut, the single and the remaster as three distinct URIs, so URI-dedupe alone gives one
  artist's two slots to the same song — the first dry run produced "Papa Roach — Last Resort" twice.
  `playlists._title_key` (Stage 8, and discovery's heard-check) drops everything from the first
  ` - `, ` (` or ` [`, and a title that is *only* a suffix keeps its full form rather than folding to
  `""` and matching everything. Stage 10 adds `running.version_key`, which drops only pressing notes
  (`config.RUN_PRESSING_NOTE_RE`: remaster, radio/extended/original mix, feat./with, mono, explicit,
  clean) and keeps remixes, VIPs, flips and edits. `Placements` holds a version once across BOTH
  playlists and a song once per playlist, so a remix is its own record — a garage original no longer
  keeps its remix out of dubstep — and the known pool sums pressings into one row ("tell you
  straight" was two half-counted rows on 70 and 44 plays). The pattern runs in RE2 and Python alike,
  so it avoids lookaround, backreferences and `\d`/`\s`; a test holds the two to the same answers.
- **Stage 11 never writes its source.** `capture.py` reads the playlist SJ saves into (or Liked
  Songs) and writes only `Fresh · Claude`. A write to the dump would destroy what this stage orders
  on: a `uris` replace restamps `added_at` on every track. It refuses a `" · Claude"` source — the
  pipeline's own output read back as taste is circular — and refuses whenever the target could be a
  source: by name before anything is read, by the ID in `capture_state.json` once the sources are
  known, and by the ID `ensure_playlist` resolves, since a stale state file or a source renamed to
  the target's name mid-run would otherwise land the PUT on it. `tests/test_capture.py` asserts that
  no PUT or POST carries a source ID on any of those paths.
- **Liked Songs is read-only to the pipeline.** Only `user-library-read` is ever requested, and only
  when a source is `liked`, so a run on a named playlist never prompts. Do not add a save, not even a
  "no-op" one: the save endpoint takes no timestamp and re-saving a liked track re-stamps it to now,
  rewriting the date Fresh sorts on. The only write ever made to it was Step 0's backfill, outside
  the repo.
- **`capture.parquet` is a full snapshot, not an append-only cache.** A removed save disappears from
  it, and that is intended. The append-only rule protects answers that cost MusicBrainz requests;
  these dates live on Spotify and a re-read costs one request per 100 tracks (50 for Liked Songs).
  What was rendered survives as `fresh_selection` rows in `playlists.parquet`, and what was
  overwritten as `fresh_pre_replace_snapshot`, archived BEFORE the PUT rather than after it. That
  snapshot is read with `consolidate.read_playlist`, not `playlists.playlist_items`: the latter
  returns `[]` or the pages it got when a read fails, and the PUT would then overwrite what was
  never recorded. A failed snapshot read refuses the replace.

## Privacy constraints

The raw export, every derived Parquet, `output/`, the cache and `.env` are gitignored; only code and
config are tracked. Before changing anything here, understand why it is the way it is:

- `.gitignore` blankets `*.json` to keep the export out, then re-includes tracked config **by name**
  (`!.claude/settings.json`). Adding a tracked JSON file requires a new explicit `!` line — never a
  broad pattern, or the export starts leaking back in.
- `artist_overrides.csv` is gitignored: it is a list of artists the user listens to. The tracked
  template is `artist_overrides.example.csv`. Same split as `.env` / `.env.example`.
- The original build brief (`spotify-history-project-spec.md`) is gitignored and was untracked in
  `6950659`. It is still present in history before that commit; do not re-add it.
- `ip_addr` (`config.DROPPED_FIELDS`) must not exist in any derived artifact. Stage 1 asserts this and
  prints a privacy check on every run.
- The Streamlit command includes `--server.address 127.0.0.1` deliberately: Streamlit otherwise binds
  every interface and advertises personal listening history on the LAN.
- `.claude/settings.json` denies reads of `.env` and `env`/`printenv`. Leave it in place.

## Gotchas

- `SPOTIFY_CLIENT_ID` in `.env` is needed by `poll.py`, `playlists.py`, `consolidate.py`,
  `running.py` and `capture.py`, which share one developer app (Authorization Code + PKCE, no client secret, redirect URI exactly `http://127.0.0.1:3000`).
  Every other stage runs with no `.env` at all. `SPOTIFY_CLIENT_SECRET` is read by nothing — do not
  add a flow that wants one.
- **Spotify's February 2026 rename is why a 403 here may mean a dead endpoint, not a denied one.**
  The old paths were removed and now answer `403 Forbidden` rather than `404`, which is
  indistinguishable from a permissions failure until you try the new path. Verified live 2026-07-31:
  `/playlists/{id}/tracks` → 403 but `/playlists/{id}/items` → 200; `POST /users/{uid}/playlists` →
  403 but `POST /me/playlists` → 201. The body nesting moved too — a playlist's `tracks` object is
  `items`, and each row's `track` is `item`. `playlists.py` uses the current paths and documents the
  matrix at `FORBIDDEN_NOTE`; do not "restore" the old ones. From the same release: search `limit`
  maxes at 10 (hence `SP_SEARCH_LIMIT = 10`, not the once-documented 50) and tracks no longer carry
  `popularity` — which is why Stage 8 ranks on search relevance rather than that field.
- **Liked Songs kept its read path through the rename and lost its write paths.** Verified live
  2026-09-27: read is `GET /me/tracks?limit=50`, paged via `next` (`limit=51` → `400 "Invalid
  limit"`), and each row nests the track under **`track`**, not `item`, beside its `added_at`.
  `GET /me/tracks/contains` → 403 (dead); `GET /me/library/contains?uris=<comma list>` → 200, a list
  of booleans. Save is `PUT /me/library?uris=<comma list, max 40>` → 200 with an empty body;
  `PUT /me/tracks` → 403 (dead). The save takes **no timestamp**, and re-saving a track that is
  already liked **re-stamps** its `added_at` to now — the one-track no-op probe moved "My Home" to
  the top. The token now holds `playlist-read-private playlist-read-collaborative user-library-read
  user-library-modify playlist-modify-private playlist-modify-public user-read-recently-played`;
  `user-library-modify` is Step 0's and no stage requests it.
- **Liked Songs' dates are not save history for the backfilled tracks.** Step 0's one-off backfill
  (a scratchpad script outside the repo; plan `2026-09-25-driving-dump-playlist.md`) saved the 422
  Driving #2 tracks that were not already liked, oldest-first, one per request, so "Recently added"
  order is Driving #2's order. 367 were saved on 2026-09-27 before the quota lockout below, and the
  other 55 on 2026-09-28 after it lifted. The read-back found all 475 Driving #2 tracks liked (Liked
  Songs 3,167). None of the 422 carries its playlist date, and neither does "My Home", which the
  no-op probe re-stamped on 2026-09-27. So `capture.py --source liked` reports large same-day stamps
  (368 on 2026-09-27, 55 on 2026-09-28), and that is the backfill, not a wrong count: to verify it,
  count the Driving #2 URIs per stamp date, not every stamp. The other 52 keep their older dates.
  A union with the playlist source keeps the NEWEST `added_at` per URI — so each backfilled track
  takes its save date over its real playlist date, while a track liked before the backfill keeps an older date and ranks below all of them (bar
  "My Home"), however recently it joined the playlist. Until genuine hearts fill the newest 100,
  render from the playlist source alone.
- **The developer app has a daily request quota, and exhausting it locks every Spotify stage out for
  about a day.** On 2026-09-27 the Step 0 backfill hit `429 {"reason": "QUOTA_EXCEEDED"}` with
  `Retry-After: 85859` (≈ 24 h) after roughly 800 requests that day — a `contains` check before each
  of 367 saves doubled the count. It is not the ordinary rate limit: `Spotify._req`'s capped 30 s
  retry cannot outwait it, and every stage that touches Spotify (6, 8, 9, 10, 11) fails until it
  lifts. Budget bulk writes: use one read of the whole set instead of per-item checks, and never
  schedule the poller or a refresh to run beside a bulk job.
- **ListenBrainz's Popularity API is disabled server-side** (`500: "Popularity API currently disabled
  due to high load"` on `top-recordings-for-artist` and `top-release-groups-for-artist`; the batch
  `popularity/recording` route answers 200 with `total_listen_count: null` for everything). That is
  why Stage 8 orders on Spotify relevance rather than real listen counts. If it ever comes back,
  `choose_tracks` is where a real popularity signal would slot in.
- Spotify's Web API is a dead end for enrichment and recommendations, and the code says so in
  several places: `/v1/artists/{id}` returns 200 with `genres` absent, batch endpoints and
  `related-artists`/`top-tracks`/`new-releases` return 403, `/v1/recommendations` returns 404. Genre
  data comes from MusicBrainz, candidates from ListenBrainz `similar-artists`. Do not "restore" a
  Spotify fallback without re-verifying the endpoints.
- The export's artist field is the **album** artist. Two things partly repair it: the title regex in
  `credits.py` (a floor — features living solely in Spotify metadata stay invisible to it) and the
  poller's true track artists (exact, but only for tracks it has seen). Per-artist totals for
  never-polled tracks remain skewed.
- The poller first ran on 2026-09-25 and merged 50 plays, so the repair path is live but thin:
  `credit_source = 'poller'` covers 91 credit rows on 49 tracks, the rest is `export`. Do not read "it
  changed little" as "it does not work". The token's consent now holds `user-read-recently-played`
  alongside the playlist scopes — the union, as `missing_scopes` intends. Polled months are
  provisional until the next export and stay out of every trend (see the coverage-cut invariant).
- `ts` is UTC, so monthly buckets are UTC months.
