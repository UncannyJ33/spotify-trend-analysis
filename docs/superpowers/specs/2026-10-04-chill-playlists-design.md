# Chill playlists: seeded and excluded Stage 8 specs — design

Date: 2026-10-04 · Status: approved in conversation, awaiting written-spec review

## Intent

SJ wants to listen to more chill indie, vapor soul, vaporwave and "whatever Joji is", and has said
plainly that this goes against the trend. The gap ranking is not the judge here; `playlist_overrides.csv`
is, as it already is for every Stage 8 playlist. He is also dropping the speed garage frontier: he
already hears enough garage, and he is deleting the Spotify playlist himself.

**Success:** after one `playlists.py` run SJ has 7 frontier playlists. Three are the ones he kept
(heavy metal, indie, indie rock) and four are new (chill indie, vapor soul, vaporwave, lo-fi). Each
new playlist is filled mostly with music he has not heard, actually in the genre, with no Drake in
vapor soul. Stage 8 never writes to the speed garage playlist again.

## What stops Stage 8 today (measured 2026-10-04)

1. **Cap.** `config.N_PLAYLISTS = 4`, documented as "hard cap agreed with the user". SJ raised it to 7.
2. **Supply.** Discovery candidates come only from Stage 5's 358 recommendations, which are seeded on
   all listening and dominated by workout music. Before the 0.25 share bar, the candidates carrying
   any spec tag number: chill indie ≤51, vapor soul ≤28, lo-fi ≤25, vaporwave ≤8. The bar cuts all of
   these further. Vaporwave has ~0 library hours, so it would have neither anchors nor candidates.
3. **Hijack.** In this library `alternative r&b` (80 h) is mostly Drake (111 h), Post Malone, NAV and
   Tory Lanez. They would anchor vapor soul.

## Decisions taken

- **Lineup (build order matters: the first playlist to claim a song keeps it as an anchor):**

  | label | tags | seeds | exclude |
  |---|---|---|---|
  | heavy metal | heavy metal\|thrash metal\|speed metal\|groove metal | | |
  | indie | indie pop\|indie folk\|folk pop | | |
  | indie rock | indie rock | | |
  | chill indie | bedroom pop\|dream pop\|indie folk\|ambient pop | Men I Trust\|Still Woozy\|Clairo\|Dayglow | |
  | vapor soul | neo soul\|alternative r&b\|soul | Frank Ocean\|Daniel Caesar\|Brent Faiyaz\|Masego\|FKJ | Drake\|Post Malone\|NAV\|Tory Lanez |
  | vaporwave | vaporwave\|chillwave\|synthwave\|hypnagogic pop | Tycho\|Washed Out\|Toro y Moi\|Macintosh Plus | |
  | lo-fi | lo-fi hip hop\|trip hop | Joji\|Nujabes\|keshi | |

  The speed garage row is deleted. "lo-fi" is Joji's genre: MusicBrainz tags him `lo-fi hip hop` 3,
  ahead of alternative r&b, art pop and trip hop. Chill indie leaves out `indie pop` because the kept
  indie playlist already covers it. `indie folk` sits in both playlists; cross-playlist anchor dedupe
  (`select_run_anchors`) keeps any one song in a single playlist.
- **Supply comes from hand-named seeds (approach A).** Auto-seeding from SJ's top artists in each genre
  was rejected: vaporwave has none, and vapor soul's top seeds would be Drake. Re-running Stage 5 was
  rejected too, because its seeds are the whole taste vector.
- **Cap:** `N_PLAYLISTS = 7`.

## Design

### 1. The override file gains two optional columns

`playlist_overrides.csv` columns: `label,tags,seeds,exclude`. Both new columns are pipe-separated and
optional. A file without them parses exactly as today, and `tests/test_playlist_overrides.py` keeps
passing unchanged. `load_playlist_specs` adds `spec["seeds"]` and `spec["exclude"]` (lists, `[]` when
blank). `playlist_overrides.example.csv` documents both columns, following the existing comment style.
The real file is gitignored and the implementer edits it to the lineup above.

A seed is either an artist name or a MusicBrainz UUID. A UUID is how SJ answers a name that refuses to
resolve. This is the same convention as `artist_overrides.csv`.

