# Chill Playlists Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stage 8 builds 7 frontier playlists from `playlist_overrides.csv`, where each row may name `seeds` (artists whose ListenBrainz neighbours feed discovery) and `exclude` (artists kept out of that playlist entirely). The speed garage playlist is retired.

**Architecture:** Two optional pipe-separated columns are added to the existing override file. A new `seed_candidates` in `playlists.py` resolves seeds without guessing. It pools their neighbours using Stage 5's cached `fetch_similar`/`fetch_candidate_tags`, normalised per seed, and runs them through the **same** `serving_sql` bar Stage 5's candidates already clear. Excludes filter anchors (in SQL), candidates, and discovery tracks (Spotify credit list).

**Tech Stack:** Python 3.12 (`.venv/bin/python` only), DuckDB in-memory, standalone test scripts in `tests/` (no pytest).

**Spec:** `docs/superpowers/specs/2026-10-04-chill-playlists-design.md`

## Global Constraints

- Every command runs through `.venv/bin/python`, never the system interpreter.
- Tests are standalone scripts run from the repo root: `.venv/bin/python tests/test_X.py`. They exit 1 on failure, use no network, and point `SPOTIFY_DATA_DIR`/`SPOTIFY_CACHE_DIR`/`SPOTIFY_PLAYLIST_OVERRIDES` at a temp dir **before** importing `config` (copy the header of `tests/test_discovery_lead.py`).
- `config.py` is the only place tuning constants live. New: `N_PLAYLISTS = 7`, `PLAYLIST_SEED_CANDIDATES = 40`.
- Network caches are append-only JSONL, fsynced per record, and hold only real answers. An `"error"` resolution is never written.
- Seed resolution **never guesses**: only `status == "resolved"` with an MBID is used. `artist_overrides.csv` outranks every cache (CLAUDE.md): a seed answered `IGNORE`/`NONE` there is skipped before Stage 2's raw cache is read, because that cache still holds the wrong auto-match the row exists to throw away (PLAT.), and the MBID is what seeds ListenBrainz.
- Seeded strangers clear `serving_sql` (`ANCHOR_MIN_TAG_SHARE` 0.25 + `MIN_TAG_COUNT_FOR_ANCHOR`), with no lower bar.
- Name comparisons go through `enrich.normalise`. SQL uses it as a registered UDF (`_register_udf`), never a re-spelling.
- `playlist_overrides.csv` is gitignored (personal taste). Edit it, never `git add` it. `playlist_overrides.example.csv` is tracked.
- Stage 8 never deletes or unfollows a playlist. Do not add that.
- Feature work goes on branch `chill-playlists`, not `main`. Commit after each task. Git writes may be denied by a guard hook ("another git write is in progress"): wait a few seconds and retry; never remove a lock.
- Commit messages end with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_013AVyFuk1JgDykno4V8vJ8Q
  ```

## Review Focus

1. **A seed MusicBrainz cannot resolve exactly** (a name it files under another primary name, or one its fold discards): expect one warning naming the seed and the `Name=MBID` remedy. It must not take the top hit. Pinned in Task 3 (`ambiguous seed skipped`). For the dry run's expectations: measured 2026-10-04, 12 of the 16 real seeds (Joji, Frank Ocean, Masego, FKJ, Daniel Caesar, Still Woozy, Brent Faiyaz, Tycho, Dayglow, Men I Trust, Clairo, Toro y Moi) already sit in `artist_tags` with an MBID and resolve free — and all 12 are in `plays`, so none is ever offered as a candidate. Only Nujabes, keshi, Washed Out and Macintosh Plus cost a MusicBrainz search. Joji's 100 ListenBrainz neighbours are already in `similar_artists.jsonl`.
2. **A seed that is a library artist** (Joji): it feeds neighbours but is never offered as a discovery candidate. Pinned in Task 3.
3. **An excluded artist arriving through a feature on someone else's record** (Drake on a candidate's track): the track is dropped. Pinned in Task 2.
4. **An old two-column override file:** it parses exactly as before, with seeds/exclude `[]`. Pinned in Task 1.
5. **A ListenBrainz failure for one seed:** the other seeds still supply candidates, and nothing is cached for the failed one (`fetch_similar` already guarantees this). Pinned in Task 3 (`a seed with no neighbours does not sink the rest`) — the fixture's dead seed must carry a real UUID, or `resolve_seed` refuses it at the `Name=MBID` check and the path is never exercised.
6. **A seed answered `NONE` in `artist_overrides.csv` whose Stage 2 raw cache still holds a wrong `resolved` match** (PLAT. on ＰＬＡＴ): it is skipped, not seeded on the stranger's MBID. Pinned in Task 3.
7. **Raising the cap breaks two existing fixtures silently.** `tests/test_playlist_overrides.py` and `tests/test_playlist_selection.py` both build `genre_gaps` with FIVE rows and assert on four; at `N_PLAYLISTS = 7` the fallback returns all five. Reading `config.N_PLAYLISTS` in the assertion is not enough — five rows cannot exercise a cap of seven. Task 1 widens both fixtures to eight rows.

---

### Task 1: Override columns and the cap

**Files:**
- Modify: `config.py:118` (`N_PLAYLISTS`)
- Modify: `playlists.py` `load_playlist_specs` (~line 321)
- Modify: `playlist_overrides.example.csv`
- Test: `tests/test_playlist_overrides.py`
- Test: `tests/test_playlist_selection.py` (its `genre_gaps` fixture and the cap assertion at line 70 — see Step 1)

**Interfaces:**
- Produces: every spec dict from `load_playlist_specs` carries `"seeds": list[str]` and `"exclude": list[str]` (`[]` when absent or blank, including on the no-file gap fallback).

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_playlist_overrides.py`, before the final `if failures:` block:

