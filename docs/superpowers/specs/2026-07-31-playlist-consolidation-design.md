# Stage 9 — consolidating two hand-made playlists

Date: 2026-07-31
Status: approved for a first dry run

## Problem

Three playlists exist, all built by hand:

| Playlist | Tracks | First added | Treatment |
| --- | --- | --- | --- |
| `Electric workout` | 261 | 2025-02-10 | current set, house/speed garage/dubstep — kept whole |
| `Hustle💪🏋️` | 410 | 2021-12-14 | older set, hip-hop/rap heavy — filtered |
| `headbang ` | 10 | — | filtered (note the trailing space in the real name) |

The goal is one consolidated playlist: the current set entire, plus the parts of
the others that are not hip-hop or rap. The stage takes any number of sources, each
either `--keep-whole` or `--filter`.

## What this is not

It does not delete or unfollow anything. Spotify has no delete-playlist API — only
`DELETE /playlists/{id}/followers` — and the `Spotify` client in `playlists.py`
deliberately implements no delete verb, with a test asserting it. Consolidation is
additive: a third playlist is created and the two sources are left untouched. The
user unfollows the leftovers by hand.

## Design decisions

Each of these was chosen against real data from this library, not in the abstract.

### Output is a new third playlist

Both sources stay byte-for-byte intact, so a bad filter run costs nothing and the
merge can be re-run until the mix is right.

### Genre judgement is a weighted family balance, not a veto or an allow list

`rap_share = rap_weight / (rap_weight + electronic_weight)` over MusicBrainz tag
counts per artist. Both simpler rules fail on this library's own data:

- A **veto list** ("drop anything tagged hip hop/rap/trap") deletes Daft Punk
  (1 rap tag vs 92 electronic), Skrillex (6 vs 47), Chase & Status, Fred again..,
  Chris Lake and ZHU — the artists the playlist exists for. 178 artists in this
  library carry tags in both families.
- An **allow list** ("keep only electronic-tagged") keeps Kendrick Lamar
  (58 rap vs 2 electronic) and Kanye West (57 vs 5) on the strength of one stray
  electronic tag each.

Weighting separates these cleanly: Daft Punk scores 0.01, Kendrick 0.97.

### Bands, with a review file for the middle

- `rap_share < 0.25` → **keep**
- `rap_share > 0.60` → **drop**
- otherwise → **review**

Off-family tracks (rock, metal, pop — real tags, zero weight in both families) and
artists MusicBrainz cannot tag also go to review. Nothing is guessed in the band
where the data is genuinely unclear, matching how Stage 2 refuses to guess an MBID.

### Family assignment has precedence and a blocklist

Naive substring matching collides on this library's real vocabulary:
`hardcore hip hop` (48 artists) matches both families; `garage rock`,
`garage rock revival`, `hardcore punk` and `post-hardcore` match the electronic
patterns but are not electronic. So each tag is assigned to **exactly one** family:
rap patterns are tested first and win, then electronic patterns are tested with an
explicit blocklist. Everything else is off-family. The resolved membership is
printed in the run report so it can be eyeballed rather than trusted.

### Zero-vote tags fall back to presence

MusicBrainz tag counts go negative on downvotes and are clamped at 0, so an artist
whose only tags sit at 0 votes would score 0/0 and be called off-family despite
being tagged. When both family weights come out 0, the score falls back to counting
tags rather than votes. This is the same failure `MIN_TAG_COUNT_FOR_ANCHOR` exists
to describe — REAPER carrying `heavy metal` at 0 votes.

### Features are scored at half weight, worst credit wins

Spotify's `artists` array does not mark who is featured, so position is the only
signal: `artists[0]` at full weight, the rest at `FEATURE_CREDIT_WEIGHT = 0.5`.
Each credit's contribution is `rap_share(artist) * weight`, and the track takes the
maximum. So a rapper's guest verse pulls a track into review rather than
automatically dropping it, while a rap track under a rap primary still drops.

