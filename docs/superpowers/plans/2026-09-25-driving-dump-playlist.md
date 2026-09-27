# Driving #2 — The Dump Playlist Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Phases are gated. Do not start a phase until its gate is met.

**Goal:** Make what SJ saves playable newest-first, without the pipeline ever writing to the playlist he saves into. Measure which playlists actually get played. Hand recent saves to Stage 10 as a discovery supply — but through the Stage 10 fixes plan, not here.

**Diagnosis: Driving #2 is a capture log, not a playlist anyone plays.** Four facts explain why it goes unused.

- **Its music is heard, just not from it.**
  - Its tracks carried 66.5 h of 2026 listening, 17.7% of 375.8 h (export rows only), and still draw 7–12 h a month.
  - Of the last 50 plays, 9 were Driving #2 tracks and **none were played from Driving #2**: 7 came from Spotify-generated mixes and 2 from Workout · Claude. The mixes supplied 33 of the 50.
  - 242 of its 475 tracks were played *before* being added, so adding a track works like a heart: a save, not a queue.
- **In order, it opens on the stalest and least-chosen music.**
  - 116 of the first 117 positions come from four bulk stamps on 2025-01-23/24. The largest is a 74-track copy of Electronic/Instrumental.
  - Only 2 of 475 dates are out of order, so position is age.
  - Positions 0–299 average **1.7** plays each in 2026; positions 400–474 average **10**.