```python
# --------------------------------------------------------------------------
# seeds / exclude — optional columns (2026-10-04). An old two-column file must
# parse exactly as before.
# --------------------------------------------------------------------------
csv_path.write_text("label,tags\nindie,indie pop\n", encoding="utf-8")
s = playlists.load_playlist_specs(con)[0]
check("two-column file -> seeds []", s["seeds"], [])
check("two-column file -> exclude []", s["exclude"], [])

csv_path.write_text(
    "label,tags,seeds,exclude\n"
    "vapor soul,neo soul|alternative r&b, Frank Ocean | Masego ,Drake|  NAV \n"
    "lo-fi,lo-fi hip hop,Joji,\n", encoding="utf-8")
specs = playlists.load_playlist_specs(con)
check("seeds split and trimmed", specs[0]["seeds"], ["Frank Ocean", "Masego"])
check("exclude split and trimmed", specs[0]["exclude"], ["Drake", "NAV"])
check("blank exclude -> []", specs[1]["exclude"], [])

csv_path.unlink()
check("gap fallback specs carry empty seeds/exclude",
      {(tuple(s["seeds"]), tuple(s["exclude"]))
       for s in playlists.load_playlist_specs(con)}, {((), ())})
```

Also change the existing cap test from 9 rows to 12, so it still exceeds the new cap:

```python
csv_path.write_text("label,tags\n" + "".join(
    f"g{i},tag{i}\n" for i in range(12)), encoding="utf-8")
```

**Two existing fixtures assume the cap is 4 and have only five gap rows**, so at 7 the fallback returns all five and the assertions fail. Widen both to eight rows so they exceed the new cap and still prove it (verified: `select_gaps` is `ORDER BY gap_score DESC LIMIT N_PLAYLISTS`, so the new rows must score below the ones already asserted on).

In `tests/test_playlist_overrides.py`, the `genre_gaps` fixture (lines 25–30) becomes:

```python
con.execute("""
CREATE TABLE genre_gaps AS SELECT * FROM (VALUES
  ('dubstep', 0.0005, 30.0, 81, 0.91), ('heavy metal', 0.0003, 4.9, 16, 1.27),
  ('techno', 0.0004, 8.7, 38, 1.32), ('rap rock', 0.0007, 12.3, 13, 1.01),
  ('edm', 0.0002, 98.7, 160, 0.50), ('ambient', 0.00015, 1.0, 3, 0.10),
  ('jazz', 0.0001, 1.0, 3, 0.10), ('folk', 0.00005, 1.0, 3, 0.10)
) t(tag, gap_score, hours, n_artists, rel_change_per_year)""")
```

and the two fallback assertions (lines 40–43) expect the top seven of eight — `folk` is the one the cap drops:

```python
check("no override file -> top gaps by score",
      [s["label"] for s in specs],
      ["rap rock", "dubstep", "techno", "heavy metal", "edm", "ambient", "jazz"])
check("fallback specs are single-tag", [s["tags"] for s in specs],
      [["rap rock"], ["dubstep"], ["techno"], ["heavy metal"], ["edm"],
       ["ambient"], ["jazz"]])
```

Nothing else in that file reads the fixture by count: `dubstep` is still a gap, `indie` still is not, `n_artists == 81` still holds.

In `tests/test_playlist_selection.py`, add three rows scoring below `wave` to its `genre_gaps` fixture (lines 9–13):

```python
  ('wave', 0.00001, 2.0, 5, 0.50), ('ambient', 0.000009, 1.0, 3, 0.10),
  ('jazz', 0.000008, 1.0, 3, 0.10), ('folk', 0.000007, 1.0, 3, 0.10)
```

and change line 70 to read the cap from config:

```python
check("gaps capped at N_PLAYLISTS", len(gaps), playlists.config.N_PLAYLISTS)
```

`gaps ordered by score` still sees `dubstep, classical` first; nothing else in that file touches `genre_gaps`.

- [ ] **Step 2: Run, expect FAIL** (`KeyError: 'seeds'`, and the two widened fallback checks fail on count until `N_PLAYLISTS` is 7):
  `.venv/bin/python tests/test_playlist_overrides.py`

- [ ] **Step 3: Implement.** In `playlists.py`, above `load_playlist_specs`:

```python
def _pipe_list(raw: str | None) -> list[str]:
    """A pipe-separated override cell as a list, trimmed, blanks dropped."""
    return [t.strip() for t in (raw or "").split("|") if t.strip()]
```

In `load_playlist_specs`, the no-file fallback becomes:

```python
        return [{"label": g["tag"], "tags": [g["tag"]], "seeds": [], "exclude": [],
                 "pinned": False, "gap": g}
                for g in select_gaps(con)]
```

The row loop uses `_pipe_list`:

```python
            tags = _pipe_list(row.get("tags")) or [label]
            specs.append({"label": label, "tags": tags,
                          "seeds": _pipe_list(row.get("seeds")),
                          "exclude": _pipe_list(row.get("exclude")),
                          "pinned": label not in gaps, "gap": gaps.get(label)})
```

Delete the now-unused `raw = ...` line. Add to the docstring: "`seeds` names artists whose ListenBrainz neighbours feed this playlist's discovery (seed_candidates); `exclude` keeps artists out of this playlist only — anchors, candidates and any discovery track crediting them."

In `config.py` replace line 118:

```python
N_PLAYLISTS = 7            # cap agreed with the user: 4, raised to 7 by SJ on 2026-10-04
```

In `playlist_overrides.example.csv`, document the columns after the `tags` paragraph, in the file's comment style:

```
#   seeds  optional. Artists whose ListenBrainz neighbours feed this playlist's
#          discovery, pipe-separated — for a genre your listening history is
#          too thin to supply. A name must resolve EXACTLY on MusicBrainz or it
#          is skipped with a warning; answer it as Name=MBID. Neighbours clear
#          the same genre-share bar as every other stranger.
#   exclude optional. Artists kept out of THIS playlist entirely: never an
#          anchor, never a candidate, and any discovery track crediting them
#          (a feature included) is dropped. Other playlists are unaffected.
```

Change the header line to `label,tags,seeds,exclude`, and give one illustrative row a seeds value. Use `shoegaze,shoegaze|dream pop,Slowdive|Ride,`. Update `# At most N_PLAYLISTS rows are used (config.py, default 4).` to `default 7`.