The discount is **suspended when the primary artist cannot be scored**. It exists
to stop a guest overruling a known primary; with no known primary there is
nothing to protect. Applying it anyway means every rap track whose lead
MusicBrainz has not tagged scores exactly `1.0 * 0.5 = 0.5` and lands in review
rather than dropping — the first real run rescued 15 that way
("Dave, Central Cee — Sprinter", "22Gz, Kodak Black — Spin the Block"), all
plainly rap. Found by reading the run output, not by reasoning about it.

Known imprecision that remains: a genuine co-headline like "Chase & Status,
Stormzy" gets Stormzy half-weighted, scores 0.5 and goes to review — which is the
right destination for it. `CONSOLIDATE_FEATURE_WEIGHT` is a config knob to tune
once more runs have been read.

### Dedupe on folded title, not URI

`playlists._title_key` already exists for this: Spotify presses the album cut, the
single and the remaster as distinct URIs. The dedupe key is the folded title plus
the normalised primary artist. `Electric workout` wins ties, keeping its version
and its position.

### Token handling avoids rotating a shared credential

`poll.access_token()` refreshes unconditionally, and Spotify rotates the PKCE
refresh token when it does. With another instance sharing
`.cache/spotify_token.json`, a needless rotation can invalidate a credential out
from under a run already in flight. Stage 9 therefore reuses the cached access
token and refreshes only when Spotify actually rejects it.

### Artists MusicBrainz has no local tags for go to review

Not to a guess. The first run left 36 such artists — mostly UK drill and rap acts
(A92, ArrDee, Bandokay, CJ, DUSTY LOCANE, Dave) that Stage 2 has never resolved
because they are thin in the listening history.

**Not yet built:** a `--resolve-missing` flag spending 1.1s-per-request
MusicBrainz lookups to tag them, which would shrink the review list by roughly
half. Deferred because it writes to the shared append-only resolution cache and
the first run should not have done that unasked.

## Pipeline

1. Read the cached token; refresh only on rejection.
2. Resolve both sources by **exact** name via `/me/playlists` — exact including
   case, per the existing invariant. Ambiguity or absence is a hard error.
3. Read both playlists, carrying `added_at`, the full artist list and the URI.
4. Dedupe within and across, `Electric workout` winning.
5. Score every `Hustle💪🏋️` track; assign keep / drop / review.
6. Apply `consolidate_overrides.csv` if present — an override always outranks a
   score.
7. Emit `consolidate_review.csv` (machine output, regenerated every run) listing
   everything needing a human decision, with its score, band and tags.
8. Dry run by default; `--write` creates the playlist and fills it. This deviates
   from Stage 8, which writes by default, because Stage 8 only ever touches
   playlists it created and this reads playlists a person built.
9. `report()` prints family membership, band counts, and the kept/dropped split.

Order in the consolidated playlist: all of `Electric workout` in its current order,
then surviving `Hustle💪🏋️` tracks in theirs.

## Files

- `consolidate.py` — the stage; imports `Spotify` and `_title_key` from
  `playlists.py`, `normalise` from `enrich.py`.
- `config.py` — a Stage 9 block, appended (paths and thresholds live only here).
- `consolidate_overrides.example.csv` — tracked; the real file is gitignored, same
  split as `artist_overrides.csv` and `playlist_overrides.csv`.
- `tests/test_consolidate.py` — synthetic DuckDB tag tables, no network.

## Testing

Per CLAUDE.md, the `report()` surface is too coarse to trust for scoring logic.
Assertions that matter:

- Daft Punk (1 vs 92) keeps; Kendrick (58 vs 2) drops — the two cases that break
  the naive rules.
- `hardcore hip hop` lands in rap, `garage rock` in neither.
- Feature half-weight arithmetic: rap feature under an electronic primary lands in
  review, not drop.
- Remaster/single/album-cut fold to one slot.
- Zero-vote tags fall back to presence rather than scoring off-family.
- An override beats the computed band.
- No code path reaches a delete verb.
