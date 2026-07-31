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
- Produces: a filled-in **Probe Results** block and a go/no-go decision for `LB_POP_URL` and `LB_TAG_URL` used verbatim in Task 5.

- [ ] **Step 1: Write the probe script**

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

- [ ] **Step 2: Run it**

Run: `.venv/bin/python <scratchpad>/probe_lb.py`
Expected: candidate A returns 200 with recording MBIDs + listen counts, bulk-tag-lookup returns 200 with per-recording tags. Any non-200: record status and body.

- [ ] **Step 3: Record results and decide**

Fill in this block in the plan file (edit in place):

```
PROBE RESULTS (Task 1) — filled in by executor
  popularity endpoint : <URL that worked, or NONE>
  response shape      : <field names for recording mbid / name / listen count>
  bulk tag endpoint   : <URL + method that worked, or NONE>
  tag response shape  : <field names>
  decision            : APPROACH 1 as designed | tags via MB per-recording fallback | POPULARITY DEAD -> Spotify-search fallback (Task 5 note)
```

Decision rules: popularity works + bulk tags work → proceed as designed. Popularity works, bulk tags dead → per-recording tags via MusicBrainz `inc=tags+genres` at 1.1 s (bounded: ~50 recordings per playlist refresh). Popularity dead → Task 5's `lb_top_recordings` is replaced by Spotify search-per-artist relevance order (Approach 2 degradation); genre preference then applies only via artist-level tags.

- [ ] **Step 4: Commit the updated plan**

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

- [ ] **Step 3: Record results in the plan**

```
PROBE RESULTS (Task 3) — filled in by executor
  search          : <status; do results carry uri/name/artists as expected?>
  create/replace  : <statuses>
  details/read    : <statuses>
  consent scopes  : <what the consent screen showed>
  surprises       : <anything the client in Task 7 must accommodate>
```

If search is dead (403/404): STOP. Approach 1's resolver and Approach 2's fallback both die with it; report to the user — the playlist feature is not currently buildable, only the dry-run selection layer is.

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

ListenBrainz decides WHAT (top recordings per artist, preferring recordings
whose own tags match the gap genre); Spotify only resolves each chosen track
to a URI and holds the shelf. The playlist is a rendering: every run archives
its selections locally, and a snapshot of anything it overwrites, so Spotify
never holds the only copy of anything.