- [ ] **Step 4: Run, expect PASS:** `.venv/bin/python tests/test_playlist_overrides.py` and `.venv/bin/python tests/test_playlist_selection.py`
- [ ] **Step 5: Run the full suite:** `for t in tests/test_*.py; do .venv/bin/python "$t" || break; done`. Every script must pass. `grep -n "N_PLAYLISTS\|, 4)" tests/*.py` finds only the two sites Step 1 already fixed; if another count assertion fails, widen its fixture past 7 the same way rather than asserting on fewer rows than the cap.
- [ ] **Step 6: Commit** `config.py playlists.py playlist_overrides.example.csv tests/test_playlist_overrides.py tests/test_playlist_selection.py` with message `Stage 8: seeds/exclude override columns; cap 4 -> 7`.

---

### Task 2: Exclude: anchors, candidates, discovery tracks

**Files:**
- Modify: `playlists.py` — `select_anchor_tracks`, `select_run_anchors`, `select_candidates`, a new `drop_excluded`, and `build_selections`
- Create: `tests/test_playlist_exclude.py`

**Interfaces:**
- Consumes: `spec["exclude"]` (Task 1).
- Produces:
  - `select_anchor_tracks(con, tags, taken=None, exclude=()) -> list[dict]`
  - `select_candidates(con, tags, tag_cache, exclude=()) -> list[dict]`
  - `drop_excluded(tracks: list[dict], exclude) -> list[dict]`
  - `register_norm_name(con)`, which registers the SQL UDF `norm_name(VARCHAR) -> VARCHAR` = `enrich.normalise`
  - `_serving_mbids(con, mbids: list[str], tags: list[str], tag_cache: dict) -> set[str]`, extracted from `select_candidates` for Task 3 to reuse

- [ ] **Step 1: Write the failing test** `tests/test_playlist_exclude.py`. Copy the temp-dir/env header and the `check` helper from `tests/test_discovery_lead.py`, then:

```python
con = duckdb.connect()
# plays: recent listening; month inside the anchor window of the horizon.
con.execute("""CREATE TABLE plays AS SELECT * FROM (VALUES
  ('Drake',       'Passionfruit',          'spotify:track:d1', 9000.0, DATE '2026-08-01', false),
  ('Daniel Caesar','Get You',              'spotify:track:c1', 3000.0, DATE '2026-08-01', false),
  ('Halsey',      'Colors - Drake Remix',  'spotify:track:h1', 5000.0, DATE '2026-08-01', false)
) t(artist_name, track_name, spotify_track_uri, played_seconds, month, ms_played_estimated)""")
con.execute("""CREATE TABLE artist_tags AS SELECT * FROM (VALUES
  ('Drake', 'm-drake', 'alternative r&b', 5, true, 'musicbrainz'),
  ('Daniel Caesar', 'm-dc', 'neo soul', 5, true, 'musicbrainz')
) t(artist_name, mbid, tag, tag_count, is_genre, source)""")
```

`config.ANALYSIS_HORIZON_SQL` is `(SELECT max(month) FROM plays WHERE NOT ms_played_estimated)`, which is why the fixture has that column. The first check below (both artists anchor when nothing is excluded) proves the fixture is valid. If it fails, fix the fixture, not the code.

```python
tags = ["neo soul", "alternative r&b"]
got = {a["judge"] for a in playlists.select_anchor_tracks(con, tags)}
check("fixture valid: both anchor without exclude", got, {"Drake", "Daniel Caesar"})

got = {a["judge"] for a in playlists.select_anchor_tracks(con, tags, exclude=["drake"])}
check("excluded album artist never anchors (case-folded)", got, {"Daniel Caesar"})

got = [a["track_name"] for a in playlists.select_anchor_tracks(con, tags, exclude=["Drake"])]
check("a remix JUDGED as the excluded artist never anchors",
      "Colors - Drake Remix" in got, False)

specs = [{"label": "vs", "tags": tags, "exclude": ["Drake"]}]
got = {a["judge"] for a in playlists.select_run_anchors(con, specs)[0]}
check("select_run_anchors passes the spec's exclude", got, {"Daniel Caesar"})

# Candidates: Stage 5 recommendations + candidate tag cache.
con.execute("""CREATE TABLE recommendations AS SELECT * FROM (VALUES
  ('NAV', 'm-nav', 0.9), ('Brent Faiyaz', 'm-bf', 0.8)
) t(artist_name, mbid, score)""")
tag_cache = {"m-nav": {"tags": [{"tag": "alternative r&b", "count": 3}]},
             "m-bf":  {"tags": [{"tag": "alternative r&b", "count": 3}]}}
got = [c["artist_name"] for c in playlists.select_candidates(con, tags, tag_cache, exclude=["NAV"])]
check("excluded candidate dropped", got, ["Brent Faiyaz"])

tracks = [
  {"track_name": "Clouded", "artists": [{"name": "Brent Faiyaz", "id": "b"}]},
  {"track_name": "Wasting Time", "artists": [{"name": "Brent Faiyaz", "id": "b"},
                                             {"name": "Drake", "id": "d"}]},
]
got = [t["track_name"] for t in playlists.drop_excluded(tracks, ["drake"])]
check("a discovery track featuring an excluded artist is dropped", got, ["Clouded"])
check("empty exclude is a no-op", len(playlists.drop_excluded(tracks, [])), 2)

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
```

- [ ] **Step 2: Run, expect FAIL** (`unexpected keyword argument 'exclude'`): `.venv/bin/python tests/test_playlist_exclude.py`

- [ ] **Step 3: Implement** in `playlists.py`.

Next to `register_song_key`:

```python
def register_norm_name(con: duckdb.DuckDBPyConnection) -> None:
    """norm_name(name) in SQL — enrich.normalise itself, so an exclude row
    matches an artist exactly as every other name comparison here does."""
    _register_udf(con, "norm_name", normalise)
```

`select_anchor_tracks`: add the parameter `exclude=()` and call `register_norm_name(con)` alongside the other two registrations. Extend the `fresh` CTE's WHERE clause:

```sql
            WHERE NOT list_contains(?::VARCHAR[][], [j.judge, j.song_key])
              AND NOT list_contains(?::VARCHAR[], norm_name(j.judge))
              AND NOT list_contains(?::VARCHAR[], coalesce(norm_name(j.artist_name), ''))
```

