# Stage 8 — Gap Playlists Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new stage (`playlists.py`) that builds up to 4 private Spotify playlists — one per under-explored rising genre from Stage 7 — each mixing ~5 familiar "anchor" tracks from the user's own library with ~20 discovery tracks from Stage 5's candidate artists, updated in place on re-run.

**Architecture:** ListenBrainz decides *what* (top recordings per artist, with recording-level genre tags preferred over artist-level where they exist); Spotify is only a URI resolver and a shelf (search to resolve each chosen track, playlist endpoints to write). Playlist identity is a locally stored ID with exact-name fallback; every run archives its selections (and a snapshot of what it's about to overwrite) to Parquet, so the Spotify playlist is just a rendering of local data.

**Tech Stack:** Python 3.12 in `.venv`, DuckDB over Parquet, `requests` via `enrich.Throttled`, Spotify PKCE auth reused from `poll.py` with expanded scopes. No new dependencies.

## Design decisions already made with the user (do not relitigate)

- **B, capped at 4**: one playlist per top gap genre (`genre_gaps.parquet` by `gap_score`), max 4.
- **Track-level genre matching for playlist selection only.** History/trend analysis stays artist-level.
- **Overwrite in place + local archive.** Playlist IDs stored locally; name-match fallback; never dated/new playlists per run.
- **Visible provenance in the title** (template below) *plus* description provenance with refresh date. Playlists are **private**.
- **Anchored discovery**: ~5 tracks from library artists serving the gap genre, ~20 from candidates. Anchors are the user's own most-played recent tracks by those artists (their URIs come free from the export — no search needed).
- **Approach 1**: LB popularity → recording MBIDs → bulk tag lookup → gap-genre-preferred track choice → Spotify search only as URI resolver. Fallback if LB popularity is unusable: Spotify search relevance order per artist (loses track-level matching; a documented degradation, not a redesign).

## Global Constraints

- Every Python invocation uses `.venv/bin/python` — never the system interpreter.
- Prefer SQL in DuckDB over pandas manipulation wherever equivalent.
- Every Parquet write goes through `ORDER BY ALL` (total order; idempotency).
- Network caches are append-only JSONL in `.cache/`, fsynced per record — reuse `recommend.load_jsonl` / `recommend.append_jsonl`.
- MusicBrainz/ListenBrainz throttled via `enrich.Throttled(MB_MIN_INTERVAL)` (1.1 s). Spotify calls spaced ≥ 0.25 s and honor `Retry-After` on 429 (Spotify's is real, unlike MusicBrainz's).
- **The stage must NEVER delete or unfollow a playlist** (the one probe playlist in Task 3 is the sole exception). It may only write to playlists whose ID is in its state file or whose name exactly equals the rendered template.
- All personal outputs land in `data/` (gitignored). No playlist contents, names of listened artists, or IDs in tracked files.
- Feature branch: create `stage8-gap-playlists` before the first commit; PRs need SJ's explicit approval, plain merges don't.
- Tests are plain-python assertion scripts (no pytest dependency) committed under `tests/`, run as `.venv/bin/python tests/<file>.py`, exiting non-zero on failure — same style as the synthetic-data verification documented in CLAUDE.md.
- **User-gated steps:** Task 3 onward needs `SPOTIFY_CLIENT_ID` in `.env` and an interactive browser consent. The Spotify developer app may not exist yet (redirect URI must be exactly `http://127.0.0.1:3000`). Pause and ask the user rather than skipping.

---

### Task 1: Probe the ListenBrainz endpoints (no auth, no user needed)

Approach 1 rests on two endpoints this project has never called. Verify them empirically before building anything, and record the results in this plan file.

**Files:**
- Create: `<scratchpad>/probe_lb.py` (throwaway; not committed)
- Modify: `docs/superpowers/plans/2026-07-31-stage8-gap-playlists.md` (fill in the Probe Results block below)

**Interfaces:**
- Produces: a filled-in **Probe Results** block and a go/no-go decision for the endpoints Task 5 will use verbatim. (Outcome: no-go on both ListenBrainz candidates; Tasks 5 and 6 rewritten around MusicBrainz + Spotify search.)

- [x] **Step 1: Write the probe script**

```python
"""Probe ListenBrainz popularity + bulk tag lookup with known-good MBIDs."""
import json, requests

UA = {"User-Agent": "spotify-trend-analysis/0.1 "
                    "( https://github.com/UncannyJ33/spotify-trend-analysis )"}
DEPECHE = "8538e728-ca0b-4321-b7e5-cff6565dd4c0"   # from recommendations.parquet

# Candidate A: main API host, GET
r = requests.get(
    f"https://api.listenbrainz.org/1/popularity/top-recordings-for-artist/{DEPECHE}",
    headers=UA, timeout=30)
print("A popularity:", r.status_code)
if r.ok:
    items = r.json()
    print("  count:", len(items))
    print("  first:", json.dumps(items[0], indent=2)[:400])

# Candidate B (only if A fails): labs host
r2 = requests.get(
    "https://labs.api.listenbrainz.org/popular-recordings-by-artist/json",
    params={"artist_mbid": DEPECHE}, headers=UA, timeout=30)
print("B popularity (labs):", r2.status_code, r2.text[:200] if not r2.ok else "")

# Bulk tag lookup (labs): POST a JSON array of recording_mbids from A's output
if r.ok and items:
    mbids = [i.get("recording_mbid") for i in items[:5] if i.get("recording_mbid")]
    r3 = requests.post(
        "https://labs.api.listenbrainz.org/bulk-tag-lookup/json",
        json=[{"recording_mbid": m} for m in mbids], headers=UA, timeout=30)
    print("bulk-tag-lookup:", r3.status_code)
    print("  body:", json.dumps(r3.json(), indent=2)[:600] if r3.ok else r3.text[:300])

# MB per-recording fallback for tags (works regardless; 1 req is enough to prove shape)
if r.ok and items:
    r4 = requests.get(
        f"https://musicbrainz.org/ws/2/recording/{mbids[0]}",
        params={"inc": "tags+genres", "fmt": "json"}, headers=UA, timeout=30)
    print("MB recording tags:", r4.status_code)
    if r4.ok:
        d = r4.json()
        print("  genres:", [g["name"] for g in d.get("genres", [])][:8])
        print("  tags  :", [t["name"] for t in d.get("tags", [])][:8])
```

- [x] **Step 2: Run it**

Run: `.venv/bin/python <scratchpad>/probe_lb.py`
Expected: candidate A returns 200 with recording MBIDs + listen counts, bulk-tag-lookup returns 200 with per-recording tags. Any non-200: record status and body.

- [x] **Step 3: Record results and decide**

```
PROBE RESULTS (Task 1) — probed 2026-07-31

  popularity endpoint : NONE — the whole Popularity dataset is unavailable.
      GET /1/popularity/top-recordings-for-artist/{mbid}     -> 500, sticky over 4 tries
      GET /1/popularity/top-release-groups-for-artist/{mbid} -> 500 (same sibling failure)
          body: {"code":500,"error":"Popularity API currently disabled due to
                 high load on the server. Please try again later."}
      GET labs /popular-recordings-by-artist/json            -> 404 (no such endpoint)
      POST /1/popularity/recording {"recording_mbids":[...]} -> 200, but every row
          comes back {"total_listen_count": null, "total_user_count": null},
          for obscure AND canonical recordings alike. The endpoint is up; the
          data behind it is not. There is no listen-count signal to be had.

  bulk tag endpoint   : https://labs.api.listenbrainz.org/bulk-tag-lookup/json (POST
      a JSON array of {"recording_mbid": ...}; GET with a single param also 200s)
  tag response shape  : FLAT array, one row per (recording, tag) — NOT grouped:
      {"recording_mbid", "tag", "tag_count", "percent", "source"}
      `source` is one of 'recording' | 'artist' | 'release-group'. Only 'recording'
      is a true track-level tag; the other two are propagated down from the artist
      or the release and would silently re-introduce artist-level matching.
      A 25-recording batch answered 19 distinct recordings (6 carry no tags at all).

  MB recording tags   : GET /ws/2/recording/{mbid}?inc=tags+genres -> 200, but the
      canonical recordings sampled carried empty genres[] and tags[]. Per-recording
      MB lookups are 1.1 s each and mostly return nothing. Not worth the budget.

  THE ROUTE THAT SURVIVED (found while probing, not in the original design):
      GET /ws/2/recording?query=arid:{artist_mbid} AND tag:"{genre}" -> 200
      One MusicBrainz request returns that artist's recordings carrying that tag.
      Real hits on this project's own candidates:
        Papa Roach   + rap rock    -> count=2   ['Anxiety', 'Last Resort']
        N*E*R*D      + rap rock    -> count=13  ['Truth or Dare', 'Lapdance', ...]
        Seven Lions  + dubstep     -> count=46  ['Days to Come', 'Someday', ...]
        Disturbed    + heavy metal -> count=104 ['The Animal', 'Down With the Sickness (demo)', ...]
      Caveat that shapes the design: ordering WITHIN those hits is Lucene
      relevance, not popularity — Aphex Twin + techno leads with
      'SAW:II CD2.6 / Sexy Bit Courtesy of NinjaTune', System of a Down + heavy
      metal with 'Chupa Cabra / Power Struggle'. It answers WHICH recordings are
      on-genre; it cannot answer which are worth hearing.

  decision            : POPULARITY DEAD -> HYBRID (agreed with the user, 2026-07-31)
```

**Decision: the hybrid.** Neither original branch is taken. Popularity is dead, so
Approach 1 as designed is unbuildable; but the plan's documented degradation
("genre preference then applies only via artist-level tags") would abandon the
user's explicit *track-level genre matching* decision, and the `arid AND tag:`
route makes that unnecessary. So:

- **Spotify search per artist supplies the ordering** — `q=artist:"X"&type=track`
  comes back in relevance order, which is the best popularity proxy still standing,
  and it carries the URI, so resolution and ranking cost one request instead of two.
- **MusicBrainz `arid AND tag:` supplies the genre truth** — one request per
  (artist, gap genre) yields the set of on-genre recording titles.
- **Ranking prefers titles present in that set**, Spotify relevance breaking ties
  within each group. Popular *and* on-genre wins; on-genre-but-obscure beats
  off-genre; the Subtronics rule survives intact.

Two requests per candidate artist. `bulk-tag-lookup` is NOT used: it needs
recording MBIDs as input, and the only way to get those per artist is the same MB
search that already applies the tag filter — so the tag filter is strictly cheaper
and strictly simpler. Task 5 and Task 6 below are rewritten to match; Tasks 2, 3, 4,
7 and 8 are unaffected.

- [x] **Step 4: Commit the updated plan**

```bash
git checkout -b stage8-gap-playlists
git add docs/superpowers/plans/2026-07-31-stage8-gap-playlists.md
git commit -m "Stage 8 plan: record ListenBrainz probe results"
```

---

### Task 2: Scope-aware Spotify auth (`poll.py`) + Stage 8 config block

`poll.py`'s PKCE machinery is scoped to `user-read-recently-played` via a module constant. Parameterise it so `playlists.py` can request playlist scopes, with re-consent taking the **union** of granted + needed scopes so polling never loses capability.

**Files:**
- Modify: `config.py` (append Stage 8 block after the Stage 5 block)
- Modify: `poll.py` (`SCOPE` usage in `authorize` / `access_token`; new pure helper `missing_scopes`)
- Create: `tests/test_scope_auth.py`

**Interfaces:**
- Consumes: existing `poll.authorize(client_id)`, `poll.access_token(client_id)`, `poll.load_tokens()`.
- Produces: `poll.authorize(client_id: str, scope: str = SCOPE) -> dict`; `poll.access_token(client_id: str, scope: str = SCOPE) -> str`; `poll.missing_scopes(tok: dict, scope: str) -> set[str]`. Config names: `PLAYLISTS_PARQUET`, `PLAYLIST_STATE_JSON`, `N_PLAYLISTS`, `PLAYLIST_SIZE`, `ANCHOR_TRACKS`, `TRACKS_PER_ARTIST`, `ANCHOR_WINDOW_MONTHS`, `PLAYLIST_NAME_TEMPLATE`, `PLAYLIST_DESCRIPTION_TEMPLATE`.

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_scope_auth.py — scope arithmetic for the shared Spotify token."""
import sys
sys.path.insert(0, ".")
import poll

failures = []
def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")

check("empty token misses everything",
      poll.missing_scopes({}, "a b"), {"a", "b"})
check("covered token misses nothing",
      poll.missing_scopes({"scope": "a b c"}, "a b"), set())
check("partial token misses the difference",
      poll.missing_scopes({"scope": "user-read-recently-played"},
                          "user-read-recently-played playlist-modify-private"),
      {"playlist-modify-private"})
check("order and duplicates are irrelevant",
      poll.missing_scopes({"scope": "b a"}, "a a b"), set())

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/python tests/test_scope_auth.py`
Expected: `AttributeError: module 'poll' has no attribute 'missing_scopes'`

- [ ] **Step 3: Implement**

In `poll.py`, add below `load_tokens`:

```python
def missing_scopes(tok: dict, scope: str) -> set[str]:
    """Scopes `scope` needs that the stored token was not granted.

    Spotify includes the granted scope string in every token response and
    save_tokens stores the response whole, so this is a pure set difference.
    """
    return set(scope.split()) - set(tok.get("scope", "").split())
```

Change the two signatures (and their internal uses of the module constant):

```python
def authorize(client_id: str, scope: str = SCOPE) -> dict:
    ...
    params = { ... "scope": scope, ... }   # was SCOPE
```

```python
def access_token(client_id: str, scope: str = SCOPE) -> str:
    """A usable access token covering `scope`, refreshing or re-consenting."""
    tok = load_tokens()
    if not tok.get("refresh_token") or missing_scopes(tok, scope):
        # Re-consent with the UNION of old and new scopes, so widening for
        # playlists never narrows what the poller was already granted.
        merged = set(scope.split()) | set(tok.get("scope", "").split())
        return authorize(client_id, " ".join(sorted(merged)))["access_token"]
    # ... refresh flow unchanged from here down
```

In `config.py`, append after the Stage 5 block:

```python
# --- Stage 8: gap playlists --------------------------------------------------
PLAYLISTS_PARQUET = DATA_DIR / "playlists.parquet"      # archive of every run
PLAYLIST_STATE_JSON = DATA_DIR / "playlist_state.json"  # gap tag -> playlist id

N_PLAYLISTS = 4            # hard cap agreed with the user: never more than 4
PLAYLIST_SIZE = 25
ANCHOR_TRACKS = 5          # familiar tracks from library artists serving the gap
TRACKS_PER_ARTIST = 2      # one act must not own a playlist
ANCHOR_WINDOW_MONTHS = 18  # anchors ranked on recent listening, like SEED_WINDOW_MONTHS

# The title marker is for the user's eyes in their own library: anything
# carrying it is pipeline-managed and safe to regenerate; anything without it
# is hand-made and must never be touched.
PLAYLIST_NAME_TEMPLATE = "{genre} frontier · Claude"
PLAYLIST_DESCRIPTION_TEMPLATE = (
    "Rising, under-explored genre in your listening: {genre}. "
    "A few anchors you know, the rest neighbours you don't. "
    "Built by spotify-trend-analysis · refreshed {date}"
)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python tests/test_scope_auth.py`
Expected: `all assertions passed`

- [ ] **Step 5: Regression-check the poller path still parses and defaults correctly**

Run: `.venv/bin/python poll.py --status`
Expected: the Stage 6 status block prints exactly as before (`authorised : no`, ...). No auth prompt.

- [ ] **Step 6: Commit**

```bash
git add config.py poll.py tests/test_scope_auth.py
git commit -m "Stage 8 groundwork: scope-aware Spotify auth and playlist config"
```

---

### Task 3: Probe Spotify search + playlist endpoints (USER-GATED)

**Stop and check with the user first.** This needs (a) a Spotify developer app with redirect URI exactly `http://127.0.0.1:3000` — the user has never run the poller, so it may not exist — and (b) the user present for a browser consent. Ask, wait, then proceed.

**Files:**
- Create: `<scratchpad>/probe_spotify.py` (throwaway; not committed)
- Modify: this plan file (Probe Results block below)

**Interfaces:**
- Consumes: `poll.access_token(client_id, scope)` from Task 2.
- Produces: verified status codes for search / create / replace / details / unfollow, and the user's Spotify user id mechanism (`GET /v1/me`).

- [ ] **Step 1: Write the probe**

```python
"""One-shot probe: search, create private playlist, replace, rename, unfollow."""
import os, sys, time, requests
sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()
import poll

SCOPES = "playlist-modify-private playlist-read-private"
tok = poll.access_token(os.environ["SPOTIFY_CLIENT_ID"], SCOPES)
H = {"Authorization": f"Bearer {tok}"}
API = "https://api.spotify.com/v1"

r = requests.get(f"{API}/search", headers=H, timeout=30,
                 params={"q": 'track:"Enjoy the Silence" artist:"Depeche Mode"',
                         "type": "track", "limit": 3})
print("search:", r.status_code)
items = r.json().get("tracks", {}).get("items", []) if r.ok else []
for t in items:
    print("  ", t["uri"], "-", t["name"], "-", [a["name"] for a in t["artists"]])

me = requests.get(f"{API}/me", headers=H, timeout=30)
print("me:", me.status_code, me.json().get("id") if me.ok else me.text[:200])
uid = me.json()["id"]

c = requests.post(f"{API}/users/{uid}/playlists", headers=H, timeout=30,
                  json={"name": "probe · Claude (delete me)", "public": False,
                        "description": "endpoint probe — safe to delete"})
print("create:", c.status_code)
pid = c.json()["id"]
time.sleep(0.3)

if items:
    rep = requests.put(f"{API}/playlists/{pid}/tracks", headers=H, timeout=30,
                       json={"uris": [items[0]["uri"]]})
    print("replace items:", rep.status_code)

det = requests.put(f"{API}/playlists/{pid}", headers=H, timeout=30,
                   json={"name": "probe renamed · Claude",
                         "description": "renamed by probe"})
print("update details:", det.status_code)

got = requests.get(f"{API}/playlists/{pid}", headers=H, timeout=30,
                   params={"fields": "id,name,tracks.total"})
print("read back:", got.status_code, got.json() if got.ok else "")

# The ONLY deletion this stage ever performs: removing its own probe artifact.
d = requests.delete(f"{API}/playlists/{pid}/followers", headers=H, timeout=30)
print("unfollow probe playlist:", d.status_code)
```

- [ ] **Step 2: Run it (user present for consent)**

Run: `.venv/bin/python <scratchpad>/probe_spotify.py`
Expected: search 200 with URIs; me 200; create 201; replace 200/201 (returns a snapshot_id); details 200; read-back shows the rename and `tracks.total: 1`; unfollow 200. The browser consent screen should list both playlist scopes.

- [x] **Step 3: Record results in the plan**

```
PROBE RESULTS (Task 3) — probed 2026-07-31, client_id 3522b9b8…

  consent scopes  : consent screen listed both playlist scopes and granted them;
                    the stored token reports
                      "playlist-read-private playlist-modify-private"
                    and later, after a wider re-consent,
                      "…playlist-modify-public playlist-read-collaborative"
                    Scope is NOT the problem — see below.

  WORKS (200):
    GET  /me                                    200
    GET  /search?q=artist:"X"&type=track        200, relevance-ordered, validated
    GET  /tracks/{id}                           200
    GET  /me/playlists                          200  (34 playlists, paginates)
    GET  /playlists/{id}   (metadata fields)    200
    PUT  /playlists/{id}   (name, description)  200  — metadata writes DO work

  BLOCKED (403 "Forbidden", empty message, no headers of note):
    POST /users/{uid}/playlists                 403  create
    PUT  /playlists/{id}/tracks                 403  replace  <- Stage 8's core write
    POST /playlists/{id}/tracks                 403  add
    GET  /playlists/{id}/tracks                 403  read contents

  Ruled out before concluding it is the app:
    - not scope: 403 persists with playlist-modify-private, -public,
      read-private and read-collaborative all granted
    - not the request: uid is 25 chars, alnum, URL-safe; playlist id URL-safe;
      GET on the same playlist id returns 200
    - not public/private: create 403s with public true, false, and omitted
    - not the verb: PUT and POST on /tracks both 403

  OTHER RESTRICTIONS OBSERVED (this app is quota-limited, not merely scoped):
    - search `limit` above 10 returns 400 "Invalid limit" (documented max is 50)
    - track objects come back with NO `popularity` field
    - /me/playlists rows report tracks.total as null
    These match the pattern already recorded in CLAUDE.md for this app —
    related-artists, top-tracks, new-releases 403; /v1/recommendations 404.

  surprises       : the split is PLAYLIST CONTENTS, not playlists. This app can
                    create nothing and cannot see or change any playlist's track
                    list, but can rename and re-describe one freely. Stage 8's
                    selection, search and archive layers are all unaffected; only
                    the final write is blocked.

  CONCLUSION      : live playlist writing is not available to this Spotify app.
                    `--dry-run` is fully functional and is the working entry
                    point. The live path is built, tested against fakes, and
                    left in place — it needs no code change if the app is
                    granted Extended Quota Mode.
```

The plan's stop condition was "if search is dead". Search is alive; it is the
write half that is blocked, which this plan did not anticipate. Per the
constraint that this stage must never destroy anything, nothing was forced:
the probe wrote a description to a throwaway playlist SJ created for the test
and touched nothing else.

- [ ] **Step 4: Commit the updated plan**

```bash
git add docs/superpowers/plans/2026-07-31-stage8-gap-playlists.md
git commit -m "Stage 8 plan: record Spotify endpoint probe results"
```

---

### Task 4: Selection core in `playlists.py` — gaps, anchors, candidates, assembly

Pure data logic, all testable offline against synthetic tables. This file section contains no network code.

**Files:**
- Create: `playlists.py` (module docstring, imports, constants, and the four functions below)
- Create: `tests/test_playlist_selection.py`

**Interfaces:**
- Consumes: `data/genre_gaps.parquet` (`tag`, `gap_score`, `hours`, `n_artists`, `rel_change_per_year`), `data/plays.parquet` (`artist_name`, `track_name`, `spotify_track_uri`, `played_seconds`, `month`), `data/artist_tags.parquet` (`artist_name`, `tag`, `is_genre`), `data/recommendations.parquet` (`artist_name`, `mbid`, `score`), `.cache/candidate_tags.jsonl` (`mbid` → `tags[{tag,count}]`), `enrich.normalise`.
- Produces (used by Tasks 5–7):
  - `select_gaps(con) -> list[dict]` — up to `config.N_PLAYLISTS` rows, keys `tag`, `gap_score`, `hours`, `n_artists`, `rel_change_per_year`, ordered by `gap_score` desc.
  - `select_anchor_tracks(con, tag: str) -> list[dict]` — keys `artist_name`, `track_name`, `spotify_track_uri`, `hours`; ≤ `ANCHOR_TRACKS` rows, ≤ `TRACKS_PER_ARTIST` per artist, recent-window only.
  - `select_candidates(con, tag: str, tag_cache: dict) -> list[dict]` — keys `artist_name`, `mbid`, `score`; candidates whose tag vector contains `tag`, excluding artists already in the library, ordered by Stage 5 score desc.
  - `assemble(anchors: list, discovery: list, size: int) -> list[dict]` — final ordered tracklist; each dict gains `slot` = `'anchor'|'discovery'` and `position` (0-based); anchors spread at evenly spaced positions, discovery fills the rest, total ≤ `size`.

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_playlist_selection.py — selection logic on synthetic tables."""
import sys
sys.path.insert(0, ".")
import duckdb
import playlists

con = duckdb.connect()
con.execute("""
CREATE TABLE genre_gaps AS SELECT * FROM (VALUES
  ('dubstep', 0.0005, 30.0, 81, 0.91), ('classical', 0.00008, 6.4, 83, 0.78),
  ('dance', 0.00003, 62.8, 231, 0.20), ('deep house', 0.00002, 8.2, 73, 0.19),
  ('wave', 0.00001, 2.0, 5, 0.50)
) t(tag, gap_score, hours, n_artists, rel_change_per_year)""")
con.execute("""
CREATE TABLE plays AS SELECT * FROM (VALUES
  -- recent dubstep-artist listening: two tracks by Sub, one by Ex
  ('Subtronics', 'Griztronics',  'uri:griz',  7200.0, DATE '2026-06-01'),
  ('Subtronics', 'Scream Saver', 'uri:scream',3600.0, DATE '2026-05-01'),
  ('Subtronics', 'Third Track',  'uri:third', 1800.0, DATE '2026-04-01'),
  ('Excision',   'The Paradox',  'uri:parad', 5400.0, DATE '2026-06-01'),
  -- old play outside the anchor window: must not surface
  ('Subtronics', 'Ancient One',  'uri:old',   9999.0, DATE '2020-01-01'),
  -- library artist with no dubstep tag: must not anchor dubstep
  ('Drake',      'Passionfruit', 'uri:pass',  9000.0, DATE '2026-06-01')
) t(artist_name, track_name, spotify_track_uri, played_seconds, month)""")
con.execute("""
CREATE TABLE artist_tags AS SELECT * FROM (VALUES
  ('Subtronics', 'dubstep', TRUE), ('Excision', 'dubstep', TRUE),
  ('Drake', 'hip hop', TRUE)
) t(artist_name, tag, is_genre)""")
con.execute("""
CREATE TABLE recommendations AS SELECT * FROM (VALUES
  ('Virtual Riot', 'mbid-vr', 0.9), ('SVDDEN DEATH', 'mbid-sd', 0.8),
  ('Boring Artist', 'mbid-ba', 0.7),
  ('Subtronics',    'mbid-sub', 0.99)   -- already in library: must be excluded
) t(artist_name, mbid, score)""")

TAG_CACHE = {
    "mbid-vr":  {"tags": [{"tag": "dubstep", "count": 5}]},
    "mbid-sd":  {"tags": [{"tag": "dubstep", "count": 2}, {"tag": "metal", "count": 1}]},
    "mbid-ba":  {"tags": [{"tag": "ambient", "count": 9}]},
    "mbid-sub": {"tags": [{"tag": "dubstep", "count": 9}]},
}

failures = []
def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")

gaps = playlists.select_gaps(con)
check("gaps capped at N_PLAYLISTS", len(gaps), 4)
check("gaps ordered by score", [g["tag"] for g in gaps][:2], ["dubstep", "classical"])

anchors = playlists.select_anchor_tracks(con, "dubstep")
check("anchor cap respected", len(anchors) <= playlists.config.ANCHOR_TRACKS, True)
check("per-artist cap: Subtronics contributes 2 not 3",
      sum(1 for a in anchors if a["artist_name"] == "Subtronics"), 2)
check("old play outside window excluded",
      any(a["spotify_track_uri"] == "uri:old" for a in anchors), False)
check("untagged-artist track excluded",
      any(a["artist_name"] == "Drake" for a in anchors), False)
check("ranked by recent hours", anchors[0]["spotify_track_uri"], "uri:griz")

cands = playlists.select_candidates(con, "dubstep", TAG_CACHE)
check("gap-tag filter applied", [c["artist_name"] for c in cands],
      ["Virtual Riot", "SVDDEN DEATH"])
check("library artist excluded from discovery",
      any(c["artist_name"] == "Subtronics" for c in cands), False)

tracks = playlists.assemble(
    anchors=[{"spotify_track_uri": f"uri:a{i}"} for i in range(3)],
    discovery=[{"spotify_track_uri": f"uri:d{i}"} for i in range(30)],
    size=10)
check("assembled to size", len(tracks), 10)
check("positions are 0..n-1", [t["position"] for t in tracks], list(range(10)))
check("anchors spread not clumped",
      [t["position"] for t in tracks if t["slot"] == "anchor"], [0, 3, 6])
check("no uri repeats", len({t["spotify_track_uri"] for t in tracks}), 10)

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/python tests/test_playlist_selection.py`
Expected: `ModuleNotFoundError: No module named 'playlists'`

- [ ] **Step 3: Implement**

Create `playlists.py`:

```python
"""Stage 8 — render the genre-gap analysis into Spotify playlists.

    .venv/bin/python playlists.py --dry-run     # selections only, no Spotify writes
    .venv/bin/python playlists.py               # build/refresh the playlists

One playlist per under-explored rising genre (Stage 7's gap analysis), capped
at N_PLAYLISTS. Each mixes ANCHOR_TRACKS familiar tracks — the listener's own
recent plays by library artists serving that genre — with discovery tracks
from Stage 5's candidate artists.

Two sources split the judgment. Spotify's search relevance ORDERS an artist's
tracks — with ListenBrainz's popularity dataset disabled it is the only
popularity signal still standing, and it carries the URI for free. MusicBrainz
says WHICH of that artist's recordings actually carry the gap genre
(`arid AND tag:`), and those are preferred within the relevance order. So the
artist's on-genre work outranks their bigger off-genre hit, without demos and
5.1 remixes outranking everything — which is what MusicBrainz alone would give.

The playlist is a rendering: every run archives its selections locally, and a
snapshot of anything it overwrites, so Spotify never holds the only copy of
anything.

Identity is the locally stored playlist ID (data/playlist_state.json), with an
exact-name fallback on first run. The title carries a visible marker
(PLAYLIST_NAME_TEMPLATE) so pipeline-managed playlists are recognisable in the
library. This stage never deletes or unfollows a playlist.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date

import duckdb

import config
from enrich import Throttled, normalise, MB_MIN_INTERVAL
from recommend import load_jsonl, append_jsonl
from report import pretty

SP_API = "https://api.spotify.com/v1"
SP_SCOPES = "playlist-modify-private playlist-read-private"
SP_MIN_INTERVAL = 0.25
SP_SEARCH_LIMIT = 20        # one page of relevance; TRACKS_PER_ARTIST picks from it

# ListenBrainz popularity is server-side disabled — the by-artist endpoints 500
# and the batch route answers with null counts. See the Task 1 probe record.
# Genre truth comes from MusicBrainz recording search instead.
MB_RECORDING_URL = "https://musicbrainz.org/ws/2/recording"
MB_RECORDING_LIMIT = 100    # one page is plenty; this is a filter, not a ranking

GENRE_RECORDINGS_CACHE = config.CACHE_DIR / "genre_recordings.jsonl"
ARTIST_TRACKS_CACHE = config.CACHE_DIR / "spotify_artist_tracks.jsonl"


# --------------------------------------------------------------------------
# Selection — pure data logic, no network
# --------------------------------------------------------------------------


def select_gaps(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """Top gap genres, hardest-capped at N_PLAYLISTS by agreement."""
    cols = ["tag", "gap_score", "hours", "n_artists", "rel_change_per_year"]
    rows = con.execute(
        f"""
        SELECT {', '.join(cols)} FROM genre_gaps
        ORDER BY gap_score DESC
        LIMIT {config.N_PLAYLISTS}
        """
    ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def select_anchor_tracks(con: duckdb.DuckDBPyConnection, tag: str) -> list[dict]:
    """The listener's own recent favourites by artists serving this genre.

    Anchors are chosen at artist level (the artist carries the gap tag) and
    track level by the listener's own recent play time — their URIs come
    straight from the export, so no search is ever needed for anchors.
    Recording-level genre matching is NOT attempted here: library tracks have
    no recording MBIDs, and resolving them is the per-track explosion this
    project has twice declined.
    """
    cols = ["artist_name", "track_name", "spotify_track_uri", "hours"]
    rows = con.execute(
        f"""
        WITH recent AS (
            SELECT p.artist_name, p.track_name, p.spotify_track_uri,
                   sum(p.played_seconds) / 3600.0 AS hours
            FROM plays p
            JOIN artist_tags t
              ON t.artist_name = p.artist_name AND t.is_genre AND t.tag = ?
            WHERE p.spotify_track_uri IS NOT NULL
              AND p.month >= (SELECT max(month) FROM plays)
                             - INTERVAL {config.ANCHOR_WINDOW_MONTHS} MONTH
            GROUP BY 1, 2, 3
        )
        SELECT artist_name, track_name, spotify_track_uri, hours
        FROM recent
        QUALIFY row_number() OVER (
            PARTITION BY artist_name ORDER BY hours DESC, spotify_track_uri
        ) <= {config.TRACKS_PER_ARTIST}
        ORDER BY hours DESC, spotify_track_uri
        LIMIT {config.ANCHOR_TRACKS}
        """,
        [tag],
    ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def select_candidates(con: duckdb.DuckDBPyConnection, tag: str,
                      tag_cache: dict) -> list[dict]:
    """Stage 5 candidates whose tag vector serves this gap, best score first.

    Library artists are excluded by normalised name: the discovery slots are
    for strangers, and the familiar ones already have the anchor slots.
    """
    known = {normalise(r[0]) for r in con.execute(
        "SELECT DISTINCT artist_name FROM plays WHERE artist_name IS NOT NULL"
    ).fetchall()}
    rows = con.execute(
        "SELECT artist_name, mbid, score FROM recommendations ORDER BY score DESC"
    ).fetchall()
    out = []
    for name, mbid, score in rows:
        if normalise(name) in known:
            continue
        tags = {t["tag"] for t in (tag_cache.get(mbid) or {}).get("tags", [])}
        if tag in tags:
            out.append({"artist_name": name, "mbid": mbid, "score": score})
    return out


def assemble(anchors: list[dict], discovery: list[dict], size: int) -> list[dict]:
    """Interleave: anchors at evenly spaced positions, discovery in between.

    A block of five familiar songs then twenty strangers reads as two
    playlists stapled together; spreading the anchors keeps a foothold always
    within a song or two.
    """
    anchors = list(anchors)[:size]
    n_slots = size
    step = max(1, n_slots // max(len(anchors), 1)) if anchors else n_slots
    anchor_pos = [i * step for i in range(len(anchors)) if i * step < n_slots]

    out, ai, di = [], 0, 0
    seen: set[str] = set()
    for pos in range(n_slots):
        take_anchor = ai < len(anchors) and pos in anchor_pos
        pool, idx = (anchors, ai) if take_anchor else (discovery, di)
        # Skip duplicates within the playlist (same URI via two routes).
        while idx < len(pool) and pool[idx]["spotify_track_uri"] in seen:
            idx += 1
        if take_anchor:
            ai = idx
        else:
            di = idx
        if idx >= len(pool):
            if take_anchor:          # anchors exhausted: let discovery fill
                ai = len(anchors)
                pool, idx = discovery, di
                while idx < len(pool) and pool[idx]["spotify_track_uri"] in seen:
                    idx += 1
                di = idx
                if idx >= len(pool):
                    break
            else:
                break
        row = dict(pool[idx])
        row["slot"] = "anchor" if pool is anchors else "discovery"
        row["position"] = len(out)
        seen.add(row["spotify_track_uri"])
        out.append(row)
        if pool is anchors:
            ai += 1
        else:
            di += 1
    return out
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python tests/test_playlist_selection.py`
Expected: `all assertions passed`

- [ ] **Step 5: Commit**

```bash
git add playlists.py tests/test_playlist_selection.py
git commit -m "Stage 8: gap/anchor/candidate selection and playlist assembly"
```

---

### Task 5: MusicBrainz on-genre recordings and genre-preferred track choice

> **Rewritten after the Task 1 probe.** The original task fetched ListenBrainz
> popularity and bulk recording tags. Popularity is dead (see the probe record), so
> ordering moves to Spotify (Task 6) and this task supplies only the genre truth.

**Files:**
- Modify: `playlists.py` (append a `# MusicBrainz` section)
- Create: `tests/test_track_choice.py`

**Interfaces:**
- Consumes: `MB_RECORDING_URL`, `enrich.Throttled` (has `.get(url, **kw)` only — no `.post` is needed under the hybrid, which is one reason it wins), `enrich.normalise`.
- Produces:
  - `mb_genre_recordings(http, artist_mbid: str, tag: str, cache: dict) -> set[str]` — the **normalised** titles of that artist's recordings carrying `tag`, via `query=arid:{mbid} AND tag:"{tag}"`. Cached per `(artist_mbid, tag)` in `GENRE_RECORDINGS_CACHE`; an empty result caches as an empty list, because "this artist has no recording tagged dubstep" is an answer.
  - `choose_tracks(tracks: list[dict], on_genre: set[str], k: int) -> list[dict]` — `tracks` arrives in Spotify relevance order; a **stable** sort puts on-genre titles first without disturbing relevance within either group; each returned row gains `genre_matched: bool`; ≤ k.

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_track_choice.py — the within-artist genre preference."""
import sys
sys.path.insert(0, ".")
import playlists

failures = []
def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")

# Spotify relevance order: the big off-genre hit is first.
tracks = [
    {"spotify_track_uri": "uri:1", "track_name": "Mega Hit"},
    {"spotify_track_uri": "uri:2", "track_name": "Pure Dubstep"},
    {"spotify_track_uri": "uri:3", "track_name": "Also Dubstep"},
    {"spotify_track_uri": "uri:4", "track_name": "B-side"},
]
on_genre = {playlists.normalise("Pure Dubstep"), playlists.normalise("Also Dubstep")}

got = playlists.choose_tracks(tracks, on_genre, k=3)
check("on-genre beats a higher-ranked off-genre hit",
      [t["spotify_track_uri"] for t in got], ["uri:2", "uri:3", "uri:1"])
check("genre_matched flag set", [t["genre_matched"] for t in got],
      [True, True, False])

check("no on-genre data at all -> relevance order survives untouched",
      [t["spotify_track_uri"] for t in playlists.choose_tracks(tracks, set(), k=2)],
      ["uri:1", "uri:2"])

check("sort is stable within each group",
      [t["spotify_track_uri"] for t in playlists.choose_tracks(tracks, on_genre, k=4)],
      ["uri:2", "uri:3", "uri:1", "uri:4"])

check("k caps output", len(playlists.choose_tracks(tracks, on_genre, k=1)), 1)
check("empty input tolerated", playlists.choose_tracks([], set(), k=3), [])

# Title folding must survive Spotify's suffixes: the MB title is plain, the
# Spotify title carries a remaster/edit tail.
tail = [{"spotify_track_uri": "uri:9",
         "track_name": "Pure Dubstep - Extended Mix"}]
check("suffixed Spotify title still matches the MB title",
      playlists.choose_tracks(tail, on_genre, k=1)[0]["genre_matched"], True)

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/python tests/test_track_choice.py`
Expected: `AttributeError: module 'playlists' has no attribute 'choose_tracks'`

- [ ] **Step 3: Implement** (append to `playlists.py`)

```python
# --------------------------------------------------------------------------
# MusicBrainz — which of this artist's recordings actually serve the genre
# --------------------------------------------------------------------------


def _title_key(name: str) -> str:
    """Fold a track title for comparison across two catalogues.

    Spotify and MusicBrainz disagree constantly about the tail of a title —
    '- 2006 Remaster', '(Extended Mix)', '- Radio Edit'. Everything after the
    first ' - ' or ' (' is dropped before the usual normalise(), so the two
    catalogues are compared on the song rather than on the pressing.
    """
    head = re.split(r"\s+[-–(\[]", name or "", maxsplit=1)[0]
    return normalise(head) or normalise(name or "")


def mb_genre_recordings(http: Throttled, artist_mbid: str, tag: str,
                        cache: dict) -> set[str]:
    """Normalised titles of this artist's recordings tagged with `tag`.

    One request answers 'which of their work is dubstep'. What it cannot answer
    is which of it is any good — MusicBrainz orders by Lucene relevance, so
    demos and 5.1 remixes rank alongside the hits. That is why this is a
    PREFERENCE SET applied over Spotify's relevance order, never an ordering.

    An empty answer is cached like any other: 'nobody tagged this artist's
    recordings dubstep' is a fact, not a failure to retry every run.
    """
    key = f"{artist_mbid}::{tag}"
    if key in cache:
        return {t for t in cache[key]["titles"]}
    r = http.get(MB_RECORDING_URL, params={
        "query": f'arid:{artist_mbid} AND tag:"{tag}"',
        "fmt": "json", "limit": MB_RECORDING_LIMIT,
    })
    titles: set[str] = set()
    if r is not None and r.status_code == 200:
        for rec in r.json().get("recordings", []):
            key_title = _title_key(rec.get("title", ""))
            if key_title:
                titles.add(key_title)
    rec = {"key": key, "artist_mbid": artist_mbid, "tag": tag,
           "titles": sorted(titles)}
    append_jsonl(GENRE_RECORDINGS_CACHE, rec)
    cache[key] = rec
    return titles


def choose_tracks(tracks: list[dict], on_genre: set[str], k: int) -> list[dict]:
    """Prefer the artist's on-genre work; Spotify relevance does the rest.

    `tracks` arrives in Spotify's relevance order, which is the popularity
    proxy. Sorting is STABLE and keyed only on the genre flag, so relevance is
    preserved inside each group: the artist's on-genre work rises above their
    bigger off-genre hit, but among two on-genre tracks the better-known one
    still leads. With no on-genre data the sort is a no-op and this degrades
    cleanly to plain relevance order.
    """
    flagged = [dict(t, genre_matched=_title_key(t.get("track_name", "")) in on_genre)
               for t in tracks]
    flagged.sort(key=lambda t: not t["genre_matched"])
    return flagged[:k]
```

Add to the imports and constants at the top of `playlists.py`:

```python
import re

MB_RECORDING_URL = "https://musicbrainz.org/ws/2/recording"
MB_RECORDING_LIMIT = 100   # one page is plenty; this is a filter, not a ranking

GENRE_RECORDINGS_CACHE = config.CACHE_DIR / "genre_recordings.jsonl"
```

(These are already in the Task 4 header above, which was updated when this task
was rewritten — the ListenBrainz constants it originally carried are gone.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python tests/test_track_choice.py`
Expected: `all assertions passed`

- [ ] **Step 5: Commit**

```bash
git add playlists.py tests/test_track_choice.py
git commit -m "Stage 8: MusicBrainz on-genre recordings and genre-first track choice"
```

---

### Task 5 (superseded) — original ListenBrainz popularity design

<details>
<summary>Kept for the record; unbuildable while the Popularity API is disabled.</summary>

**Interfaces:**
- Consumes: `LB_POP_URL` / `LB_TAG_URL` (as verified in Task 1), `Throttled` (has `.get(url, params=...)`; give it a `.post` only if the probe showed bulk lookup needs POST — if `Throttled` lacks `.post`, use module-level `requests.post` wrapped in the same `time.sleep` spacing, matching whatever the probe proved).
- Produces:
  - `lb_top_recordings(http, artist_mbid: str, cache: dict) -> list[dict]` — keys `recording_mbid`, `recording_name`, `listen_count`; cached per artist in `POPULARITY_CACHE`. **Adjust field extraction to the Task 1 probe record.**
  - `lb_recording_tags(http, mbids: list[str], cache: dict) -> dict[str, set[str]]` — recording mbid → tag set; cached per recording in `RECORDING_TAG_CACHE`; mbids absent from the response cache as empty sets (a miss is an answer).
  - `choose_tracks(recordings: list[dict], rec_tags: dict[str, set[str]], tag: str, k: int) -> list[dict]` — the Subtronics rule: recordings whose own tags contain the gap tag first (by listen_count desc), then untagged-or-unmatched by listen_count desc; ≤ k.

- [ ] **Step 1: Write the failing test** (pure logic only — network functions are exercised in Task 8's live run)

```python
"""tests/test_track_choice.py — the within-artist genre preference."""
import sys
sys.path.insert(0, ".")
import playlists

failures = []
def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")

recs = [
    {"recording_mbid": "r1", "recording_name": "Mega Hit",     "listen_count": 900},
    {"recording_mbid": "r2", "recording_name": "Pure Dubstep", "listen_count": 500},
    {"recording_mbid": "r3", "recording_name": "Also Dubstep", "listen_count": 100},
    {"recording_mbid": "r4", "recording_name": "B-side",       "listen_count": 50},
]
tags = {"r1": {"melodic bass"}, "r2": {"dubstep"}, "r3": {"dubstep"}, "r4": set()}

got = playlists.choose_tracks(recs, tags, "dubstep", k=3)
check("genre-matched beat a bigger unmatched hit",
      [r["recording_mbid"] for r in got], ["r2", "r3", "r1"])

got = playlists.choose_tracks(recs, {}, "dubstep", k=2)
check("no recording tags at all -> popularity order (artist-level fallback)",
      [r["recording_mbid"] for r in got], ["r1", "r2"])

check("k caps output", len(playlists.choose_tracks(recs, tags, "dubstep", k=1)), 1)
check("empty input tolerated", playlists.choose_tracks([], {}, "dubstep", k=3), [])

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/python tests/test_track_choice.py`
Expected: `AttributeError: module 'playlists' has no attribute 'choose_tracks'`

- [ ] **Step 3: Implement** (append to `playlists.py`; adjust the two response-parsing lines to the Task 1 probe record)

```python
# --------------------------------------------------------------------------
# ListenBrainz — what to play, before Spotify is asked where it lives
# --------------------------------------------------------------------------


def lb_top_recordings(http: Throttled, artist_mbid: str, cache: dict) -> list[dict]:
    """Top recordings for an artist, by real listen counts. Cached per artist."""
    if artist_mbid in cache:
        return cache[artist_mbid]["recordings"]
    r = http.get(f"{LB_POP_URL}/{artist_mbid}", params={})
    recordings: list[dict] = []
    if r is not None and r.status_code == 200:
        for item in r.json():
            if item.get("recording_mbid"):
                recordings.append({
                    "recording_mbid": item["recording_mbid"],
                    "recording_name": item.get("recording_name"),
                    "listen_count": item.get("total_listen_count") or 0,
                })
    rec = {"artist_mbid": artist_mbid, "recordings": recordings[:25]}
    append_jsonl(POPULARITY_CACHE, rec)
    cache[artist_mbid] = rec
    return rec["recordings"]


def lb_recording_tags(http: Throttled, mbids: list[str],
                      cache: dict) -> dict[str, set[str]]:
    """Recording-level tags, bulk where possible. A miss caches as empty —
    'nobody tagged this' is an answer, not an error to retry forever."""
    out: dict[str, set[str]] = {}
    todo = [m for m in mbids if m not in cache]
    if todo:
        r = http.post(LB_TAG_URL, json=[{"recording_mbid": m} for m in todo])
        found: dict[str, set[str]] = {}
        if r is not None and r.status_code == 200:
            for item in r.json():          # shape per Task 1 probe record
                m = item.get("recording_mbid")
                if m:
                    found.setdefault(m, set()).add(item.get("tag", ""))
        for m in todo:
            rec = {"recording_mbid": m, "tags": sorted(found.get(m, set()) - {""})}
            append_jsonl(RECORDING_TAG_CACHE, rec)
            cache[m] = rec
    for m in mbids:
        out[m] = set(cache[m]["tags"]) if m in cache else set()
    return out


def choose_tracks(recordings: list[dict], rec_tags: dict[str, set[str]],
                  tag: str, k: int) -> list[dict]:
    """Prefer recordings whose OWN tags match the gap genre; popularity breaks
    ties and fills the remainder. This is the whole point of track-level data:
    the artist's on-genre work beats their bigger off-genre hit."""
    ranked = sorted(
        recordings,
        key=lambda r: (tag not in rec_tags.get(r["recording_mbid"], set()),
                       -(r.get("listen_count") or 0)),
    )
    return ranked[:k]
```

If Task 1 recorded that `Throttled` lacks `.post` and bulk lookup needs one, add to the ListenBrainz section:

```python
import time as _time
import requests as _requests

def _throttled_post(http: Throttled, url: str, *, json):
    """POST spaced like Throttled.get; reuses its User-Agent discipline."""
    _time.sleep(MB_MIN_INTERVAL)
    try:
        return _requests.post(url, json=json, timeout=30,
                              headers={"User-Agent": http.session.headers.get(
                                  "User-Agent", "")} if hasattr(http, "session") else {})
    except _requests.RequestException:
        return None
```
and call `_throttled_post(http, LB_TAG_URL, json=...)` instead of `http.post` — match whichever exists after reading `enrich.Throttled`'s actual attributes.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python tests/test_track_choice.py`
Expected: `all assertions passed`

- [ ] **Step 5: Commit**

```bash
git add playlists.py tests/test_track_choice.py
git commit -m "Stage 8: ListenBrainz popularity, recording tags, genre-first choice"
```

</details>

---

### Task 6: Spotify artist-track search

> **Rewritten after the Task 1 probe.** The original task resolved one known
> (artist, track) pair to a URI, because ListenBrainz was to have chosen the
> tracks. With popularity dead, Spotify's own relevance order IS the track
> choice, so the search is per-artist and returns a ranked list. The result
> validation — never trust a search hit's artist — is unchanged and is the part
> that mattered.

**Files:**
- Modify: `playlists.py` (append a `# Spotify` section)
- Create: `tests/test_uri_resolver.py`

**Interfaces:**
- Consumes: a bearer token (passed in as a string; auth happens once in `main`).
- Produces:
  - `Spotify` — thin bearer client with `get`/`post`/`put` and **no delete**, 429-aware.
  - `_artist_match(item: dict, artist: str) -> bool` — accept a hit only if the wanted artist is actually credited on it, after `enrich.normalise` folding.
  - `sp_artist_tracks(sp, artist: str, cache: dict) -> list[dict]` — up to `SP_SEARCH_LIMIT` of that artist's tracks in Spotify relevance order, keys `artist_name`, `track_name`, `spotify_track_uri`. Cached per normalised artist in `ARTIST_TRACKS_CACHE`; an empty result caches too, so an artist Spotify does not carry is asked exactly once.

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_uri_resolver.py — search result validation, not vibes."""
import sys
sys.path.insert(0, ".")
import playlists

failures = []
def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")


class FakeSp:
    def __init__(self, items):
        self.items = items
        self.calls = 0
    def get(self, path, params=None):
        self.calls += 1
        return {"tracks": {"items": self.items}}


def item(uri, name, *artists):
    return {"uri": uri, "name": name, "artists": [{"name": a} for a in artists]}


# Relevance order is preserved exactly as Spotify returned it.
sp = FakeSp([item("spotify:track:1", "Energy Drink", "Virtual Riot"),
             item("spotify:track:2", "Idols",        "Virtual Riot")])
cache = {}
got = playlists.sp_artist_tracks(sp, "Virtual Riot", cache)
check("relevance order preserved",
      [t["spotify_track_uri"] for t in got], ["spotify:track:1", "spotify:track:2"])
check("track names carried", [t["track_name"] for t in got],
      ["Energy Drink", "Idols"])
check("artist is the one we asked for, not the credit string",
      {t["artist_name"] for t in got}, {"Virtual Riot"})

# A search for one artist returns other people's tracks; they must be dropped.
sp = FakeSp([item("spotify:track:bad", "Virtual Riot Tribute", "Karaoke Crew"),
             item("spotify:track:ok",  "Energy Drink",         "Virtual Riot")])
check("wrong-artist hit dropped",
      [t["spotify_track_uri"] for t in playlists.sp_artist_tracks(sp, "Virtual Riot", {})],
      ["spotify:track:ok"])

# A featured credit still counts as the artist appearing on the track.
sp = FakeSp([item("spotify:track:f", "Collab", "Someone Else", "Virtual Riot")])
check("featured credit accepted",
      [t["spotify_track_uri"] for t in playlists.sp_artist_tracks(sp, "Virtual Riot", {})],
      ["spotify:track:f"])

# Stylisation folds: A$AP vs ASAP
sp = FakeSp([item("spotify:track:3", "Praise the Lord", "A$AP Rocky")])
check("stylised artist name matches",
      [t["spotify_track_uri"] for t in playlists.sp_artist_tracks(sp, "ASAP Rocky", {})],
      ["spotify:track:3"])

# Nothing acceptable -> empty, and the miss is cached so it is asked once.
sp = FakeSp([item("spotify:track:5", "Different Song", "Someone Else")])
cache = {}
check("no usable hit returns empty",
      playlists.sp_artist_tracks(sp, "Nobody At All", cache), [])
check("miss was cached", playlists.normalise("Nobody At All") in cache, True)
playlists.sp_artist_tracks(sp, "Nobody At All", cache)
check("cached miss not re-searched", sp.calls, 1)

# The invariant from Task 7, asserted here too: the client cannot delete.
check("client has no delete method", hasattr(playlists.Spotify, "delete"), False)

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/python tests/test_uri_resolver.py`
Expected: `AttributeError: module 'playlists' has no attribute 'sp_artist_tracks'`

- [ ] **Step 3: Implement** (append to `playlists.py`)

```python
# --------------------------------------------------------------------------
# Spotify — resolver first; the playlist client proper lives below
# --------------------------------------------------------------------------

import time

import requests


class Spotify:
    """Thin bearer-token client. 429s are honoured (Spotify's Retry-After is
    real, unlike MusicBrainz's), capped so a bad header cannot hang a run."""

    def __init__(self, token: str):
        self.h = {"Authorization": f"Bearer {token}"}
        self._last = 0.0

    def _wait(self):
        gap = SP_MIN_INTERVAL - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()

    def _req(self, method: str, path: str, **kw):
        for attempt in (1, 2):
            self._wait()
            try:
                r = requests.request(method, f"{SP_API}{path}", headers=self.h,
                                     timeout=30, **kw)
            except requests.RequestException:
                return None
            if r.status_code == 429 and attempt == 1:
                time.sleep(min(int(r.headers.get("Retry-After", "1") or 1), 30))
                continue
            if r.status_code >= 400:
                return {"_status": r.status_code, "_body": r.text[:200]}
            return r.json() if r.text else {}
        return None

    def get(self, path: str, params: dict | None = None):
        return self._req("GET", path, params=params)

    def post(self, path: str, json: dict):
        return self._req("POST", path, json=json)

    def put(self, path: str, json: dict):
        return self._req("PUT", path, json=json)


def _artist_match(item: dict, artist: str) -> bool:
    """Accept a hit only when the artist we asked for is really credited on it.

    Searching `artist:"Virtual Riot"` is a relevance query, not a filter:
    karaoke acts, tribute covers and 'in the style of' uploads all come back.
    Folding is `enrich.normalise`, the same one Stage 2 resolves names with, so
    'A$AP Rocky' and 'ASAP Rocky' are one artist. A featured credit counts —
    the artist is genuinely on the track.
    """
    want = normalise(artist)
    return want in {normalise(a.get("name", "")) for a in item.get("artists", [])}


def sp_artist_tracks(sp, artist: str, cache: dict) -> list[dict]:
    """This artist's tracks in Spotify's relevance order, validated.

    Relevance order is the whole point: with ListenBrainz popularity down it is
    the only popularity signal left, so the list is returned in exactly the
    order Spotify gave it and nothing here re-sorts it. `artist_name` is set to
    the name we asked for, not the credit string on the result, so downstream
    grouping and the per-artist cap stay keyed on one spelling.

    An empty answer caches like any other: an artist Spotify does not carry is
    asked once, not once per run.
    """
    key = normalise(artist)
    if key in cache:
        return [dict(t, artist_name=artist) for t in cache[key]["tracks"]]
    resp = sp.get("/search", params={
        "q": f'artist:"{artist.replace(chr(34), "")}"',
        "type": "track", "limit": SP_SEARCH_LIMIT,
    })
    items = (resp or {}).get("tracks", {}).get("items", []) if isinstance(resp, dict) else []
    tracks = [{"track_name": it.get("name"), "spotify_track_uri": it.get("uri")}
              for it in items
              if it.get("uri") and _artist_match(it, artist)]
    rec = {"key": key, "artist": artist, "tracks": tracks}
    append_jsonl(ARTIST_TRACKS_CACHE, rec)
    cache[key] = rec
    return [dict(t, artist_name=artist) for t in tracks]
```

Add to the constants at the top of `playlists.py`:

```python
SP_SEARCH_LIMIT = 20        # one page of relevance; TRACKS_PER_ARTIST picks from it
ARTIST_TRACKS_CACHE = config.CACHE_DIR / "spotify_artist_tracks.jsonl"
```

and drop `TRACK_URI_CACHE`, which the per-track resolver used.

Note: `load_jsonl(ARTIST_TRACKS_CACHE, "key")` is how `main` loads this cache — the `key` field exists for that.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python tests/test_uri_resolver.py`
Expected: `all assertions passed`

- [ ] **Step 5: Commit**

```bash
git add playlists.py tests/test_uri_resolver.py
git commit -m "Stage 8: validated Spotify artist-track search with miss caching"
```

---

### Task 7: Playlist lifecycle, state, archive, `main()`, `report()`

**Files:**
- Modify: `playlists.py` (append `# Playlist lifecycle` + `# Archive` + `main`/`report`)
- Create: `tests/test_playlist_lifecycle.py`

**Interfaces:**
- Consumes: everything above; `poll.access_token(client_id, SP_SCOPES)`; `config.PLAYLIST_STATE_JSON`, `config.PLAYLISTS_PARQUET`, name/description templates; `report.pretty`.
- Produces:
  - `load_state() -> dict`, `save_state(d: dict) -> None` — `{gap_tag: {"id": ..., "name": ...}}` in `PLAYLIST_STATE_JSON`.
  - `ensure_playlist(sp, uid: str, tag: str, name: str, state: dict) -> str` — stored-ID → verify; else exact-name adoption from the user's playlists; else create private. Never deletes.
  - `playlist_items(sp, pid: str) -> list[dict]` — current contents (uri, name, artists) for the pre-replace snapshot.
  - `write_archive(con, rows: list[dict]) -> None` — append-style Parquet: read existing + union + `ORDER BY ALL` rewrite.
  - `main()` with `--dry-run` (full selection + resolution, prints the would-be playlists, **no Spotify writes, no state/archive writes**) and default live mode.

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_playlist_lifecycle.py — identity and never-delete, on a fake."""
import sys
sys.path.insert(0, ".")
import playlists

failures = []
def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")


class FakeSp:
    """Records every verb; serves canned playlist data."""
    def __init__(self, existing=None, dead_ids=()):
        self.existing = existing or []      # [{"id","name"}] user's playlists
        self.dead = set(dead_ids)
        self.verbs = []
        self.created = []
    def get(self, path, params=None):
        self.verbs.append(("GET", path))
        if path.startswith("/playlists/"):
            pid = path.split("/")[2]
            if pid in self.dead:
                return {"_status": 404, "_body": "gone"}
            return {"id": pid, "name": "whatever"}
        if path == "/me/playlists":
            return {"items": self.existing, "next": None}
        return {}
    def post(self, path, json):
        self.verbs.append(("POST", path))
        self.created.append(json)
        return {"id": f"new-{len(self.created)}"}
    def put(self, path, json):
        self.verbs.append(("PUT", path))
        return {"snapshot_id": "snap"}


# 1. Stored ID that still exists: reused, nothing created
sp = FakeSp()
state = {"dubstep": {"id": "keep-me", "name": "dubstep frontier · Claude"}}
pid = playlists.ensure_playlist(sp, "uid", "dubstep", "dubstep frontier · Claude", state)
check("stored id reused", pid, "keep-me")
check("nothing created", sp.created, [])

# 2. Stored ID deleted remotely: falls through to name-adopt
sp = FakeSp(existing=[{"id": "adopt-me", "name": "dubstep frontier · Claude"}],
            dead_ids={"keep-me"})
state = {"dubstep": {"id": "keep-me", "name": "dubstep frontier · Claude"}}
pid = playlists.ensure_playlist(sp, "uid", "dubstep", "dubstep frontier · Claude", state)
check("dead id replaced by exact-name adoption", pid, "adopt-me")

# 3. No state, no name match: creates private with the template name
sp = FakeSp(existing=[{"id": "x", "name": "my own dubstep mix"}])
pid = playlists.ensure_playlist(sp, "uid", "dubstep", "dubstep frontier · Claude", {})
check("created fresh", pid, "new-1")
check("created private", sp.created[0]["public"], False)
check("near-miss name NOT adopted",
      any(v == ("PUT", "/playlists/x/tracks") for v in sp.verbs), False)

# 4. The invariant: no DELETE verb exists on the client at all
check("client has no delete method", hasattr(playlists.Spotify, "delete"), False)

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/python tests/test_playlist_lifecycle.py`
Expected: `AttributeError: module 'playlists' has no attribute 'ensure_playlist'`

- [ ] **Step 3: Implement** (append to `playlists.py`)

```python
# --------------------------------------------------------------------------
# Playlist lifecycle — ID first, exact name second, create third. Never delete.
# --------------------------------------------------------------------------


def load_state() -> dict:
    if not config.PLAYLIST_STATE_JSON.exists():
        return {}
    try:
        return json.loads(config.PLAYLIST_STATE_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(d: dict) -> None:
    config.PLAYLIST_STATE_JSON.write_text(
        json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")


def _alive(resp) -> bool:
    return isinstance(resp, dict) and "_status" not in resp and resp.get("id")


def ensure_playlist(sp, uid: str, tag: str, name: str, state: dict) -> str:
    """Resolve the playlist this stage owns for `tag`, creating if needed.

    Identity is the stored ID — immune to the user renaming things. The name
    match is an exact-template fallback for first runs and lost state; a
    near-miss is somebody's hand-made playlist and must never be adopted.
    """
    entry = state.get(tag) or {}
    if entry.get("id"):
        if _alive(sp.get(f"/playlists/{entry['id']}", params={"fields": "id,name"})):
            return entry["id"]

    page = sp.get("/me/playlists", params={"limit": 50})
    while isinstance(page, dict) and "_status" not in page:
        for item in page.get("items", []):
            if item.get("name") == name:
                return item["id"]
        nxt = page.get("next")
        if not nxt:
            break
        page = sp.get(nxt.removeprefix(SP_API), params=None)

    created = sp.post(f"/users/{uid}/playlists", json={
        "name": name, "public": False,
        "description": "created by spotify-trend-analysis",
    })
    if not _alive(created):
        raise SystemExit(f"Could not create playlist '{name}': {created}")
    return created["id"]


def playlist_items(sp, pid: str) -> list[dict]:
    """Current contents, for the pre-replace snapshot. Nothing we overwrite
    goes unrecorded — hand-added tracks included."""
    out, path = [], (f"/playlists/{pid}/tracks"
                    "?fields=items(track(uri,name,artists(name))),next&limit=100")
    resp = sp.get(path, params=None)
    while isinstance(resp, dict) and "_status" not in resp:
        for it in resp.get("items", []):
            t = it.get("track") or {}
            out.append({"uri": t.get("uri"), "track_name": t.get("name"),
                        "artist_name": ", ".join(a.get("name", "")
                                                 for a in t.get("artists", []))})
        nxt = resp.get("next")
        if not nxt:
            break
        resp = sp.get(nxt.removeprefix(SP_API), params=None)
    return out


# --------------------------------------------------------------------------
# Archive — the playlist is a rendering; this Parquet is the record
# --------------------------------------------------------------------------


def write_archive(con: duckdb.DuckDBPyConnection, rows: list[dict]) -> None:
    if not rows:
        return
    cols = ["run_date", "kind", "gap_tag", "playlist_id", "position",
            "slot", "artist_name", "track_name", "spotify_track_uri", "source"]
    con.execute(f"""CREATE OR REPLACE TABLE _new ({', '.join(
        c + (' INTEGER' if c == 'position' else ' VARCHAR') for c in cols)})""")
    con.executemany(
        f"INSERT INTO _new VALUES ({', '.join('?' for _ in cols)})",
        [[r.get(c) for c in cols] for r in rows])
    if config.PLAYLISTS_PARQUET.exists():
        con.execute(f"""CREATE OR REPLACE TABLE _all AS
            SELECT * FROM '{config.PLAYLISTS_PARQUET}' UNION ALL SELECT * FROM _new""")
    else:
        con.execute("CREATE OR REPLACE TABLE _all AS SELECT * FROM _new")
    con.execute(f"COPY (SELECT * FROM _all ORDER BY ALL) TO "
                f"'{config.PLAYLISTS_PARQUET}' (FORMAT PARQUET, COMPRESSION ZSTD)")


# --------------------------------------------------------------------------


def register_sources(con: duckdb.DuckDBPyConnection) -> None:
    needed = {
        "genre_gaps": config.DATA_DIR / "genre_gaps.parquet",
        "plays": config.PLAYS_PARQUET,
        "artist_tags": config.ARTIST_TAGS_PARQUET,
        "recommendations": config.RECOMMENDATIONS_PARQUET,
    }
    for name, path in needed.items():
        if not path.exists():
            raise SystemExit(f"{path} not found — run the earlier stages first.")
        con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM '{path}'")


def build_selections(con, http, sp) -> list[dict]:
    """Everything up to (but excluding) the Spotify writes; shared by both
    modes so --dry-run previews exactly what a live run would do.

    Per candidate artist this spends one Spotify search (relevance order, which
    is the surviving popularity signal) and one MusicBrainz search (which of
    their recordings carry the gap genre). Both are cached, so a re-run inside
    the same quarter spends nothing.
    """
    tag_cache = load_jsonl(config.CACHE_DIR / "candidate_tags.jsonl", "mbid")
    genre_rec_cache = load_jsonl(GENRE_RECORDINGS_CACHE, "key")
    artist_tracks_cache = load_jsonl(ARTIST_TRACKS_CACHE, "key")

    out = []
    for gap in select_gaps(con):
        tag = gap["tag"]
        anchors = select_anchor_tracks(con, tag)
        seen_uris = {a["spotify_track_uri"] for a in anchors}
        discovery = []
        for cand in select_candidates(con, tag, tag_cache):
            if len(discovery) >= config.PLAYLIST_SIZE:   # enough material
                break
            tracks = sp_artist_tracks(sp, cand["artist_name"], artist_tracks_cache)
            if not tracks:
                continue
            on_genre = mb_genre_recordings(http, cand["mbid"], tag, genre_rec_cache)
            for chosen in choose_tracks(tracks, on_genre, config.TRACKS_PER_ARTIST):
                if chosen["spotify_track_uri"] in seen_uris:
                    continue
                seen_uris.add(chosen["spotify_track_uri"])
                discovery.append(chosen)
        tracks = assemble(anchors, discovery, config.PLAYLIST_SIZE)
        out.append({"gap": gap, "tracks": tracks})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Stage 8 gap playlists")
    ap.add_argument("--dry-run", action="store_true",
                    help="select and resolve, print the result, write nothing")
    args = ap.parse_args()

    import os
    from dotenv import load_dotenv
    load_dotenv()
    from poll import access_token

    config.ensure_dirs()
    con = duckdb.connect()
    register_sources(con)

    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    if not client_id:
        raise SystemExit(
            "SPOTIFY_CLIENT_ID missing from .env — Stage 8 shares the Stage 6 "
            "Spotify app (redirect URI exactly http://127.0.0.1:3000).")
    sp = Spotify(access_token(client_id, SP_SCOPES))
    http = Throttled(MB_MIN_INTERVAL)

    selections = build_selections(con, http, sp)
    today = date.today().isoformat()

    if args.dry_run:
        report(selections, dry=True)
        return

    me = sp.get("/me", params=None)
    if not _alive(me):
        raise SystemExit(f"Could not read the current user: {me}")
    uid = me["id"]

    state = load_state()
    archive_rows = []
    for sel in selections:
        tag = sel["gap"]["tag"]
        name = config.PLAYLIST_NAME_TEMPLATE.format(genre=pretty(tag))
        pid = ensure_playlist(sp, uid, tag, name, state)

        for i, old in enumerate(playlist_items(sp, pid)):
            archive_rows.append({
                "run_date": today, "kind": "pre_replace_snapshot", "gap_tag": tag,
                "playlist_id": pid, "position": i, "slot": None,
                "artist_name": old["artist_name"], "track_name": old["track_name"],
                "spotify_track_uri": old["uri"], "source": "spotify",
            })

        uris = [t["spotify_track_uri"] for t in sel["tracks"]]
        resp = sp.put(f"/playlists/{pid}/tracks", json={"uris": uris})
        if not isinstance(resp, dict) or "_status" in resp:
            print(f"  ⚠ replace failed for '{name}': {resp} — skipping")
            continue
        sp.put(f"/playlists/{pid}", json={
            "name": name,
            "description": config.PLAYLIST_DESCRIPTION_TEMPLATE.format(
                genre=pretty(tag), date=today),
        })
        state[tag] = {"id": pid, "name": name}
        for t in sel["tracks"]:
            archive_rows.append({
                "run_date": today, "kind": "selection", "gap_tag": tag,
                "playlist_id": pid, "position": t["position"], "slot": t["slot"],
                "artist_name": t["artist_name"], "track_name": t["track_name"],
                "spotify_track_uri": t["spotify_track_uri"],
                "source": "plays" if t["slot"] == "anchor" else "listenbrainz",
            })

    save_state(state)
    write_archive(con, archive_rows)
    report(selections, dry=False)


def report(selections: list[dict], dry: bool) -> None:
    print()
    print("=" * 74)
    print(f"STAGE 8 — GAP PLAYLISTS {'(dry run — nothing written)' if dry else ''}")
    print("=" * 74)
    for sel in selections:
        g = sel["gap"]
        n_anchor = sum(1 for t in sel["tracks"] if t["slot"] == "anchor")
        n_disc = len(sel["tracks"]) - n_anchor
        n_matched = sum(1 for t in sel["tracks"] if t.get("genre_matched"))
        print(f"\n{config.PLAYLIST_NAME_TEMPLATE.format(genre=pretty(g['tag']))}")
        print(f"  gap: {g['n_artists']} artists, {g['hours']:.0f} h, "
              f"{100 * g['rel_change_per_year']:+.0f}%/yr")
        print(f"  {len(sel['tracks'])} tracks — {n_anchor} anchors, {n_disc} "
              f"discovery ({n_matched} matched on recording-level tags)")
        for t in sel["tracks"][:8]:
            mark = "⚓" if t["slot"] == "anchor" else " "
            print(f"   {mark} {t['artist_name'][:28]:<28} {t['track_name'][:38]}")
        if len(sel["tracks"]) > 8:
            print(f"     ... {len(sel['tracks']) - 8} more")
    if not dry:
        print(f"\nArchive -> {config.PLAYLISTS_PARQUET}")
        print(f"State   -> {config.PLAYLIST_STATE_JSON}")
    print("=" * 74)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python tests/test_playlist_lifecycle.py`
Expected: `all assertions passed`
Also re-run Tasks 4–6 tests (imports changed):
`.venv/bin/python tests/test_playlist_selection.py && .venv/bin/python tests/test_track_choice.py && .venv/bin/python tests/test_uri_resolver.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add playlists.py tests/test_playlist_lifecycle.py
git commit -m "Stage 8: playlist lifecycle, archive, dry-run and live modes"
```

---

### Task 8: Live end-to-end run (USER PRESENT), then docs

**Files:**
- Modify: `README.md` (command table row + Stage 8 section)
- Modify: `CLAUDE.md` (commands block, invariants, gotchas)

- [ ] **Step 1: Dry run against real data**

Run: `.venv/bin/python playlists.py --dry-run`
Expected: 4 playlists printed (dubstep / classical / dance / deep house on current data), anchors from the user's library, discovery tracks resolving to URIs. This spends LB + Spotify-search requests and fills the three caches; acceptable. Inspect the output *with the user* — this is the moment to catch a selection that looks wrong before anything touches their library.

- [ ] **Step 2: Live run (user consents in browser if scopes changed)**

Run: `.venv/bin/python playlists.py`
Expected: 4 private playlists appear in the user's Spotify library bearing the `· Claude` marker; report prints IDs; `data/playlist_state.json` and `data/playlists.parquet` exist. Verify in the Spotify client with the user.

- [ ] **Step 3: Idempotency check**

Run: `.venv/bin/python playlists.py` (again, immediately)
Expected: same 4 playlist IDs (state reuse — no new playlists), contents replaced with identical tracks, archive grows by one run's rows plus snapshots. `git status` clean of anything personal.

- [ ] **Step 4: Update README.md**

Add to the All commands table after the Forecast row:
```markdown
| 8. Playlists | `python playlists.py` (`--dry-run`) | 4 private Spotify playlists + `data/playlists.parquet` |
```
Add a `### Stage 8 — Gap playlists` section after Stage 7's, covering: one playlist per top gap genre (≤4); anchored discovery (own recent favourites + candidate neighbours); recording-level tag preference with artist-level fallback; overwrite-in-place with local archive + pre-replace snapshot; the `· Claude` title marker; shares the Stage 6 app and PKCE flow with two playlist scopes; `--dry-run` previews without writing. Note Spotify is only resolver + shelf; selection judgment is ListenBrainz/MusicBrainz data.

- [ ] **Step 5: Update CLAUDE.md**

Commands block, after forecast.py:
```
.venv/bin/python playlists.py --dry-run    # Stage 8 preview — no Spotify writes
.venv/bin/python playlists.py              # Stage 8 → 4 private playlists + data/playlists.parquet
```
Invariants section, add:
```markdown
- **Stage 8 never deletes or unfollows a playlist.** It writes only to IDs in
  `data/playlist_state.json` or an exact `PLAYLIST_NAME_TEMPLATE` name match; a
  near-miss name is somebody's hand-made playlist. Before every replace it
  snapshots current contents into `data/playlists.parquet` (`kind =
  'pre_replace_snapshot'`), so nothing it overwrites goes unrecorded. The
  `Spotify` client deliberately has no delete method — keep it that way.
- **Spotify scopes only ever widen.** `poll.access_token(client_id, scope)`
  re-consents with the union of granted + needed, so running Stage 8 never
  strips the poller's scope or vice versa.
```
Gotchas: update the "poller has never been run" bullet if the Task 8 consent changed that; add that Stage 8 shares the Stage 6 developer app.

- [ ] **Step 6: Run the full test set once more, then commit**

Run: `for t in tests/test_*.py; do .venv/bin/python "$t" || break; done`
Expected: every file ends `all assertions passed`.

```bash
git add README.md CLAUDE.md
git commit -m "Stage 8: document gap playlists in README and CLAUDE.md"
```

- [ ] **Step 7: Merge (no PR without SJ's approval)**

```bash
git checkout main && git merge --ff-only stage8-gap-playlists && git push origin main
git branch -d stage8-gap-playlists && git push origin --delete stage8-gap-playlists
```

---

## Self-review record

- **Spec coverage:** all eight user decisions map to tasks (cap-4 → `select_gaps` LIMIT; track-level-playlists-only → Task 5 `choose_tracks` + anchor docstring explicitly declining recording MBIDs for library tracks; overwrite+archive → Task 7; marker naming → config templates; anchored mix → Tasks 4/7; Approach 1 + fallbacks → Tasks 1/5).
- **Placeholder scan:** the two "filled in by executor" probe blocks are deliberate decision records, not implementation gaps; every code step is complete.
- **Type consistency:** `select_*` return `list[dict]` with the exact keys their consumers read (`build_selections`, `assemble`, tests); `Spotify.get/post/put` signatures match every call site; `missing_scopes` name is identical in Task 2's code and test.