Identity is the locally stored playlist ID (data/playlist_state.json), with an
exact-name fallback on first run. The title carries a visible marker
(PLAYLIST_NAME_TEMPLATE) so pipeline-managed playlists are recognisable in the
library. This stage never deletes or unfollows a playlist.
"""

from __future__ import annotations

import argparse
import json
from datetime import date

import duckdb

import config
from enrich import Throttled, normalise, MB_MIN_INTERVAL
from recommend import load_jsonl, append_jsonl
from report import pretty

SP_API = "https://api.spotify.com/v1"
SP_SCOPES = "playlist-modify-private playlist-read-private"
SP_MIN_INTERVAL = 0.25

# Set from the Task 1 probe record; both candidates are in the plan.
LB_POP_URL = "https://api.listenbrainz.org/1/popularity/top-recordings-for-artist"
LB_TAG_URL = "https://labs.api.listenbrainz.org/bulk-tag-lookup/json"

POPULARITY_CACHE = config.CACHE_DIR / "recording_popularity.jsonl"
RECORDING_TAG_CACHE = config.CACHE_DIR / "recording_tags.jsonl"
TRACK_URI_CACHE = config.CACHE_DIR / "spotify_track_uris.jsonl"


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

### Task 5: ListenBrainz fetchers and genre-preferred track choice

**Files:**
- Modify: `playlists.py` (append a `# ListenBrainz` section)
- Create: `tests/test_track_choice.py`

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

---

### Task 6: Spotify URI resolver

**Files:**
- Modify: `playlists.py` (append a `# Spotify resolver` section)
- Create: `tests/test_uri_resolver.py`

**Interfaces:**
- Consumes: a bearer token (passed in as a string; auth happens once in `main`).
- Produces: `sp_search_uri(sp, artist: str, track: str, cache: dict) -> str | None` where `sp` is a `Spotify` client (Task 7 defines it; for this task only `sp.get(path, params) -> dict|None` is needed — define the minimal `Spotify` class here and Task 7 extends it). Misses cache as `{"uri": None}` so a track Spotify lacks is asked exactly once. Match validation via `_match(result, artist, track) -> bool`.

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


# Exact-ish hit accepted
sp = FakeSp([item("spotify:track:1", "Pure Dubstep", "Virtual Riot")])
cache = {}
check("plain hit resolves",
      playlists.sp_search_uri(sp, "Virtual Riot", "Pure Dubstep", cache),
      "spotify:track:1")

# Wrong artist rejected even at rank 1; right artist at rank 2 wins
sp = FakeSp([item("spotify:track:bad", "Pure Dubstep", "Karaoke Crew"),
             item("spotify:track:2",  "Pure Dubstep", "Virtual Riot")])
check("wrong-artist result skipped",
      playlists.sp_search_uri(sp, "Virtual Riot", "Pure Dubstep", {}),
      "spotify:track:2")

# Stylisation folds: A$AP vs ASAP
sp = FakeSp([item("spotify:track:3", "Praise The Lord", "A$AP Rocky")])
check("stylised artist name matches",
      playlists.sp_search_uri(sp, "ASAP Rocky", "Praise The Lord", {}),
      "spotify:track:3")

# Remaster suffix on the result title still matches the plain query
sp = FakeSp([item("spotify:track:4", "Enjoy the Silence - 2006 Remaster", "Depeche Mode")])
check("suffixed title matches",
      playlists.sp_search_uri(sp, "Depeche Mode", "Enjoy the Silence", {}),
      "spotify:track:4")

# Nothing acceptable -> None, and the miss is cached
sp = FakeSp([item("spotify:track:5", "Different Song", "Someone Else")])
cache = {}
check("no match returns None",
      playlists.sp_search_uri(sp, "Virtual Riot", "Pure Dubstep", cache), None)
check("miss was cached", playlists._uri_key("Virtual Riot", "Pure Dubstep") in cache, True)
playlists.sp_search_uri(sp, "Virtual Riot", "Pure Dubstep", cache)
check("cached miss not re-searched", sp.calls, 1)

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/python tests/test_uri_resolver.py`
Expected: `AttributeError: module 'playlists' has no attribute 'sp_search_uri'`

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


def _uri_key(artist: str, track: str) -> str:
    return f"{normalise(artist)} :: {normalise(track)}"


def _match(item: dict, artist: str, track: str) -> bool:
    """Accept a result only when both names really agree, after the same
    folding Stage 2 uses. Titles match on prefix so '— 2006 Remaster' and
    similar suffixes do not cost the track."""
    want_artist = normalise(artist)
    got_artists = {normalise(a.get("name", "")) for a in item.get("artists", [])}
    if want_artist not in got_artists:
        return False
    want, got = normalise(track), normalise(item.get("name", ""))
    return got == want or got.startswith(want) or want.startswith(got)


def sp_search_uri(sp, artist: str, track: str, cache: dict) -> str | None:
    """One targeted search; the first VALIDATED result wins; misses cached."""
    key = _uri_key(artist, track)
    if key in cache:
        return cache[key]["uri"]
    q = f'track:"{track.replace(chr(34), "")}" artist:"{artist.replace(chr(34), "")}"'
    resp = sp.get("/search", params={"q": q, "type": "track", "limit": 5})
    uri = None
    items = (resp or {}).get("tracks", {}).get("items", []) if isinstance(resp, dict) else []
    for item in items:
        if _match(item, artist, track):
            uri = item.get("uri")
            break
    rec = {"key": key, "artist": artist, "track": track, "uri": uri}
    append_jsonl(TRACK_URI_CACHE, rec)
    cache[key] = rec
    return uri
```

Note: `load_jsonl(TRACK_URI_CACHE, "key")` is how `main` will load this cache — the `key` field exists for that.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python tests/test_uri_resolver.py`
Expected: `all assertions passed`

- [ ] **Step 5: Commit**

```bash
git add playlists.py tests/test_uri_resolver.py
git commit -m "Stage 8: validated Spotify URI resolver with miss caching"
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


def build_selections(con, http, sp, dry: bool) -> list[dict]:
    """Everything up to (but excluding) the Spotify writes; shared by both
    modes so --dry-run previews exactly what a live run would do."""
    tag_cache = load_jsonl(config.CACHE_DIR / "candidate_tags.jsonl", "mbid")
    pop_cache = load_jsonl(POPULARITY_CACHE, "artist_mbid")
    rtag_cache = load_jsonl(RECORDING_TAG_CACHE, "recording_mbid")
    uri_cache = load_jsonl(TRACK_URI_CACHE, "key")

    out = []
    for gap in select_gaps(con):
        tag = gap["tag"]
        anchors = select_anchor_tracks(con, tag)
        discovery = []
        for cand in select_candidates(con, tag, tag_cache):
            if len(discovery) >= config.PLAYLIST_SIZE:   # enough material
                break
            recs = lb_top_recordings(http, cand["mbid"], pop_cache)
            rtags = lb_recording_tags(http, [r["recording_mbid"] for r in recs],
                                      rtag_cache)
            for chosen in choose_tracks(recs, rtags, tag, config.TRACKS_PER_ARTIST):
                uri = sp_search_uri(sp, cand["artist_name"],
                                    chosen["recording_name"], uri_cache)
                if uri:
                    discovery.append({
                        "artist_name": cand["artist_name"],
                        "track_name": chosen["recording_name"],
                        "spotify_track_uri": uri,
                        "genre_matched": tag in rtags.get(chosen["recording_mbid"], set()),
                    })
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

    selections = build_selections(con, http, sp, args.dry_run)
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