The `coalesce` is load-bearing: `list_contains(list, NULL)` is NULL in DuckDB even for an empty list (verified), so `NOT ...` would silently drop every row with a NULL album artist whether or not anything is excluded. `j.judge` needs none — it is joined to `serving`, so it is never NULL. Today no `plays` row has a NULL `artist_name` and a non-NULL URI, so this guards the "empty exclude is a no-op" promise rather than a live row.

Bind order follows placeholder position in the text — the `serving` CTE (tags twice) precedes `fresh` (taken, excl, excl); verified against the current query — so extend the parameter list to:

```python
        excl = sorted({normalise(n) for n in exclude})
        ...
        [*tags, *tags, sorted([list(k) for k in taken or ()]), excl, excl],
```

Add one docstring paragraph: "EXCLUDE. A spec's `exclude` names artists kept out of this playlist. Both the judge and the album artist are tested, so a remix judged as an excluded remixer goes too. Filtering happens BEFORE the per-artist cap and LIMIT, so an excluded artist's slots pass to someone else rather than shrinking the playlist."

`select_run_anchors`: `anchors = select_anchor_tracks(con, spec["tags"], taken, spec.get("exclude", ()))`.

Extract the serving bar from `select_candidates`, keeping the code that is there today:

```python
def _serving_mbids(con: duckdb.DuckDBPyConnection, mbids: list[str],
                   tags: list[str], tag_cache: dict) -> set[str]:
    """Which of `mbids` serve `tags`, judged by serving_sql over the candidate
    tag cache — the one bar every stranger clears, Stage 5's and seeded alike."""
    con.execute("CREATE OR REPLACE TEMP TABLE _candidate_tags "
                "(mbid VARCHAR, tag VARCHAR, tag_count INTEGER)")
    vectors = {(m, t.get("tag"), t.get("count"))
               for m in mbids for t in (tag_cache.get(m) or {}).get("tags", [])}
    if vectors:
        con.executemany("INSERT INTO _candidate_tags VALUES (?, ?, ?)", list(vectors))
    return {r[0] for r in con.execute(
        serving_sql("_candidate_tags", "mbid", len(tags)), [*tags, *tags]
    ).fetchall()}
```

`select_candidates(con, tags, tag_cache, exclude=())` then becomes:

```python
    if not tags:
        return []
    excl = {normalise(n) for n in exclude}
    known = {normalise(r[0]) for r in con.execute(
        "SELECT DISTINCT artist_name FROM plays WHERE artist_name IS NOT NULL"
    ).fetchall()}
    rows = con.execute(
        "SELECT artist_name, mbid, score FROM recommendations ORDER BY score DESC"
    ).fetchall()
    serving = _serving_mbids(con, [m for _, m, _ in rows], tags, tag_cache)
    return [{"artist_name": name, "mbid": mbid, "score": score}
            for name, mbid, score in rows
            if normalise(name) not in known and normalise(name) not in excl
            and mbid in serving]
```

New helper, near `discovery_eligible`:

```python
def drop_excluded(tracks: list[dict], exclude) -> list[dict]:
    """Drop any track whose Spotify credit list names an excluded artist.

    The whole credit list, not just the lead: an exclude row says "not in this
    playlist", and a Drake feature on someone else's record is still Drake.
    """
    excl = {normalise(n) for n in exclude}
    if not excl:
        return list(tracks)
    return [t for t in tracks
            if not any(normalise(a.get("name") or "") in excl
                       for a in t.get("artists") or [])]
```

In `build_selections`:

```python
            tracks = drop_excluded(
                discovery_eligible(candidate_led(tracks, name, pinned_id), name),
                spec.get("exclude", ()))
```

Also pass `spec.get("exclude", ())` into the `select_candidates` call. Task 3 replaces that call site, so keep the edit minimal.

- [ ] **Step 4: Run, expect PASS:** `.venv/bin/python tests/test_playlist_exclude.py`
- [ ] **Step 5: Run the full suite.** All tests must pass, especially `test_playlist_selection.py`, `test_discovery_lead.py` and `test_anchor_judge.py`, which cover the functions just touched.
- [ ] **Step 6: Commit** `playlists.py tests/test_playlist_exclude.py` with message `Stage 8: per-playlist exclude (anchors, candidates, credited features)`.

---

### Task 3: Seeded discovery candidates

**Files:**
- Modify: `config.py` (Stage 8 block): add `PLAYLIST_SEED_CANDIDATES`
- Modify: `playlists.py`: new `PLAYLIST_SEED_CACHE`, `resolve_seed`, `seed_candidates`, and wiring in `build_selections`
- Create: `tests/test_playlist_seeds.py`

**Interfaces:**
- Consumes: `spec["seeds"]`, `spec["exclude"]` (Task 1); `_serving_mbids` (Task 2); `recommend.fetch_similar(http, mbid, cache)`, `recommend.fetch_candidate_tags(http, mbid, vocab, cache)`, `recommend.SIMILAR_CACHE`; `enrich.resolve_via_musicbrainz(http, name)` (returns a dict with `status` in `resolved|ambiguous|not_found|error`, `mbid`, `artist_name`), `enrich.load_cache()` (keyed on the raw name), `enrich.load_overrides()` (keyed on `normalise(name)`, values `{"mbid", "ignore", "none", "tags", "note"}`), `enrich.load_genre_vocabulary(http)`, `enrich.MBID_RE`.
- Produces:
  - `resolve_seed(con, http, seed: str, stage2: dict, mine: dict, overrides: dict | None = None) -> dict | None`. Returns `{"artist_name", "mbid"}`, or `None` after printing a warning.
  - `seed_candidates(con, http, spec, *, sim_cache, tag_cache, stage2, mine, vocab, known, overrides=None) -> list[dict]`. Returns `[{"artist_name", "mbid", "score"}]` in rank order, already through the serving bar.

**Seed syntax.** A cell is `Name` or `Name=MBID`. This refines the spec's "a name or a UUID": a bare UUID would leave Spotify search with no name to search. `Name=MBID` uses the MBID as given and costs no resolution.