### 2. Seed resolution never guesses

For each seed name, in order:

1. `artist_tags.parquet`: a library artist already has its MBID.
2. Stage 2's resolution cache (`enrich.load_cache`).
3. `enrich.resolve_via_musicbrainz`, appending the answer to Stage 2's cache the way
   `consolidate.resolve_missing` does.

If the result is not an exact resolution with an MBID, Stage 8 prints a warning naming the seed and
saying "put its MBID in the seeds column", then skips it. It never takes the top search hit, because
the Henrik invariant applies: mis-resolution is worse than non-resolution.

### 3. Seeded candidates join the pool ahead of Stage 5's

A new `seed_candidates(con, http, spec, caches)` in `playlists.py`:

- For each resolved seed, call `recommend.fetch_similar`. It uses the same cache as Stage 5, and a
  failed request is never cached.
- **Normalise similarity per seed** (divide by that seed's max) before pooling by sum. ListenBrainz
  scores share no scale, and this is the rule Stage 10 already learned.
- A seed SJ has not heard is itself a candidate, ranked first. A seed in the library is not a
  candidate, since library artists are anchors and never discovery.
- Drop library artists (the same `known` set `select_candidates` uses) and every `exclude` name.
- Take the top `config.PLAYLIST_SEED_CANDIDATES` (new, 40) by pooled score. Fetch each one's genres
  with `recommend.fetch_candidate_tags`, which uses the same cache.
- Pass them through **the same `serving_sql` bar** (0.25 share, `MIN_TAG_COUNT_FOR_ANCHOR` floor) that
  every stranger clears. A seed's neighbour gets no lower bar for being near a seed.

`build_selections` then iterates seeded candidates first and Stage 5's `select_candidates` after,
deduped on MBID. The existing loop is otherwise untouched: per-candidate Spotify search,
`candidate_led`, `discovery_eligible`, `mb_genre_recordings`, `choose_tracks`, and the stop at
`PLAYLIST_SIZE`.

### 4. Exclude is artist-wide for that playlist only

An excluded name, compared through `enrich.normalise`:

- never anchors: it is checked against the anchor's judge (remixer or album artist) and against the
  album artist;
- is never a candidate, from seeds or from Stage 5;
- removes any discovery track whose Spotify credit list (from the credited cache) names it, so a Drake
  feature on someone else's record is out too.

It does not touch any other playlist, and it is not a Stage 10 veto.

### 5. Speed garage

Deleting the row is the whole change, because Stage 8 builds only the playlists that specs name. Its
`playlist_state.json` entry stays and is harmless: nothing reads a label that has no spec. Stage 10's
garage & house run is a separate stage and is unaffected.

### 6. Cost

First run: roughly 16 ListenBrainz requests (one per seed), up to 40 MusicBrainz tag lookups per new
playlist (≤160 at 1.1 s, ≈3 min), and a few MusicBrainz resolutions for unheard seeds. The Spotify
search loop is unchanged, stops at `PLAYLIST_SIZE` per playlist, and fits well inside the daily quota.
Later runs ask only about keys never seen before.

## Testing

All offline, in the `tests/` pattern (standalone script, fakes, synthetic DuckDB, no network). New
`tests/test_playlist_seeds.py`:

- the CSV parses with and without the new columns; blank fields become `[]`;
- a seed resolves through library → cache → MB, and an unresolved or ambiguous seed is skipped with a
  warning rather than guessed. A UUID seed is used as given;
- per-seed normalisation: a hub seed scoring 4000 does not drown one scoring 180;
- an exclude name never anchors, never becomes a candidate, and drops a discovery track that credits
  it as a feature;
- seeded neighbours face the same share bar: a 1-of-30 off-genre neighbour is refused;
- a library seed is not offered as discovery.

Then run every existing test. Then run `playlists.py --dry-run` and read its report: anchors, candidate
count, and discovery count with genre-matched share for all 7. Before any live write, SJ sees that
report.

## Docs

- CLAUDE.md: Stage 8's override description gains `seeds`/`exclude`, plus one invariant bullet: seeds
  never guess and seeded strangers clear the same bar.
- config.py comment on `N_PLAYLISTS`: raised to 7 by SJ on 2026-10-04.