- **On shuffle it is a 28.2 h genre salad, and SJ does shuffle it some of the time.**
  - The newest 100 tracks hold 57% of its 2026 plays but would get 21% of shuffle picks (100/475).
  - Inside detected dump sessions (4+ consecutive Driving #2 plays, gaps ≤15 min), shuffle was on for about 60% of plays in 2025 and somewhere between a quarter and 40% in 2026. The 2025 figure is robust; the 2026 one rests on 80–160 plays and moves with the session heuristic (the synthesis got 41% on 79 plays; a re-derivation with the same rule got 26% on 163). Across all 2026 listening the figure is 24%.
- **Use collapsed while the saving habit faded.**
  - Dump sessions fell by an order of magnitude from the first half of 2025 to 2026 (synthesis: 131 → 26 → 13 sessions across launch–Jul 2025 / Aug–Dec 2025 / 2026; re-derivation: 173 → 30 → 29). The collapse is robust, the exact counts are not.
  - Adds: ~25 a month in Feb–Dec 2025, 6 a month in 2026.
  - Driving #1 went the same way and was frozen at no cost: 291 of its tracks not in Driving #2 still drew 45.8 h in 2026.
- **Nothing measures any of this.** `poll.to_rows` stores `context.type`, which is "playlist" for all 50 polled rows, and not the playlist's URI.

Net: capturing works. What's missing is a **bounded, newest-first way to play what was captured**. And no stage reads the one explicit, dated "I chose this" signal SJ produces.

**A data caveat that applies to every count in this plan.** The poller's 50 September rows are already merged into `plays.parquet` (`source_kind = 'poll'`, 2026-09-24/25), so `max(month) FROM plays` is now 2026-09 on the strength of 50 plays with estimated durations and no `reason_end`. Anything anchored on `max(month)` — Stage 10's window, Stage 8's anchors, Stage 5's seeds, the latest month of `tag_trends` — has moved two months forward on that evidence. Every count above uses export rows only (`source_kind = 'audio'`, through 2026-07-26). Any play-count floor this plan or the Stage 10 plan adds must do the same.

**Architecture:** Driving #2 is read-only to the pipeline, permanently.
- **Stage 6** starts recording which playlist each play came from.
- **Stage 11 (`capture.py`)** is a small new stage. It reads the capture source — an exact-named playlist, and later Liked Songs — into `data/capture.parquet`. With `--write` it refreshes one pipeline-owned playlist, `Fresh · Claude`, through Stage 8's lifecycle, unchanged.
- **Liked Songs** may later become the archive and the capture point. Step 0's backfill ran on 2026-09-27, but the save endpoint takes no timestamp, so the backfilled hearts carry that day's date, not their history. Nothing in Phases 1–2 depends on it.
- **Stage 10** later reads `capture.parquet` as an optional input, on the discovery side only — specified in the Stage 10 fixes plan, not here.

**Tech Stack:** Python 3.12 in `.venv`, DuckDB over Parquet, the shared PKCE token via `consolidate.gentle_token`, and `playlists.Spotify`, which has no delete verb. No new dependencies.

## Decisions this plan takes (SJ can overturn any of them in Open questions)

- **The pipeline never writes to Driving #2 or Driving #1.** No reorder, prune, dedupe or rename. Every benefit a write would buy comes from a read plus a pipeline-owned playlist instead.
- **The capture source is a CLI choice (`--source`, repeatable), not tracked config.** This follows Stage 9's reason: personal playlist names stay out of `config.py`.
- **Phase 2 does not wait on Liked Songs.** It reads Driving #2 by exact name, which needs no new consent. `liked` as a source is a follow-on behind `user-library-read`.
- **Recent saves feed Stage 10 discovery, never the known-side score.** Driving #2 membership doesn't predict run fit. On the dubstep known side its members score 3 keep / 0 weak / 2 cut, against Workout · Claude members at 26 / 9 / 0 (evaluation RC6). Taste and run fit are different signals: D1 carries run fit, this carries taste. The mechanism lives in the Stage 10 fixes plan.
- **No new human-answer file.** Everything here is answered by an existing file (`running_overrides.csv`, `artist_overrides.csv`) or a CLI flag.

## Global Constraints

- Every Python invocation uses `.venv/bin/python`.
- DuckDB SQL over pandas wherever the two are equivalent. Every Parquet write goes through `ORDER BY ALL`.
- **Never delete or unfollow.** `Fresh · Claude` is written only by state-file ID or exact-name match, and is snapshotted into `data/playlists.parquet` before every replace. `capture.py` refuses to run if its write target could be its source.
- **Liked Songs is read-only to the pipeline.** The only write to it is Step 0, a one-off scratchpad script outside the repo, run once with SJ present and never built into a stage.
- Scopes only widen, through `poll.access_token`'s union. Library scopes are requested only by the code paths that need them: a `capture.py` run on a named playlist must never prompt for `user-library-read`.
- Use Spotify's post-Feb-2026 paths (`/me/playlists`, `/playlists/{id}/items`, `item` not `track`). Liked Songs, verified by Step 0 on 2026-09-27: read `GET /me/tracks?limit=50`, nesting **`track`** (not `item`); save `PUT /me/library?uris=` (max 40). `GET /me/tracks/contains` and `PUT /me/tracks` answer 403: a dead path answers 403, not 404.
- `public: false` doesn't stick, so descriptions must not call anything private.
- No playlist IDs in tracked files. `data/*.json` state files are already gitignored by the `*.json` blanket.
- Branch: `driving-dump` for Phases 1–2. Phase 3 belongs to the Stage 10 fixes plan and its branch. PRs need SJ's approval; plain merges don't.
- Tests are plain-assertion scripts in `tests/`, run from the repo root: `for t in tests/test_*.py; do .venv/bin/python "$t" || break; done`. Every Spotify-writing path gets an offline test on a recording fake before it touches the real API.

## What touches a hand-made playlist

| Step | Reads Driving #2 | Writes Driving #2 | Other writes |
|---|---|---|---|
| 0. Liked Songs backfill (**done 2026-09-27**) | yes | **no** | Liked Songs: additive saves, oldest-first (timestamps not honoured) — scratchpad script, SJ present |
| 1.1–1.2 habits | n/a | no | SJ's own hearts, if he chooses them |
| 1.3 poller context | no | no | `data/polled_plays.parquet` |
| 1.4 Stage 10 pins | no | no | `running_overrides.csv` (local) |
| 2. `capture.py` | with `--source "Driving #2"` | **no, refused in code** | `Fresh · Claude` (pipeline-owned), `data/capture.parquet` |
| 3. Stage 10 capture supply | via `capture.parquet` | no | run playlists (pipeline-owned) — see the Stage 10 fixes plan |
| Not built: reorder in place | n/a | **would** | opt-in spec recorded below, not recommended |

---

### Step 0 (DONE 2026-09-27): Liked Songs backfill — nothing else waits on it

**Status 2026-09-27: done, dates not preserved.** SJ consented and the backfill ran. The save endpoint takes no timestamp, so the fallback below applied: 422 tracks saved oldest-first, one per request, every one stamped 2026-09-27. The result block has the details; the final count awaits a read-back.

*History:* on 2026-09-26 the consent prompt (`poll.authorize` waits 180 s for the redirect) timed out before SJ approved it, and nothing was written that day.

**What it will do, when SJ says go.** A throwaway scratchpad script, outside the repo, with SJ present:
1. Widen the token by the union rule (`poll.access_token(client_id, "user-library-read user-library-modify")`). The playlist and recently-played scopes stay.
2. Read Liked Songs in full and record what is already there.
3. For every Driving #2 track not already liked, save it, **backdated to the track's playlist `added_at`** if the save endpoint honours timestamps. If it does not, save **oldest-first** in `added_at` order so "Recently added" still reads as save order.
4. Additive only. No write to Driving #2. No removal from anything.

**What must be verified first, before any write:**
- **The live library paths.** The Feb-2026 rename removed the old playlist paths and they now answer 403, indistinguishable from a permissions failure. Probe the read path with the read scope only (`GET` on the current library-tracks path, one page, `limit=50`), confirm the page size and whether the nesting key is `item` or `track`, and record it. Only then try the save path on **one** track SJ already likes (a no-op if it is already liked).
- **Whether timestamps are honoured.** Save one Driving #2 track with a backdated timestamp, read it back, compare `added_at` to the second. If it comes back stamped today, fall back to oldest-first for the bulk run and record that "Recently added" order is then insertion order, not history.
- **Nothing here assumes the pre-rename `PUT /me/tracks` body shape survived.** Write down what worked in the result block and in `CLAUDE.md` Gotchas next to the Feb-2026 matrix.

**Files:**
- Create: `<scratchpad>/backfill_liked.py` and `<scratchpad>/verify_liked.py` (throwaway, not committed)
- Modify: this plan file (fill in the result block)
- Modify: `CLAUDE.md` Gotchas: the verified Liked Songs paths, page size and nesting key, and the scopes the token now holds

- [x] **Step 1: SJ consents.** Only after he says so. Run the probes above with the read scope first, then the one-track write probe.
- [x] **Step 2: Backfill**, additive, backdated where honoured. (Not honoured: saved oldest-first instead.)
- [ ] **Step 3: Read back and compare.** For each Driving #2 URI, check it is present and that the liked `added_at` equals the playlist `added_at` to the second. Record the token's `scope` field only, never the tokens. (Pending: the orchestrator verifies the 422.)
- [x] **Step 4: Record the result**

```
LIKED SONGS BACKFILL (Step 0) — verified 2026-09-27

  read path / page size / nesting key : GET /me/tracks / 50 (51 -> 400 "Invalid limit") / 'track'
  contains                            : GET /me/library/contains?uris= -> 200 [bool]
                                        (GET /me/tracks/contains -> 403, dead)
  save path / timestamps honoured     : PUT /me/library?uris= (max 40) -> 200, empty body
                                        (PUT /me/tracks -> 403, dead) / NOT honoured
  Liked Songs before                  : 2,745   range 2019-07-31 -> 2026-05-20
  Driving #2 URIs present after       : 53 already liked + 422 saved (verify by read-back) / 475
  added_at preserved (to the s)       : 0 of the 422 — all stamped 2026-09-27
  token scopes now                    : playlist-read-private playlist-read-collaborative
                                        user-library-read user-library-modify
                                        playlist-modify-private playlist-modify-public
                                        user-read-recently-played
  decision                            : fallback — 422 saved oldest-first in playlist added_at
                                        order, one per request, so "Recently added" order =
                                        Driving #2 order; the dates are the backfill's, not history
  side effect                         : the one-track no-op probe RE-STAMPED "My Home" to
                                        2026-09-27 — re-saving an already-liked track restamps it
```

**`added_at` was not preserved** (all 422 stamped 2026-09-27). Saving oldest-first kept "Recently added" in Driving #2's order, so playing Liked Songs newest-first still works; the dates themselves are the backfill's. `capture.py --source liked` therefore sees 422 adds on one day, and in a union with `--source "Driving #2"` the newest-`added_at` rule gives every backfilled track that date over its real one, so anything liked before the backfill ranks below all 422. Render from the playlist source alone until genuine hearts fill the newest 100 (recorded in `CLAUDE.md` Gotchas). Re-saving **does** restamp an existing heart (the "My Home" probe), and the save takes no timestamp, so remove-and-re-save cannot restore history dates either (see Q8).

**Side effect SJ accepts by consenting:** Liked Songs feeds Spotify's generated mixes, and those mixes supplied 33 of the last 50 plays. 475 new hearts, most of them a year or more old, will steer the mixes toward the whole dump rather than its newest quarter. If the mixes drift, Step 0 is the first suspect. This is a reason to decide Q1 before Step 0, not after.

**Recurrence: no.** Nothing in the pipeline depends on Liked Songs being complete.
- If SJ saves with the heart (Q1), Liked Songs *is* the capture point and there is nothing left to sync.
- If he keeps adding to Driving #2, `capture.py --source "Driving #2"` reads it directly and Step 0 is optional cosmetics.

---

## Phase 1 — No-code habits, pins, and measurement (gate: none)

### Task 1.1: Where to save from now on (no code, SJ's call: Q1)

Two workable answers. Both keep the pipeline out of Driving #2.

- **Keep adding to Driving #2** (recommended *for now*). Needs no consent, no backfill; Phase 2 reads it by name. Its cost is the one SJ already pays: an ever-longer playlist. Freezing it later costs nothing, as Driving #1 showed.
- **Switch to the heart.** One tap, lands at the top of Liked Songs, never forces a "Driving #3". But without Step 0 it starves `Fresh · Claude`: at 6 adds a month, Liked Songs alone takes about 16 months to reach 100 tracks. So the heart implies Step 0 (which has now run, 2026-09-27, without preserving dates). `--source` is repeatable precisely so a later switch works without a backfill: `--source "Driving #2" --source liked` unions the two and dedupes by URI on the newest `added_at`.

### Task 1.2: Listen newest-first without building anything (no code)

Open Driving #2 and set **Sort → Recently added**. It is a per-device view setting and does not reorder the playlist. (Once Step 0 has run, Liked Songs in its default "Recently added" order does the same.)

| Newest N | Hours | Share of Driving #2's 2026 plays | Reaches back to |
|---|---|---|---|
| 50 | 2.5 | 25% | 2026-03 |
| 75 | 3.8 | 47% | 2025-09 |
| 100 | 5.0 | 57% | 2025-07 |
| 150 | 7.9 | 65% | 2025-06 |

The gain flattens after 75–100. **Limit:** shuffle ignores sort order, and SJ shuffles the dump between a quarter and half the time. Phase 2 covers that case.

### Task 1.3: Poller records the context URI (S)

This is the measurement every later gate depends on. It also settles the evaluation's §4 "in order or on shuffle" and D8 for the run playlists.

**Files:**
- Modify: `poll.py` (`to_rows`, `store`, `status`)
- Create: `tests/test_poll_context.py`
- Modify: `CLAUDE.md` Gotchas:
  - "The poller has never been run" is stale: 50 rows exist, they are merged into `plays.parquet`, and `credit_source` is no longer 100% `export` for the tracks they touch.
  - `data/polled_plays.parquet` is the only home of `context_uri` and must never be pruned. `ingest.merge_polled` deliberately doesn't carry the column into `plays_raw`.

**Interfaces:**
- `to_rows`: each row gains `"context_uri": (it.get("context") or {}).get("uri")`. `reason_start` keeps `context.type`.
- `store`: once the existing file is loaded, `ALTER TABLE polled ADD COLUMN IF NOT EXISTS context_uri VARCHAR`. Then change the positional `INSERT INTO polled SELECT * FROM incoming` to `INSERT INTO polled BY NAME SELECT * REPLACE (CAST(context_uri AS VARCHAR) AS context_uri) FROM incoming`.
  - The positional insert fails on the wider schema.
  - The cast is required, not defensive: a pandas column that is all `None` arrives in DuckDB typed **INTEGER** (that is why `polled_plays.parquet` already carries `reason_end INTEGER` and `shuffle INTEGER`). Verified offline 2026-09-26 on throwaway tables; the `BY NAME` + `REPLACE` form works and the dedupe below keeps the non-null context.
  - Apply the same cast on the first-ever `CREATE` path, so a fresh file never gets a non-VARCHAR `context_uri`.
  - Order the dedupe window `ORDER BY context_uri NULLS LAST` so a re-fetched play keeps its context deterministically.
- `status`: add a "plays by context, last 30 days" block, read from `polled_plays.parquet` only (never from `plays`, whose polled tail is a coverage cut). Labels, in order:
  - IDs in `playlist_state.json`, `running_state.json` and (after Phase 2) `capture_state.json`, shown by name;
  - `…:collection` as Liked Songs;
  - `spotify:playlist:37i9dQZF1…` as "Spotify-generated (unreadable)";
  - anything else as its raw ID with a count.

  When the column doesn't exist yet, print `context: not recorded yet`. No network.
- `ingest.merge_polled`: **no change.** Verified 2026-09-26: it lists every column by name in its `INSERT INTO plays_raw BY NAME SELECT …`, so an extra column in `polled_plays.parquet` is simply not selected.

- [ ] **Step 1: Write the failing test** (`tests/test_poll_context.py`). It points `poll.POLLED_PARQUET` at a temp file, never the real one, and checks:
  1. rows stored by the old `to_rows` shape (no `context_uri`) create an old-schema file;
  2. new rows append, old rows read `NULL`, new rows keep their URI;
  3. a batch with every context null appends without a type error, and the column is still VARCHAR afterwards;
  4. re-storing the same `(ts, uri)` is still counted as a dupe, and the non-null context wins;
  5. the row count in equals the row count out, with nothing lost.
- [ ] **Step 2: Run it and confirm it fails.** Expect `KeyError: 'context_uri'` or a column-count mismatch from `store`.
- [ ] **Step 3: Implement** the three changes above.
- [ ] **Step 4: Verify.**
  - The test passes, and `tests/test_scope_auth.py` still passes.
  - `.venv/bin/python poll.py --status` prints the new block with no auth prompt.
  - With SJ present: `poll.py` once, then `--status` shows real contexts.
- [ ] **Step 5: Schedule (SJ's call: Q3).** Use launchd every 3 h while awake, running `poll.py` unchanged. In 2026 the median day had 45 plays and 80 of 201 days exceeded the 50-item page, so a daily poll loses plays on about 40% of days. A scheduled poll makes the polled tail of `plays.parquet` grow until the next export replaces it; that is `merge_polled` working as designed, but it is also why the data caveat above matters.
- [ ] **Step 6: Commit** on `driving-dump`.

### Task 1.4: Put hot captures on the runs with pins (no code; SJ's call: Q4)

Some saved tracks are heavily played yet sit in no live playlist. Stage 10 can't reach them today:
- Four of the five John Summit tracks saved on 2026-04-24 (WITH ME 28, SHADOWS 27, SHADES OF BLUE 27, ALL THE TIME 20 plays in 2026, per pressing; LIGHTS GO OUT has 1) are in no other playlist. He already holds 5 tracks in the garage run through an override, against a cap of `RUN_TRACKS_PER_ARTIST = 3`.
- Łaszewo/AVELLO's `headrush` (27 plays, done-rate 0.74) is invisible to the garage cluster because the export credits Łaszewo alone.

A `keep` row in `running_overrides.csv` places a track ahead of the cap. The artist is the export's album artist and the title is the full title, which `resolve_pins` matches against history:

```
speed garage,keep,Łaszewo,headrush,AVELLO co-credit missing from the export; saved 2026-03
```

**Cross-references.**
- SJ answered evaluation **D2** on 2026-09-26: keep tech house, rename the garage run. The hold on the John Summit pins is lifted. Pinning all four takes him from 5 to 9 tracks in one playlist; that is a taste call, not a data one.
- Pins run before vetoes, so pinning a SLANDER track overrides the artist-wide veto (**D5**).
- The Stage 10 fixes plan (`docs/superpowers/plans/2026-09-26-stage10-fixes.md`) owns everything else about the runs, including SJ's other answers of 2026-09-26 (shuffle; fill short discovery with his own tracks; code fixes over stopgaps). If it changes how pins work, it wins.
- **Verify:** `running.py` (dry run) lists the pins under "pinned", with no "pinned track not found" warnings.

---

## Phase 2 — `Fresh · Claude`: the bounded newest-first pool (gate: SJ says yes to Q2 — met 2026-09-27)

Phase 2 is the one build that fixes the shuffle case. When SJ plays in order it gives the same result as Task 1.2, and it's playable anywhere a playlist is. It reads Driving #2 by name and needs no consent SJ has not already given.

### Task 2.1: Stage 11 `capture.py`: read the source, render the pool (S–M)

**Files:**
- Create: `capture.py` (`main`, argparse, `report()`)
- Modify: `consolidate.read_playlist`: add `duration_ms` to the `fields` string and to the returned dict. It is additive, and Stage 9 ignores it. **No existing test covers `read_playlist`** (`test_consolidate.py` tests scoring and dedupe only), so `test_capture.py` adds one.
- Modify: `config.py`. Add a Stage 11 block after Stage 10:
  ```python
  # --- Stage 11: capture — the dump, read-only ---------------------------------
  # Whatever SJ saves into (a playlist named on the command line, or Liked
  # Songs) is READ here and never written. Fresh · Claude is the pipeline-owned
  # rendering.
  CAPTURE_PARQUET = DATA_DIR / "capture.parquet"
  CAPTURE_STATE_JSON = DATA_DIR / "capture_state.json"   # "fresh" -> id; sources
  FRESH_SIZE = 100           # one PUT; the newest 100 held 57% of the dump's 2026 plays
  FRESH_PLAYLIST_NAME = "Fresh · Claude"
  FRESH_PLAYLIST_DESCRIPTION_TEMPLATE = (
      "The {n} newest tracks you saved, newest first ({hours:.1f} h). "
      "Built by spotify-trend-analysis · refreshed {date}")
  ```
- Create: `tests/test_capture.py`
- Modify: `tests/test_scope_auth.py`: one case checking that adding `user-library-read` to a token holding `user-read-recently-played playlist-modify-private` misses only `user-library-read`.
- Modify: `CLAUDE.md` (commands block; the invariants below) and `README.md` (command-table row, a short Stage 11 section).

**Interfaces:**
- `read_source(sp, source: str) -> tuple[dict, list[dict]]`:
  - Anything other than `liked` goes through `consolidate.find_playlist`, which requires an exact name and errors on a missing or duplicated one, then `consolidate.read_playlist`.
  - `liked` calls `read_liked(sp)`, which pages Liked Songs using the path and nesting key Step 0 verified on 2026-09-27: `GET /me/tracks?limit=50` via `next`, each row under `track`. It refuses (rather than reading an empty library) when a page's rows carry no `track` key, and when a page fails part-way.
  - It refuses a source whose name ends in `" · Claude"`, because reading the pipeline's own output as capture is circular.
  - Row shape: `spotify_track_uri, track_name, track_artists` (joined on `poll.ARTIST_SEP`), `duration_ms, added_at, source, source_rank` (0 = newest in source order).
  - It skips rows without a URI (local and unavailable tracks) and non-track items.
- `--source` is repeatable. Rows from several sources are unioned and deduped by URI, keeping the row with the newest `added_at`. Default: none — the flag is required, so nothing is read by accident.
- `write_capture(con, rows)`: writes `data/capture.parquet` as a full snapshot with `ORDER BY ALL`. This is a rendering of the current source, not a cache. The dates live on Spotify, and a re-read costs one request per 100 tracks, so the append-only-cache rule doesn't apply. What was actually rendered is preserved anyway, in `playlists.parquet`'s `fresh_selection` rows.
- `select_fresh(con, n) -> list[dict]`: `ORDER BY added_at DESC, source_rank, spotify_track_uri LIMIT n`, a total order (the bulk stamps tie on `added_at`; `source_rank` breaks them).
- `publish(sp, con, meta, tracks)`: modelled on `running.publish` for one playlist.
  - `playlists.ensure_playlist(sp, "fresh", config.FRESH_PLAYLIST_NAME, state)`, **then refuse if `pid` is any source ID**. Also refuse up front if `FRESH_PLAYLIST_NAME` equals any source name.
  - `playlists.playlist_items` goes to archive rows `kind='fresh_pre_replace_snapshot'`, then one `PUT /playlists/{id}/items`, then the description `PUT`. Archive rows `kind='fresh_selection'`, `gap_tag='fresh'`, via `playlists.write_archive`.
  - It saves `{"fresh": {...}, "sources": [{kind, id, name}, ...]}` to `CAPTURE_STATE_JSON`, so the poller's `--status` can label both.
- Scopes, through `consolidate.gentle_token`:
  - Read: `playlist-read-private playlist-read-collaborative`, plus `user-library-read` **only when a source is `liked`**. A run on a named playlist must never prompt.
  - Write: add `playlist-modify-private playlist-modify-public`, mirroring `running.SCOPES_WRITE`. Spotify reports these playlists as public, so writes need both.
- CLI:
  - `capture.py --source "<exact name>" [--source liked] [--size N]` is a dry run. It writes `capture.parquet` and prints the would-be playlist, with **no Spotify writes and no state or archive writes**.
  - `--size` is capped at 100 and refused above it: one `PUT` replaces the whole playlist and nothing here needs the chunked `POST` path.
  - `--write` refreshes the playlist.

- [x] **Step 1: Write the failing test** (`tests/test_capture.py`). It uses a fake modelled on `tests/test_playlist_lifecycle.py`'s `FakeSp` that records every verb. It checks:
  1. newest-first order with the `source_rank` tie-break, and the `FRESH_SIZE` cap;
  2. `consolidate.read_playlist` returns `duration_ms`, follows pagination, and skips a null `item`;
  3. two sources dedupe by URI on the newest `added_at`, and the row shape is identical across sources;
  4. a dry run issues zero non-GET verbs;
  5. **no PUT or POST ever targets a source ID**, including when the exact-name fallback resolves to a source (which must raise `SystemExit`);
  6. a `" · Claude"` source is refused, and `--size 101` is refused;
  7. the snapshot GET of the target precedes its PUT;
  8. `hasattr(playlists.Spotify, "delete")` is `False`.
- [x] **Step 2: Run it and confirm it fails** (`ModuleNotFoundError: capture`).
- [x] **Step 3: Implement.** Branch `capture`, 2026-09-27. Beyond the spec: `consolidate.read_playlist` also refuses a page that never answers (it used to return the pages before it as the whole playlist); the pre-replace snapshot is archived before the PUT, not after; a `--write` re-reads every source afterwards and prints whether its URIs and `added_at` are unchanged (Step 6's check); an empty selection never blanks the target.
- [x] **Step 4: Verify offline.** The full suite passes (18 files).
- [ ] **Step 5: Dry run against the real source.** `report()` must show:
  - each source's kind, name and row count;
  - the `added_at` range, and adds per month for the last 6 months;
  - the selection's size, hours and newest/oldest `added_at`, and how many selected tracks have ≥1 export play.

  Expected for Driving #2: 100 tracks, about 5.0 h, running back to 2025-07-15.
- [ ] **Step 6: Live run (SJ present), then idempotency.**
  - `capture.py --source "Driving #2" --write` creates `Fresh · Claude`.
  - A second `--write` reuses the ID and adds the first `fresh_pre_replace_snapshot` rows.
  - The source's track count and `added_at` values are unchanged, which the report prints.
- [ ] **Step 7: Docs, then commit and merge** (no PR without approval). Docs and commits done on `capture`; merge pending.

**Invariants to add to CLAUDE.md:**
- **Stage 11 never writes its source.** It refuses when its target ID or exact name could be a source, and a test asserts that.
- **Liked Songs is read-only to the pipeline.**
- **`capture.parquet` is a full snapshot, not an append-only cache.** A removed save disappears from it, and that is intended.

**Refresh:** by hand at first. Adds arrive at about 6 a month, so weekly is plenty. After two clean manual runs it can join the poller's launchd job (Q3), run sequentially after `poll.py` in the same job so the two never race on the token file. It's idempotent and snapshots before every replace.

---

## Phase 3 — Recent saves as Stage 10's discovery supply (owned by the Stage 10 fixes plan)

The evaluation found garage discovery nearly empty (**RC3**): 14 of 20 seeds have empty ListenBrainz lists, and the right scene acts have zero MusicBrainz tags. Recent saves are an SJ-chosen, dated supply in exactly those genres, and SJ's 2026-09-26 answer — when new music runs short, fill with more of his own tracks — points the same way. The supply is modest: about 3 garage-relevant tracks and up to about 8 low-play saves across both clusters (planner estimates, not re-run).

**This plan does not specify the mechanism.** It is Stage 10 work and belongs with C1, C2 and C5 in `docs/superpowers/plans/2026-09-26-stage10-fixes.md`, which is being written now. What this plan commits to is the input and the constraints the input carries:

- **Input:** `data/capture.parquet` from Phase 2 — URI, title, Spotify `track_artists` (primary first), `duration_ms`, `added_at`, source. Stage 10 registers it as an **optional** view and runs exactly as before when it is absent. Duration comes from it, so no `/tracks` request is made.
- **Discovery side only**, never a known-side factor, and not merged with D1's Workout · Claude boost into one "chosen by SJ" multiplier. The evidence in Decisions says they measure different things.
- **Any play-count floor counts export rows only** (`source_kind = 'audio'`). Polled rows are estimated, capped at 50, and carry no `reason_end`.
- **The veto always wins**, and a track the done-rate floor rejected can never come back through this route.
- **Primary-artist or remixer admission, never featured-only** — evaluation RC4/C10; Daft Punk's "Fragments of Time" reached garage through Todd Edwards that way.

**Gate:** Phase 2's `capture.parquet` exists, and the Stage 10 fixes plan has landed C1, C2 and C5. If that plan chooses a different supply (C5's `discover` rows alone, say), this phase is dropped and `capture.parquet` stays what Phase 2 made it.

---

## Not being built, and why

- **Any write to Driving #2: reorder, prune, dedupe, rename.**
  - Prune needs a delete verb that `playlists.Spotify` deliberately lacks, and a test asserts it.
  - A `uris` replace restamps `added_at` on all 475 tracks, destroying the only dated record of taste. Step 0 and Phase 3 both depend on it.
  - An in-place newest-first reorder is possible in principle. **If SJ overrides this, the only acceptable form is:**
    - a `--reorder-source` flag plus the source ID pinned by hand;
    - a snapshot first;
    - moves only, through `range_start`/`insert_before`, never `uris`;
    - every `added_at` checked unchanged afterwards;
    - all of it proven on a throwaway playlist first.

    Even then it overwrites his own manual reorders and buys nothing Task 1.2 and Phase 2 don't already give.
- **A recurring Liked Songs sync.** See Step 0.
- **An inbox triage stage with genre lanes and `inbox_overrides.csv`.**
  - It needs five lanes and leaves 81 review rows over 70 artists.
  - Tag lanes blur: only 60–69% of workout and house tracks match their own lane's centroid.
  - Most lanes have no destination SJ plays. The one that does (workout into the runs) is served by Phase 3, using Stage 10's own clusters.
- **Genre or mood shelves** (house & melodic, indie & pop, club house, festival bass) **and a rotating "rediscover" list.**
  - Seven `· Claude` playlists exist, and only Workout · Claude shows in recent plays.
  - The shelf genres are small in current listening. French house is 0.37%; indie pop is 0.77% and falling.
  - The rediscover pool is 65 tracks, so it repeats after about three refreshes, and Spotify's mixes already resurface old saves.
  - **Revisit when:** four or more weeks of Task 1.3 data show `Fresh · Claude` is actually played, and SJ names when he would put a split on.
- **Appending into Electric workout.** That is a write to a hand-made playlist, SJ's most-played one.
  - It would break the invariant that "`· Claude` = managed, everything else untouched".
  - A wrong tag-based append lands in the playlist he uses most.
  - He already files into it by hand: 15 Driving #2 tracks reached it after capture, a median of about a month later (another 26 were already there or added the same day).
- **Filing into the dormant purpose playlists** (Chill Music, background, Car bangers, Chill drive, Daily). Their last adds were in 2022–24, and tracks found only in them drew 0–0.11 plays each in 2026. Driving #2's own tracks drew 2.2.
- **A skip-magnet filter in `Fresh · Claude`.** Only 1 of the newest 100 has ≥5 plays and is finished under 35% of the time.
- **A snapshot of all hand-made playlists plus a `playlist_modes.csv` answer file, and the consumers built on it:**
  - a Stage 3/7 adds timeline: only 52 tagged Driving #2 adds in 12 months, and 2.6% out-of-sample gain;
  - a Stage 8 "chosen frontier" review file;
  - a Stage 5 add-recency blend: 10 of the 20 recent-add artists it would seed have empty ListenBrainz lists.

  All of that means a sixth answer file and a network stage for a thin signal. **Revisit** once `capture.parquet` has a year of saves.
- **Spotify artist arrays as a third credit source for Stage 1b.** This one is real: about 890 owned-playlist tracks carrying about 890 h list an artist missing from `track_credits` (re-derived 2026-09-26; the planner had 860 / 852 h). But it moves historical shares pipeline-wide and interacts with evaluation D5, C3 and C10. It deserves its own plan. `capture.parquet` already carries the arrays for every saved track, so it's a ready input.
- **Resolving never-played saved artists through Stage 2.** 98 of Driving #2's 433 credited artists (23%) have never reached `artist_resolution`, so Phase 3 cannot place them. The evaluation's C5 `discover` rows are the hand answer. A Stage 2 change to read `capture.parquet` waits on evidence that it matters.
- **Automatic "Driving #3" rotation, merging Driving #1 and #2, or triaging Driving #1.**
  - Rotation has happened once, after a taste shift (rap 57% down to 10%), not because of size.
  - The two dumps share only 17 tracks.
  - Liked Songs, once backfilled, makes starting a new dump free.
- **Inferring driving or workout mode from plays, and anything needing BPM.** CLAUDE.md forbids the first; the data for the second doesn't exist.

## Found along the way (out of scope; fix separately)

- **Stage 5 seed fan-out bug** (`recommend.build_seeds`, `recommend.py:138-155`), confirmed 2026-09-26.
  - It sums `track_credits.played_seconds`, which is each (track, artist)'s **all-time** total, once per windowed play, because the join is `track_credits JOIN plays USING (spotify_track_uri)` with the window on `plays`. A seed's weight therefore scales with all-time seconds × recent play count.
  - It has the same shape as evaluation RC1's Stage 10 seed bug.
  - Fix: sum `p.played_seconds`, split per play by credit, partitioned on `(ts, spotify_track_uri)` as `analyze.py` does. Size XS.
  - Verify with Stage 5's report, then re-read Stage 10's top-up, which consumes `recommendations.parquet`.
- **CLAUDE.md drift:**
  - "Nothing imports another stage except `recommend.py`" is stale: `consolidate.py` and `running.py` import `playlists`, and `running.py` imports `consolidate`.
  - "The poller has never been run" and "`credit_source` is 100% `export` today" are stale (Task 1.3).

## Open questions for SJ

1. **From now on, keep adding to Driving #2, or heart?** *Recommend Driving #2 for now.* It needs no consent and Phase 2 reads it directly. The heart is the better long-term capture point but only once Step 0 has run, and Step 0 waits on consent you have deferred. `--source` is repeatable so the switch costs nothing later.
2. **(Answered 2026-09-27: yes — build Phase 2.)** **When you put the dump on, is it in order or shuffle, and would you reach for a 100-track newest-first playlist?** The data says both: about 60% shuffle in 2025 dump sessions, a quarter to 40% in 2026 (on few plays). *Recommend building Phase 2 at 100 tracks* (5.0 h, 57% of the dump's 2026 plays). 75 tracks (3.8 h, 47%) is the tighter alternative. If the answer is "I'd just use Sort → Recently added", Phase 2 is not worth building.
3. **Can `poll.py` run on launchd every 3 h while you're awake, and later refresh `Fresh · Claude` in the same job?** *Recommend yes to the poll now, and to the refresh after two clean manual runs.* Without a schedule, no gate in this plan can be judged.
4. **Pin hot saves onto the runs?** *Recommend pinning `headrush` to the garage run now.* The four John Summit tracks are your call: D2 is decided in their favour, but pinning all four puts 9 of his tracks in one playlist.
5. **(Answered 2026-09-27: consented; Step 0 ran.)** **When you're ready, consent to `user-library-read` (and `user-library-modify` if Step 0 goes ahead)?** Nothing in Phases 1–2 needs it. It unlocks `--source liked` and the backfill. The backfill will steer Spotify's mixes toward the whole dump; decide Q1 first.
6. **Heart the 291 Driving #1 tracks you still played in 2026 (45.8 h)?** *Recommend no.* Driving #1 already is that archive, and hearting rap-era tracks may steer the mixes, which deliver most of your listening, back toward rap.
7. **Should the pipeline ever reorder Driving #2 in place?** *Recommend no.* See "Not being built" for the only form it could take.
8. **Only if Step 0 finds dates were not preserved:** remove and re-save the 475 hearts to restore dates? *Recommend yes, but done by you or by hand, outside the pipeline.* It removes items from your library, and the pipeline has no delete verb. **Update 2026-09-27: dates were not preserved, and this repair cannot work** — the save takes no timestamp, so a re-save stamps the day it runs again. The oldest-first order is the most the API allows.

(Recent saves feeding Stage 10 discovery — the former Q5 — is now a question for the Stage 10 fixes plan.)

## Provenance and self-review

- **Planners, and what was kept:**
  - *Minimal:* kept the core. Heart capture, Liked Songs as the archive, newest-first with no code, context URI, pins over code, a conditional newest-N playlist, and no rotation. Driving #1 hearting became Q6.
  - *Listenable:* kept the newest-N playlist, merged into `Fresh · Claude`; the dump-session shuffle evidence, which answers minimal's pivotal question in favour of building it; and the target ≠ source guard. Dropped rediscover, mood splits, the skip filter, the known-side boost and the reorder.
  - *Inbox:* kept its diagnosis (dormant purpose playlists, freezing costs nothing, hand-filing into Electric workout), primary-artist-plus-remixer admission, and "no processed marker on the playlist". Dropped the triage stage, lanes, shelves, the Electric workout append and the new answer file.
  - *Taste signal:* kept discovery-side-only Stage 10 use and its evidence against a known-side boost, the usage report, and the Stage 5 bug, which went to "found along the way". Dropped `adds.py`, `playlist_modes.csv`, the Stage 3/7/8 consumers and the Stage 5 blend. The credit third source was deferred to its own plan.
- **Numbers re-derived for this plan** (synthesis, 2026-09-25): position buckets, newest-N shares, 2026 hours share, bulk stamps and inversions; shuffle rates and dump sessions; John Summit and `headrush` play counts; plays per day and Driving #1's 2026 hours; the `build_seeds` bug. Lane consistency, the Stage 10 supply estimate, the rediscover pool and the dubstep 3/0/2 split come from the planners' scratch work and weren't re-run.
- **Fable review, 2026-09-26** (read-only, against `scratchpad/driving/*`, `data/*.parquet` and the code): every number above re-derived; all matched except as now stated in the text (monthly hours 7–12 not 8–12; dump-session counts and the 2026 shuffle share are heuristic-dependent; Electric workout 15 not 16; the credit-source count ~890 not 860; unresolved saved artists 23%). Step 0 corrected from "done" to pending. `merge_polled`'s named-column insert and the Task 1.3 DuckDB statements verified offline. The claim that `test_consolidate.py` guards `read_playlist` was false and is replaced by a test. Phase 3 handed to the Stage 10 fixes plan.
- **Hand-made playlist check:** no step writes Driving #1 or #2. The only write to SJ's library is Step 0's additive backfill, run once on 2026-09-27, outside the repo.