- [ ] **Step 1: Write the failing test** `tests/test_playlist_seeds.py`. Use the same header as Task 2. No HTTP fake is needed: `fetch_similar`, `fetch_candidate_tags` and `resolve_via_musicbrainz` are already pinned offline by `tests/test_recommend_cache.py` and Stage 2's tests, so this test monkeypatches the three module-level names on `playlists` and passes `http=None`; any path that reaches them with an unexpected name raises `KeyError` and fails loudly.

```python
con = duckdb.connect()
con.execute("""CREATE TABLE plays AS SELECT * FROM (VALUES ('Joji','Glimpse of Us','u1')
) t(artist_name, track_name, spotify_track_uri)""")
con.execute("""CREATE TABLE artist_tags AS SELECT * FROM (VALUES
  ('Joji', 'm-joji', 'lo-fi hip hop', 3, true, 'musicbrainz')
) t(artist_name, mbid, tag, tag_count, is_genre, source)""")
known = {playlists.normalise("Joji")}

asked = []
def fake_resolve(http, name):
    asked.append(name)
    return {"FKJ": {"artist_name": "FKJ", "status": "ambiguous", "mbid": None},
            "Nujabes": {"artist_name": "Nujabes", "status": "resolved", "mbid": "m-nuj"},
            "Flaky": {"artist_name": "Flaky", "status": "error", "mbid": None},
            }[name]
playlists.resolve_via_musicbrainz = fake_resolve

out = io.StringIO()
with contextlib.redirect_stdout(out):
    r_lib = playlists.resolve_seed(con, None, "joji", {}, {})
    r_pin = playlists.resolve_seed(con, None, "keshi=0a1b2c3d-0000-4000-8000-000000000000", {}, {})
    mine = {}
    r_amb = playlists.resolve_seed(con, None, "FKJ", {}, mine)
    r_mb  = playlists.resolve_seed(con, None, "Nujabes", {}, mine)
    r_err = playlists.resolve_seed(con, None, "Flaky", {}, mine)
    asked_before = len(asked)
    r_mb2 = playlists.resolve_seed(con, None, "Nujabes", {}, mine)
    # artist_overrides.csv outranks every cache. Neither name is in fake_resolve,
    # so a wrongly spent search fails loudly (KeyError) rather than passing.
    ov = {"plat": {"mbid": None, "ignore": False, "none": True, "tags": [], "note": ""},
          "wale": {"mbid": "ab2528dd-0000-4000-8000-000000000000", "ignore": False,
                   "none": False, "tags": [], "note": ""},
          "tagsonly": {"mbid": None, "ignore": False, "none": False,
                       "tags": ["ambient"], "note": ""}}
    r_none = playlists.resolve_seed(
        con, None, "PLAT.", {"PLAT.": {"artist_name": "PLAT.", "status": "resolved",
                                       "mbid": "m-wrong"}}, {}, overrides=ov)
    r_ov = playlists.resolve_seed(con, None, "Wale", {}, {}, overrides=ov)
    r_tags_only = playlists.resolve_seed(
        con, None, "TagsOnly", {"TagsOnly": {"artist_name": "TagsOnly",
                                             "status": "resolved", "mbid": "m-to"}},
        {}, overrides=ov)
check("library seed resolves from artist_tags, case-folded",
      r_lib, {"artist_name": "Joji", "mbid": "m-joji"})
check("a NONE in artist_overrides.csv beats Stage 2's raw cache (PLAT.)", r_none, None)
check("...and says so", "PLAT." in out.getvalue() and "artist_overrides" in out.getvalue(), True)
check("an MBID in artist_overrides.csv is used, no search spent",
      r_ov, {"artist_name": "Wale", "mbid": "ab2528dd-0000-4000-8000-000000000000"})
check("a tags-only override row leaves resolution alone",
      r_tags_only, {"artist_name": "TagsOnly", "mbid": "m-to"})
check("override paths spend no search", len(asked), asked_before)
check("Name=MBID used as given",
      r_pin, {"artist_name": "keshi", "mbid": "0a1b2c3d-0000-4000-8000-000000000000"})
check("ambiguous seed skipped, not guessed", r_amb, None)
check("warning names the seed and the remedy",
      "FKJ" in out.getvalue() and "Name=MBID" in out.getvalue(), True)
check("resolved seed from MusicBrainz", r_mb, {"artist_name": "Nujabes", "mbid": "m-nuj"})
check("error is not cached", "Flaky" in mine, False)
check("an answered seed is not asked twice", len(asked), asked_before)
check("ambiguous IS cached (it is an answer)", mine.get("FKJ", {}).get("status"), "ambiguous")
check("seed cache file written for answers only",
      sorted(json.loads(l)["artist_name"]
             for l in playlists.PLAYLIST_SEED_CACHE.read_text().splitlines()),
      ["FKJ", "Nujabes"])

# --- pooling ---------------------------------------------------------------
# A real UUID: "Dead=m-dead" would be refused at the Name=MBID check
# (enrich.MBID_RE, verified) and the no-neighbours path never run.
DEAD = "00000000-0000-4000-8000-00000000dead"
sim = {
  "m-joji": [{"mbid": "m-hub", "name": "Hub", "score": 4000.0},
             {"mbid": "m-a", "name": "Alpha", "score": 2000.0},
             {"mbid": "m-drake", "name": "Drake", "score": 3000.0}],
  "m-nuj":  [{"mbid": "m-b", "name": "Beta", "score": 180.0},
             {"mbid": "m-off", "name": "OffGenre", "score": 170.0}],
  DEAD: [],
}
playlists.fetch_similar = lambda http, mbid, cache: sim[mbid]
tags = {"m-hub": [{"tag": "lo-fi hip hop", "count": 4}],
        "m-a":   [{"tag": "lo-fi hip hop", "count": 2}],
        "m-b":   [{"tag": "trip hop", "count": 3}],
        "m-off": [{"tag": "lo-fi hip hop", "count": 1}, {"tag": "metalcore", "count": 30}],
        "m-nuj": [{"tag": "lo-fi hip hop", "count": 5}]}
tag_cache = {}
def fake_tags(http, mbid, vocab, cache):
    cache[mbid] = {"mbid": mbid, "tags": tags.get(mbid, []), "status": 200}
    return cache[mbid]["tags"]
playlists.fetch_candidate_tags = fake_tags

spec = {"label": "lo-fi", "tags": ["lo-fi hip hop", "trip hop"],
        "seeds": ["Joji", "Nujabes"], "exclude": ["Drake"]}
mine = {"Nujabes": {"artist_name": "Nujabes", "status": "resolved", "mbid": "m-nuj"}}
got = playlists.seed_candidates(con, None, spec, sim_cache={}, tag_cache=tag_cache,
                                stage2={}, mine=mine, vocab=set(), known=known)
names = [c["artist_name"] for c in got]
check("unheard seed is itself a candidate, ranked first", names[0], "Nujabes")
check("library seed is never a discovery candidate", "Joji" in names, False)
check("excluded neighbour never pooled", "Drake" in names, False)
check("off-genre neighbour refused by the same share bar", "OffGenre" in names, False)
check("per-seed normalisation: Nujabes' best (180) ties Joji's hub (4000)",
      {c["artist_name"]: round(c["score"], 6) for c in got}["Beta"],
      {c["artist_name"]: round(c["score"], 6) for c in got}["Hub"])

spec2 = dict(spec, seeds=["Joji", f"Dead={DEAD}"])
out2 = io.StringIO()
with contextlib.redirect_stdout(out2):
    got2 = playlists.seed_candidates(con, None, spec2, sim_cache={}, tag_cache=tag_cache,
                                     stage2={}, mine={}, vocab=set(), known=known)
check("a seed with no neighbours does not sink the rest",
      [c["artist_name"] for c in got2][:2], ["Hub", "Alpha"])
check("the unheard, untagged seed is refused by the bar, not offered",
      "Dead" in [c["artist_name"] for c in got2], False)
check("...and both facts are said", "no neighbours" in out2.getvalue()
      and "do not serve" in out2.getvalue(), True)
check("no seeds -> []", playlists.seed_candidates(
      con, None, dict(spec, seeds=[]), sim_cache={}, tag_cache={}, stage2={},
      mine={}, vocab=set(), known=known), [])
```

