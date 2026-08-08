# Stage 10 — running playlists

Design agreed 2026-08-07.

## The ask

A playlist for runs. The existing hand-made `Electric workout` (262 tracks,
13.7 h) works for lifting but sags on runs: it carries material that drops the
pace. Wanted: mostly music already listened to, curated down and modestly
expanded, leaning speed garage and dubstep, capped at roughly 1.5–2 hours. Two
playlists, so they can be compared on real runs.

## What the investigation found

Four findings shaped the design. All were measured, not assumed.

**1. There is no BPM, and BPM is the wrong signal anyway.** Spotify's
`/audio-features` (single *and* batch) and `/audio-analysis` all answer 403;
`/tracks` answers 200, so this is removal, not a permissions failure — the same
February 2026 rename that took `popularity` off tracks. Deezer's ISRC lookup
matched 40/40 sampled tracks but carried a real BPM on 5 of them; the rest
return `bpm: 0`. And tempo would mislead even if free: Zomboy's *Nuclear (Hands
Up)* measures 87 BPM because its drums are half-time, and it is exactly the kind
of track that carries a run. What is being modelled is **sustained intensity**.

**2. An include-list of genres is not sufficient.** ILLENIUM carries
`dubstep(3)` and `trap edm(3)`, so any include-list admits him — while
`melodic dubstep(3)` and `future bass(2)` are why the playlist sags.

**3. 118 artists on `Electric workout` carry no MusicBrainz genre tags at all,
and the tags that exist are wrong at the top.** REAPER is the most-represented
artist (15 tracks) and carries `heavy metal` at count 0. `Insania`, played for
69 minutes, resolved at score 100 to a **Swedish power metal band** — the
documented `Henrik` mis-resolution failure mode, live in the data.

**4. The export credits a remix to the ORIGINAL artist, and this is
load-bearing.** 334 tracks / 5,810 min carry a `- X Remix` suffix. Only 2.6% of
listening overall, but concentrated exactly in the target genres:

| minutes | export credits | actually by |
|---|---|---|
| 204.9 | Chrystal | NOTION |
| 134.6 | Halsey | Ian Asher |
| 123.5 | Disco Lines | AVELLO |
| 112.5 | John Summit | GRiZ |
| 108.9 | Tate McRae | Ian Asher |
| 98.2 | John Summit | Subtronics |
| 87.7 | Phantogram | Subtronics |

Roughly 570 minutes of garage/bassline production filed under pop artists, which
a genre filter then discards as drag. Not fixable by better artist tagging: the
remixer's name never reaches the data.

## Decisions

| Question | Decision |
|---|---|
| Deliverable | New Stage 10, `running.py` |
| Known vs new | ~⅔ known, ~⅓ discovery |
| A/B axis | Cluster: speed garage (steady) vs dubstep (aggressive) |
| Source pool | Whole listening history, not just workout playlists |
| Intensity signal | Weighted tag share + completion rate; no BPM |
| Untagged artists | Researched by hand into `artist_overrides.csv`, no review CSV |
| Remixer credits | Fixed in `credits.py` (Stage 1b), globally |
| Length | Fill to `RUN_TARGET_MINUTES = 105` by duration, not track count |

## Design

### Stage 1b change

New `credit_type = 'remixer'`. Regex
`\s-\s(.+?)\s(remix|edit|bootleg|flip|vip|rework|refix|mix|dub|version)$`, plus:

- `REMIX_FORMAT_STOPLIST` — `Radio Edit` yields "Radio", which is not a person
  and would hold 352 min of real listening. 42 tracks / 883 min rejected.
- `remaster` excluded as a type — a remaster has no remixer, and including it
  captured `"2012 -"` from `War Pigs - 2012 - Remaster`.
- Captures that are numeric, empty, or span another ` - ` are refused.
- Deduped against the album artist (self-remixes) and globally per
  `(track, artist)`.

**Fail-safe:** a suffix naming no person (`- BLM REMIX`) is still extracted,
then fails to resolve in MusicBrainz and is answered `IGNORE`. Junk degrades to
a review row, never to bad data. Refusing anything unrecognised would drop real
remixers, which is the more expensive mistake.

`analyze.py` needs no change: `CASE credit_type WHEN 'album_artist' THEN 1.0
ELSE feature_weight END` already routes `remixer` to the 0.5 feature weight, and
normalisation still partitions by play identity, so **no listening time is
created or destroyed**.

### Classification

Three tag families in `config.py`, every tag validated against the MusicBrainz
genre vocabulary (`riddim`, `uk bass`, `bass music`, `4x4 garage`, `acoustic`
are **not** MusicBrainz genres and were removed — they could never match).

Membership is a weighted share, not a set test:

```
cluster_share = cluster_weight / (cluster_weight + drag_weight)
```

with `RUN_MIN_INTENSITY_SHARE = 0.60`. ILLENIUM lands at 0.545 and is excluded.
Weights are clamped tag counts, so a zero-count tag classifies nobody (REAPER's
`heavy metal`). An artist qualifying for both clusters goes to the heavier one
(Knock2: `bass house(4)` beats `hybrid trap(2)`).

Bare `trap` is in **neither** list — it names both rap-trap and EDM-trap, so it
can neither qualify nor disqualify. Same rule as `dance` in Stage 9.

### Selection

- **Known half** — tracks from the 18-month window, joined through
  `track_credits` so album artist, feature *and remixer* all count. Ranked by
  `hours × Laplace-smoothed completion rate`. Tracks with ≥3 plays and a
  completion rate below `RUN_MIN_TRACKDONE_RATE` are dropped; a single skipped
  play is not treated as evidence. Duration comes free from the longest
  `trackdone` play.
- **Discovery half** — Stage 8's machinery unchanged: `select_candidates` →
  `mb_genre_recordings` (recording-level genre truth) → `sp_artist_tracks`
  (Spotify relevance order) → `choose_tracks`. Durations cost one request each;
  batch `/tracks` answers 403.
- **Assembly** — `playlists.assemble` interleaves so it does not read as two
  lists stapled together.

**Acknowledged limitation:** tags are per *artist*. Per-track MBID resolution is
the explosion this project has twice declined and is not reopened. Mixed
catalogues are handled by completion rate (track-level) and by vetoes; the
discovery half *is* genre-filtered per recording.

### `running_overrides.csv`

Fifth override file, same pattern as the other four: gitignored, tracked
`.example.csv`. Columns `playlist,decision,artist_name,track_name,note`.
Pins match the **full** title — `_title_key` folds at the first ` - `, and this
library holds both Insania's `iloveitiloveitiloveit - Garage` (23 plays) and
Bella Kay's `iloveitiloveitiloveit` (4 plays).

### Safety

Every Stage 8 rule inherited: never delete, never unfollow, no `delete` verb on
the client, write only to IDs in `data/running_state.json` or an exact name
match, snapshot contents to `data/playlists.parquet` before any replace
(`kind='run_pre_replace_snapshot'`). Descriptions must not claim the playlists
are private — `public: false` does not work and any playlist is link-readable.

### Verification

`tests/test_remix_credits.py` (15 assertions) and
`tests/test_running_selection.py` (26) — standalone, offline, pinning the real
cases that break naive rules.