In the last fixture, Dead resolves by its pinned UUID, is unheard so it enters the pool at `top`, has 0 neighbours (warned, skipped), and `fake_tags` gives it no tags, so `_serving_mbids` refuses it and the "do not serve" warning names it. Joji's neighbours still arrive; Drake is still excluded (`dict(spec, ...)` keeps `exclude`).

- [ ] **Step 2: Run, expect FAIL** (`no attribute 'resolve_seed'`): `.venv/bin/python tests/test_playlist_seeds.py`

- [ ] **Step 3: Implement.**

`config.py`, Stage 8 block, after `ANCHOR_WINDOW_MONTHS`:

```python
# Seeded discovery (playlist_overrides.csv `seeds`): how many pooled ListenBrainz
# neighbours per playlist get a MusicBrainz genre lookup (1.1 s each, cached)
# before the share bar. Bounds a first run at ~45 s of tag lookups per playlist.
PLAYLIST_SEED_CANDIDATES = 40
```

`playlists.py` imports: extend `from enrich import ...` with `MBID_RE, load_cache, load_genre_vocabulary, load_overrides, resolve_via_musicbrainz`, and `from recommend import ...` with `CANDIDATE_TAG_CACHE, SIMILAR_CACHE, fetch_candidate_tags, fetch_similar`. Module-level names are what the test monkeypatches, so call them unqualified (`from x import y` binds `playlists.y`; rebinding `playlists.fetch_similar` is seen by code that looks the global up at call time, which unqualified calls do). While on that line, `build_selections`' `load_jsonl(config.CACHE_DIR / "candidate_tags.jsonl", "mbid")` becomes `load_jsonl(CANDIDATE_TAG_CACHE, "mbid")` — it is a re-spelling of Stage 5's path, and this stage now writes through that same constant via `fetch_candidate_tags`.

Cache constant, beside `GENRE_RECORDINGS_CACHE`:

```python
# Seed-name resolutions this stage asked MusicBrainz for, the way Stage 9 keeps
# its own (consolidate.CONSOLIDATE_CACHE): Stage 2's cache is READ first, free,
# but never written from here — a seed is not a library artist and must not
# appear in artist_tags. Answers only; an "error" is never written.
PLAYLIST_SEED_CACHE = config.CACHE_DIR / "playlist_seed_resolution.jsonl"
```

```python
def resolve_seed(con: duckdb.DuckDBPyConnection, http, seed: str,
                 stage2: dict, mine: dict,
                 overrides: dict | None = None) -> dict | None:
    """A seed cell -> {"artist_name", "mbid"}, or None. Never guesses.

    `Name=MBID` is used as given. Otherwise, in order: artist_overrides.csv
    (an MBID row is the answer; IGNORE/NONE is a refusal), the library
    (artist_tags, matched through normalise), Stage 2's cache, this stage's
    cache, then MusicBrainz. The override file comes before every cache for
    the reason consolidate.resolve_missing gives: Stage 2's raw cache still
    holds whatever the search matched before a NONE row was written — PLAT.
    sat on the vaporwave ＰＬＡＴ — and the MBID is what seeds ListenBrainz. A
    tags-only override row leaves resolution alone, as it does in Stage 2.
    Only status "resolved" with an MBID counts — the Henrik lesson: a wrong
    artist invents listening in genres never played, which is worse than none.
    A library name with no MBID carries hand tags under NONE and is not
    searched again.
    """
    name, _, pinned = (p.strip() for p in seed.partition("="))
    if pinned:
        if MBID_RE.fullmatch(pinned.lower()):
            return {"artist_name": name, "mbid": pinned.lower()}
        print(f"  ⚠ seed {seed!r}: {pinned!r} is not an MBID — skipped")
        return None
    ov = (overrides or {}).get(normalise(name))
    if ov is not None:
        if ov.get("mbid"):
            return {"artist_name": name, "mbid": ov["mbid"]}
        if ov.get("ignore") or ov.get("none"):
            print(f"  ⚠ seed {name!r} is answered "
                  f"{'IGNORE' if ov.get('ignore') else 'NONE'} in "
                  f"{config.ARTIST_OVERRIDES_CSV.name} — skipped. Give it as "
                  "Name=MBID if MusicBrainz has since gained an entry.")
            return None
    register_norm_name(con)
    row = con.execute(
        "SELECT artist_name, max(mbid) FROM artist_tags "
        "WHERE norm_name(artist_name) = ? GROUP BY 1 ORDER BY 1 LIMIT 1",
        [normalise(name)]).fetchone()
    if row is not None:
        if row[1]:
            return {"artist_name": row[0], "mbid": row[1]}
        print(f"  ⚠ seed {name!r} is a library artist with no MusicBrainz id "
              "(answered NONE in artist_overrides.csv) — give it as Name=MBID")
        return None
    rec = stage2.get(name) or mine.get(name)
    if rec is None or rec.get("status") == "error":
        rec = resolve_via_musicbrainz(http, name)
        if rec.get("status") != "error":
            append_jsonl(PLAYLIST_SEED_CACHE, rec)
            mine[name] = rec
    if rec.get("status") == "resolved" and rec.get("mbid"):
        return {"artist_name": name, "mbid": rec["mbid"]}
    print(f"  ⚠ seed {name!r} did not resolve exactly on MusicBrainz "
          f"({rec.get('status')}) — skipped. Answer it as Name=MBID.")
    return None
```

If `artist_tags` has no row for a name, `max(mbid)` with `GROUP BY` returns no row (verified), so the `row is None` path is right. Keep `GROUP BY 1`; `ORDER BY 1` is there because two spellings can fold alike ("DEM2", "Dem 2") and `LIMIT 1` without it is not deterministic. The real `artist_tags.parquet` has an `mbid` column, 16 names with a NULL one (the NONE-with-tags rows), and no row at all for an artist that resolved but carries no tags — that case falls through to Stage 2's cache and resolves free.

```python
def seed_candidates(con: duckdb.DuckDBPyConnection, http, spec: dict, *,
                    sim_cache: dict, tag_cache: dict, stage2: dict, mine: dict,
                    vocab: set[str], known: set[str],
                    overrides: dict | None = None) -> list[dict]:
    """Strangers near the artists a person named for this playlist.

    Stage 5 seeds on the whole taste vector, dominated by workout listening, so
    a genre the listener WANTS but barely plays (vaporwave: ~0 h) gets no
    supply from it. A seed is the human's statement of where the playlist
    should go; ListenBrainz supplies who is near it.

    Similarity is normalised per seed before pooling — ListenBrainz scores share
    no scale (Skrillex tops out at 3955, REAPER at 181), the rule Stage 10's
    discovery_candidates learned. An unheard seed is itself a candidate,
    ranked above every neighbour; a library seed only feeds neighbours (library
    artists are anchors, never discovery). Excluded names never enter the pool.
    Everything then clears _serving_mbids — the same bar as every stranger.
    """
    excl = {normalise(n) for n in spec.get("exclude", [])}
    seeds = [r for s in spec.get("seeds", [])
             if (r := resolve_seed(con, http, s, stage2, mine, overrides))]
    if not seeds:
        return []
    top = float(len(seeds)) + 1.0     # above any pooled sum (each seed adds <= 1)
    pooled: dict[str, dict] = {}
    for seed in seeds:
        key = normalise(seed["artist_name"])
        if key not in known and key not in excl:
            pooled[seed["mbid"]] = {"artist_name": seed["artist_name"],
                                    "mbid": seed["mbid"], "score": top}
    for seed in seeds:
        similar = fetch_similar(http, seed["mbid"], sim_cache)
        if not similar:
            print(f"  ⚠ ListenBrainz has no neighbours for seed "
                  f"{seed['artist_name']!r} — it adds nothing this run")
            continue
        best = max(float(x.get("score") or 0) for x in similar) or 1.0
        for sim in similar:
            key = normalise(sim.get("name") or "")
            if not sim.get("mbid") or key in known or key in excl:
                continue
            row = pooled.setdefault(sim["mbid"], {
                "artist_name": sim["name"], "mbid": sim["mbid"], "score": 0.0})
            row["score"] += float(sim.get("score") or 0) / best
    ranked = sorted(pooled.values(),
                    key=lambda r: (-r["score"], r["mbid"]))[:config.PLAYLIST_SEED_CANDIDATES]
    for cand in ranked:
        fetch_candidate_tags(http, cand["mbid"], vocab, tag_cache)
    serving = _serving_mbids(con, [c["mbid"] for c in ranked], spec["tags"], tag_cache)
    refused = [s["artist_name"] for s in seeds
               if s["mbid"] in pooled and s["mbid"] not in serving]
    if refused:
        print(f"  ⚠ seed(s) {', '.join(refused)} do not serve "
              f"{' + '.join(spec['tags'])} by MusicBrainz's tags — not offered")
    return [c for c in ranked if c["mbid"] in serving]
```

Wiring in `build_selections`. `specs = load_playlist_specs(con)` already sits just before the loop (line ~1002) and the loop header is already `for spec, anchors in zip(specs, select_run_anchors(con, specs)):` — leave both. Directly after the `specs = ...` line add:

```python
    sim_cache = load_jsonl(SIMILAR_CACHE, "seed_mbid")
    seed_cache = load_jsonl(PLAYLIST_SEED_CACHE, "artist_name")
    stage2 = load_cache()
    # The genre vocabulary and artist_overrides.csv are read only when a seed
    # needs them: the vocabulary is one cached file (fetched once), and the
    # override file is personal data that a seedless run — and every existing
    # test, which never points SPOTIFY_ARTIST_OVERRIDES at a scratch dir — has
    # no business opening.
    seeded_any = any(s.get("seeds") for s in specs)
    vocab = load_genre_vocabulary(http) if seeded_any else set()
    overrides = load_overrides() if seeded_any else {}
    known = {normalise(r[0]) for r in con.execute(
        "SELECT DISTINCT artist_name FROM plays WHERE artist_name IS NOT NULL"
    ).fetchall()}
```

Replace the `candidates = select_candidates(...)` line and its print with:

```python
        seeded = seed_candidates(con, http, spec, sim_cache=sim_cache,
                                 tag_cache=tag_cache, stage2=stage2,
                                 mine=seed_cache, vocab=vocab, known=known,
                                 overrides=overrides)
        have = {c["mbid"] for c in seeded}
        candidates = seeded + [c for c in select_candidates(
            con, tags, tag_cache, spec.get("exclude", ())) if c["mbid"] not in have]
        print(f"  {len(candidates)} candidate artists carry these genres "
              f"({len(seeded)} from your seeds)")
```

`select_candidates` reads `tag_cache` from `candidate_tags.jsonl`, and `seed_candidates` fills that same in-memory dict, so seeded MBIDs are already tagged when Stage 5's pass runs. `vocab` must be the real vocabulary, not `set()`, whenever a seed exists: `fetch_candidate_tags` writes into the cache Stage 5 shares, and with an empty vocab its raw-tag fallback would store unfiltered tags there. Add a sentence to `build_selections`' docstring: "Seeded candidates (seed_candidates) go first — they are the human's statement — then Stage 5's, deduped on MBID."

`tests/test_discovery_lead.py` still passes untouched: its specs come from the gap fallback with `seeds: []`, so `seed_candidates` returns before any fetch, `load_genre_vocabulary`/`load_overrides` are never called against its `NoNetwork` http, and `load_cache()`/`load_jsonl(SIMILAR_CACHE, ...)` read the scratch cache dir and return `{}`.

- [ ] **Step 4: Run, expect PASS:** `.venv/bin/python tests/test_playlist_seeds.py`
- [ ] **Step 5: Run the full suite.** All must pass.
- [ ] **Step 6: Commit** `config.py playlists.py tests/test_playlist_seeds.py` with message `Stage 8: seeded discovery candidates (seed_candidates, never-guess resolution)`.

---

### Task 4: Docs, the real override file, and the dry run

**Files:**
- Modify: `CLAUDE.md`
- Modify (gitignored, do NOT stage): `playlist_overrides.csv`

- [ ] **Step 1: CLAUDE.md.**
  - In "Five places accept a human answer", extend the `playlist_overrides.csv` clause to: `answers Stage 8's "which genres deserve a playlist", and optionally per playlist its seeds (artists whose ListenBrainz neighbours feed discovery) and excludes (artists kept out of that playlist)`.
  - Add one invariant bullet after "Stage 8 anchors and strangers clear one bar":

```markdown
- **A Stage 8 seed is a direction, not a pass.** `playlist_overrides.csv`'s `seeds` exist because
  Stage 5 seeds on the whole taste vector, and a genre SJ wants but barely plays (vaporwave, ~0 h)
  gets no supply from it. `playlists.seed_candidates` pools each seed's ListenBrainz neighbours,
  normalised per seed, and every one of them, the seed included, clears the same `serving_sql` bar
  as any stranger. A seed name resolves only on an exact MusicBrainz match (`resolve_seed`); anything
  else is skipped with a warning and answered as `Name=MBID`, never guessed. `artist_overrides.csv` is
  read before any cache, as Stage 9 does: a `NONE` there refuses the seed even though Stage 2's raw
  cache still holds the wrong auto-match. Resolutions are cached in
  `.cache/playlist_seed_resolution.jsonl`, never in Stage 2's cache, because a seed is not a library
  artist. `exclude` is per playlist and artist-wide: it tests the anchor's judge and album artist, the
  candidate, and every Spotify credit on a discovery track. Drake holds most of this library's
  `alternative r&b` hours and would otherwise anchor vapor soul.
```

- [ ] **Step 2: Real override file.** Read the current `playlist_overrides.csv` first. Replace its contents with exactly:

```
label,tags,seeds,exclude
heavy metal,heavy metal|thrash metal|speed metal|groove metal,,
indie,indie pop|indie folk|folk pop,,
indie rock,indie rock,,
chill indie,bedroom pop|dream pop|indie folk|ambient pop,Men I Trust|Still Woozy|Clairo|Dayglow,
vapor soul,neo soul|alternative r&b|soul,Frank Ocean|Daniel Caesar|Brent Faiyaz|Masego|FKJ,Drake|Post Malone|NAV|Tory Lanez
vaporwave,vaporwave|chillwave|synthwave|hypnagogic pop,Tycho|Washed Out|Toro y Moi|Macintosh Plus,
lo-fi,lo-fi hip hop|trip hop,Joji|Nujabes|keshi,
```

The speed garage row is gone on purpose. Confirm it is still gitignored: `git check-ignore playlist_overrides.csv` must print the path.

- [ ] **Step 3: Full test suite**, then commit `CLAUDE.md` only, with message `Docs: Stage 8 seeds/exclude invariant`.

- [ ] **Step 4: Dry run** (network reads only; no Spotify writes):
  `.venv/bin/python playlists.py --dry-run 2>&1 | tee "$SCRATCH/stage8-dry.txt"`, where `$SCRATCH` is the session scratchpad directory the controller gives you. **Do not run without `--dry-run`.** The live write waits for SJ.
  If Spotify answers 429 `QUOTA_EXCEEDED`, stop and report it; do not retry.

- [ ] **Step 5: Report back**, without pasting the full output:
  - per playlist: anchors, candidates (seeded / total), discovery tracks, genre-matched count, total tracks;
  - every `⚠` line verbatim (unresolved seeds, refused seeds, empty neighbours);
  - which seeds cost a MusicBrainz search: expect only Nujabes, keshi, Washed Out and Macintosh Plus (Review Focus 1); anything else means the library lookup missed;
  - vapor soul's anchors by name — Drake, Post Malone, NAV and Tory Lanez must not appear as `judge` or `artist_name`;
  - the first 8 tracks of each of the four new playlists, as the report prints them;
  - confirmation that `speed garage` appears nowhere in the output.

Do not merge the branch. The controller reviews the dry run with SJ first.
