"""tests/test_capture.py — Stage 11 reads a playlist a person saves into and
renders its newest tracks into one it owns, on a recording fake.

No network. The fake records every verb, so the two properties that matter can
be asserted outright rather than trusted: the source is only ever READ (no PUT
or POST path carries a source ID, whichever way the target was resolved), and a
dry run issues nothing but GETs.
"""
import contextlib
import io
import json
import pathlib
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, ".")
import duckdb

import config
import consolidate
import playlists
from playlists import SP_API
from poll import ARTIST_SEP

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")


def raises(label, fn, why=""):
    """fn must exit the run, for the stated reason; its printout is swallowed.

    `why` is a fragment of the message, so a refusal cannot pass by failing
    earlier for some unrelated reason.
    """
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            fn()
    except SystemExit as e:
        check(label, why in str(e), True)
        return
    check(label, "no SystemExit", "SystemExit")


tmp = pathlib.Path(tempfile.mkdtemp())
config.CAPTURE_PARQUET = tmp / "capture.parquet"
config.CAPTURE_STATE_JSON = tmp / "capture_state.json"
config.PLAYLISTS_PARQUET = tmp / "playlists.parquet"
config.PLAYS_PARQUET = tmp / "no_plays_here.parquet"   # optional input: absent

import capture   # noqa: E402  (after the paths are redirected)


# --------------------------------------------------------------------------
# The fake. Pages are served by offset from a fixed list, so a re-read of the
# same source gets the same answer — as the real API does when nobody saves.
# --------------------------------------------------------------------------


def trk(uri, name="T", artists=("A",), ms=180_000):
    return {"uri": uri, "name": name, "duration_ms": ms,
            "artists": [{"name": a} for a in artists]}


def pl_item(uri, added, **kw):
    # Playlists nest the track under `item` since the Feb 2026 rename.
    return {"added_at": added, "item": trk(uri, **kw)}


def liked_item(uri, added, **kw):
    # Liked Songs still nests it under `track` (verified live 2026-09-27).
    return {"added_at": added, "track": trk(uri, **kw)}


class FakeSp:
    def __init__(self, playlists_=None, items=None, liked=None, dead=()):
        self.playlists = playlists_ or []    # [{"id","name"}] — /me/playlists
        self.items = items or {}             # pid -> [playlist item]
        self.liked = liked or []             # [liked item], newest first
        self.dead = set(dead)
        self.verbs = []
        self.created = []
        self.put_bodies = []

    @staticmethod
    def _offset(path):
        q = parse_qs(urlsplit(path).query)
        return int(q.get("offset", ["0"])[0]), q

    def _page(self, rows, path, size, base):
        off, _ = self._offset(path)
        nxt = (f"{SP_API}{base}?offset={off + size}&limit={size}"
               if off + size < len(rows) else None)
        return {"items": rows[off:off + size], "next": nxt}

    def get(self, path, params=None):
        self.verbs.append(("GET", path))
        if path.startswith("/me/tracks"):
            return self._page(self.liked, path, 50, "/me/tracks")
        if path.startswith("/me/playlists"):
            return {"items": self.playlists, "next": None}
        if path.startswith("/playlists/") and "/items" in path:
            pid = path.split("/")[2]
            return self._page(self.items.get(pid, []), path, 100,
                              f"/playlists/{pid}/items")
        if path.startswith("/playlists/"):
            pid = path.split("/")[2].split("?")[0]
            if pid in self.dead:
                return {"_status": 404, "_body": "gone"}
            return {"id": pid, "name": "whatever"}
        if path == "/me":
            return {"id": "me"}
        return {}

    def post(self, path, json):
        self.verbs.append(("POST", path))
        self.created.append(json)
        return {"id": f"new-{len(self.created)}"}

    def put(self, path, json):
        self.verbs.append(("PUT", path))
        self.put_bodies.append((path, json))
        return {"snapshot_id": "snap"}

    def writes(self):
        return [v for v in self.verbs if v[0] != "GET"]


def ts(day, sec=0):
    return f"2025-{1 + day // 28:02d}-{1 + day % 28:02d}T12:00:{sec:02d}Z"


def run(sp, sources, size=None, write=False):
    """capture.run with its report swallowed; returns what it returned."""
    con = duckdb.connect()
    with contextlib.redirect_stdout(io.StringIO()):
        return capture.run(sp, con, sources,
                           config.FRESH_SIZE if size is None else size, write)


def reset_files():
    for p in (config.CAPTURE_PARQUET, config.CAPTURE_STATE_JSON,
              config.PLAYLISTS_PARQUET):
        p.unlink(missing_ok=True)


SRC = {"id": "src-dump", "name": "My dump"}


# --------------------------------------------------------------------------
# 2. consolidate.read_playlist carries duration_ms, pages, skips a null item.
#    No test covered read_playlist before this; Stage 9 ignores the new key.
# --------------------------------------------------------------------------

sp = FakeSp(items={"p": [pl_item(f"spotify:track:{i}", ts(i), ms=1000 + i)
                         for i in range(150)]
                   + [{"added_at": ts(1), "item": None}]})
got = consolidate.read_playlist(sp, "p")
check("read_playlist follows pagination (150 tracks over two pages)", len(got), 150)
check("...asks for duration_ms in its fields list",
      "duration_ms" in sp.verbs[0][1], True)
check("...and returns it", (got[0]["duration_ms"], got[149]["duration_ms"]),
      (1000, 1149))
check("...skipping a null item rather than crashing",
      all(r["spotify_track_uri"] for r in got), True)


class ShortSp(FakeSp):
    """The second page never arrives: no response at all, not an envelope."""
    def get(self, path, params=None):
        if "offset=100" in path:
            self.verbs.append(("GET", path))
            return None
        return super().get(path, params)


raises("read_playlist refuses a read that stops answering part-way",
       lambda: consolidate.read_playlist(ShortSp(items=sp.items), "p"),
       "failed part-way")
check("...with Stage 9's keys unchanged",
      set(got[0]), {"spotify_track_uri", "track_name", "artists", "added_at",
                    "duration_ms"})


# --------------------------------------------------------------------------
# read_liked: /me/tracks, fifty a page, nested under `track`.
# --------------------------------------------------------------------------

liked = [liked_item(f"spotify:track:L{i}", f"2026-09-{27 - i // 10:02d}T10:00:00Z")
         for i in range(120)]
sp = FakeSp(liked=liked + [{"added_at": "2020-01-01T00:00:00Z", "track": None}])
got = capture.read_liked(sp)
check("read_liked pages Liked Songs to the end", len(got), 120)
check("...fifty at a time (51 answers 400 'Invalid limit')",
      "limit=50" in sp.verbs[0][1], True)
check("...on /me/tracks, the verified read path",
      {v[1].split("?")[0] for v in sp.verbs}, {"/me/tracks"})
check("...reading the `track` key, with duration",
      (got[0]["spotify_track_uri"], got[0]["duration_ms"]),
      ("spotify:track:L0", 180_000))
check("...and never writing", sp.writes(), [])


class RenamedSp(FakeSp):
    """The nesting key moved again: every row arrives under `item`."""
    def get(self, path, params=None):
        page = super().get(path, params)
        if path.startswith("/me/tracks"):
            page = dict(page, items=[{"added_at": it["added_at"],
                                      "item": it["track"]}
                                     for it in page["items"]])
        return page


raises("Liked Songs rows with no `track` key refuse, not read as empty",
       lambda: capture.read_liked(RenamedSp(liked=liked[:5])), "no 'track' key")


class FailingSp(FakeSp):
    def get(self, path, params=None):
        if "offset=50" in path:
            self.verbs.append(("GET", path))
            return {"_status": 502, "_body": "bad gateway"}
        return super().get(path, params)


raises("a Liked Songs read failing part-way refuses rather than going short",
       lambda: capture.read_liked(FailingSp(liked=liked)), "failed part-way")


class SilentSp(FakeSp):
    """Page two of Liked Songs never arrives: Spotify.get returns None on a
    network error, which is no envelope at all. Read as the end of the
    library, it snapshots the first 50 hearts as the whole of Liked Songs."""
    def get(self, path, params=None):
        if "offset=50" in path:
            self.verbs.append(("GET", path))
            return None
        return super().get(path, params)


raises("a Liked Songs read that stops answering part-way refuses, not short",
       lambda: capture.read_liked(SilentSp(liked=liked)), "failed part-way")


# --------------------------------------------------------------------------
# read_source: one row shape for both kinds; source_rank 0 = newest.
# --------------------------------------------------------------------------

items = [pl_item("spotify:track:old", "2025-01-01T00:00:00Z", artists=("X", "Y, Z")),
         {"added_at": "2025-02-01T00:00:00Z",
          "item": {"uri": "spotify:local:Artist:Album:Song:200", "name": "Song",
                   "duration_ms": 200_000, "artists": []}},
         {"added_at": "2025-03-01T00:00:00Z",
          "item": {"uri": "spotify:episode:abc", "name": "Pod",
                   "duration_ms": 3_600_000, "artists": []}},
         pl_item("spotify:track:new", "2025-04-01T00:00:00Z")]
sp = FakeSp(playlists_=[SRC], items={SRC["id"]: items},
            liked=[liked_item("spotify:track:h1", "2026-09-01T00:00:00Z"),
                   liked_item("spotify:track:h0", "2026-08-01T00:00:00Z")])
meta_p, rows_p = capture.read_source(sp, SRC["name"])
meta_l, rows_l = capture.read_source(sp, "liked")
check("a playlist source's meta", (meta_p["kind"], meta_p["id"], meta_p["name"]),
      ("playlist", SRC["id"], SRC["name"]))
check("Liked Songs' meta", (meta_l["kind"], meta_l["name"]), ("liked", "Liked Songs"))
check("local files and episodes are skipped; rows come newest first",
      [r["spotify_track_uri"] for r in rows_p],
      ["spotify:track:new", "spotify:track:old"])
check("...and are counted, not silently lost", (meta_p["n_read"], meta_p["n_skipped"]),
      (4, 2))
check("the row shape is identical across source kinds",
      (sorted(rows_p[0]), sorted(rows_l[0])),
      (sorted(capture.ROW_KEYS), sorted(capture.ROW_KEYS)))
check("track_artists is joined on 0x1f, so 'Y, Z' stays one artist",
      next(r for r in rows_p if r["spotify_track_uri"] == "spotify:track:old")
      ["track_artists"].split(ARTIST_SEP), ["X", "Y, Z"])
check("playlist source_rank: the LAST position is the newest (rank 0)",
      {r["spotify_track_uri"]: r["source_rank"] for r in rows_p},
      {"spotify:track:new": 0, "spotify:track:old": 1})
check("Liked Songs source_rank: the API's first row is the newest (rank 0)",
      {r["spotify_track_uri"]: r["source_rank"] for r in rows_l},
      {"spotify:track:h1": 0, "spotify:track:h0": 1})
check("reading sources issued only GETs", sp.writes(), [])


# --------------------------------------------------------------------------
# 6. Refusals that must happen before anything is read or requested.
# --------------------------------------------------------------------------

sp = FakeSp(playlists_=[{"id": "own", "name": "Workout · Claude"}])
raises("a ' · Claude' source is refused (the pipeline's own output)",
       lambda: capture.read_source(sp, "Workout · Claude"), "circular")
check("...before a single request", sp.verbs, [])
raises("the target's own name as a source is refused",
       lambda: capture.check_sources([config.FRESH_PLAYLIST_NAME]))
raises("--size 101 is refused (one PUT carries at most 100)",
       lambda: capture.check_size(101), "--size 101 refused")
raises("--size 0 is refused", lambda: capture.check_size(0))
check("--size 100 is accepted", capture.check_size(100), 100)
check("a repeated --source is read once", capture.check_sources(["a", "a", "liked"]),
      ["a", "liked"])

tokens = []
capture.gentle_token = lambda cid, scope: tokens.append(scope) or "tok"
raises("main refuses --size 101",
       lambda: capture.main(["--source", "My dump", "--size", "101"]), "refused")
raises("main refuses a ' · Claude' source",
       lambda: capture.main(["--source", "dubstep run · Claude"]), "circular")
check("...both before asking for a token", tokens, [])


# --------------------------------------------------------------------------
# Scopes: user-library-read only when Liked Songs is a source. A run on a
# named playlist must never prompt for it.
# --------------------------------------------------------------------------

for write in (False, True):
    check(f"named playlist, write={write}: no library scope",
          "user-library-read" in capture.scopes_for(["My dump"], write).split(), False)
    check(f"liked, write={write}: library read scope requested",
          "user-library-read" in capture.scopes_for(["My dump", "liked"], write).split(),
          True)
check("no run ever asks for user-library-modify",
      any("user-library-modify" in capture.scopes_for(s, w).split()
          for s in (["x"], ["liked"]) for w in (False, True)), False)
check("the write scopes mirror Stage 10's (both public and private modify)",
      set(capture.scopes_for(["x"], True).split()),
      {"playlist-read-private", "playlist-read-collaborative",
       "playlist-modify-private", "playlist-modify-public"})


# --------------------------------------------------------------------------
# 1. Newest first, bulk-stamp ties broken by position, capped at FRESH_SIZE.
# --------------------------------------------------------------------------

reset_files()
dump = [pl_item(f"spotify:track:{i:03d}", ts(i)) for i in range(150)]
# Three tracks pasted in one go share a second, as the 2025-01-23/24 bulk
# stamps do: the later POSITION is the later add.
for i in (140, 141, 142):
    dump[i] = pl_item(f"spotify:track:{i:03d}", ts(140))
sp = FakeSp(playlists_=[SRC], items={SRC["id"]: dump})
out = run(sp, [SRC["name"]])
sel = [t["spotify_track_uri"] for t in out["fresh"]]
check("selection is capped at FRESH_SIZE", len(sel), config.FRESH_SIZE)
check("...newest first",
      sel[:10], [f"spotify:track:{i:03d}" for i in
                 (149, 148, 147, 146, 145, 144, 143, 142, 141, 140)])
check("...a same-second tie resolved by source_rank (later position first)",
      sel[7:10], ["spotify:track:142", "spotify:track:141", "spotify:track:140"])
check("...ending at the 100th newest", sel[-1], "spotify:track:050")
check("positions run 0..99", [t["position"] for t in out["fresh"]],
      list(range(config.FRESH_SIZE)))


# --------------------------------------------------------------------------
# 4. A dry run issues zero non-GET verbs and writes no state or archive —
#    only capture.parquet, which is the point of reading.
# --------------------------------------------------------------------------

check("dry run: zero non-GET verbs", sp.writes(), [])
check("dry run: capture.parquet written", config.CAPTURE_PARQUET.exists(), True)
check("dry run: no state file", config.CAPTURE_STATE_JSON.exists(), False)
check("dry run: no archive rows", config.PLAYLISTS_PARQUET.exists(), False)
n = duckdb.sql(f"SELECT count(*) FROM '{config.CAPTURE_PARQUET}'").fetchone()[0]
check("capture.parquet holds the whole source, not just the selection", n, 150)


# --------------------------------------------------------------------------
# 3. Two sources dedupe by URI on the NEWEST added_at.
# --------------------------------------------------------------------------

reset_files()
both = FakeSp(
    playlists_=[SRC],
    items={SRC["id"]: [pl_item("spotify:track:shared", "2025-06-01T00:00:00Z"),
                       pl_item("spotify:track:only-pl", "2025-05-01T00:00:00Z")]},
    liked=[liked_item("spotify:track:shared", "2026-01-01T00:00:00Z"),
           liked_item("spotify:track:only-liked", "2024-01-01T00:00:00Z")])
run(both, [SRC["name"], "liked"])
rows = duckdb.sql(f"SELECT spotify_track_uri, source, strftime(added_at, '%Y-%m-%d') "
                  f"FROM '{config.CAPTURE_PARQUET}' ORDER BY 1").fetchall()
check("union of two sources, one row per URI, newest added_at wins", rows,
      [("spotify:track:only-liked", "liked", "2024-01-01"),
       ("spotify:track:only-pl", SRC["name"], "2025-05-01"),
       ("spotify:track:shared", "liked", "2026-01-01")])
cols = [r[0] for r in duckdb.sql(
    f"DESCRIBE SELECT * FROM '{config.CAPTURE_PARQUET}'").fetchall()]
check("capture.parquet columns", cols, list(capture.ROW_KEYS))

# A full snapshot, not an append-only cache: a save removed at the source
# disappears on the next read.
both.liked = both.liked[:1]
run(both, [SRC["name"], "liked"])
left = [r[0] for r in duckdb.sql(
    f"SELECT spotify_track_uri FROM '{config.CAPTURE_PARQUET}' ORDER BY 1").fetchall()]
check("a removed save is gone from the next snapshot", left,
      ["spotify:track:only-pl", "spotify:track:shared"])


# --------------------------------------------------------------------------
# --write: snapshot, one PUT, description; archive and state; then 7 and 5.
# --------------------------------------------------------------------------

reset_files()
sp = FakeSp(playlists_=[SRC], items={SRC["id"]: dump})
out = run(sp, [SRC["name"]], write=True)
pid = out["playlist_id"]
check("first write creates the target", (pid, len(sp.created)), ("new-1", 1))
check("...under the exact template name", sp.created[0]["name"],
      config.FRESH_PLAYLIST_NAME)
item_puts = [b for p, b in sp.put_bodies if p == f"/playlists/{pid}/items"]
check("one PUT replaces the whole playlist", len(item_puts), 1)
check("...with the 100 newest, newest first", item_puts[0]["uris"][:2],
      ["spotify:track:149", "spotify:track:148"])
check("no POST adds tracks (nothing needs the chunked path)",
      [v for v in sp.verbs if v[0] == "POST" and v[1].endswith("/items")], [])
desc = next(b for p, b in sp.put_bodies if p == f"/playlists/{pid}")
check("description names the count and hours, and claims nothing private",
      ("100 newest" in desc["description"], "5.0 h" in desc["description"],
       "private" in desc["description"].lower()), (True, True, False))
state = json.loads(config.CAPTURE_STATE_JSON.read_text(encoding="utf-8"))
check("state records the target and every source, for poll --status",
      state, {"fresh": {"id": pid, "name": config.FRESH_PLAYLIST_NAME},
              "sources": [{"kind": "playlist", "id": SRC["id"],
                           "name": SRC["name"]}]})
kinds = duckdb.sql(f"SELECT kind, gap_tag, count(*) FROM '{config.PLAYLISTS_PARQUET}' "
                   "GROUP BY ALL ORDER BY ALL").fetchall()
check("archive: fresh_selection rows under gap_tag 'fresh' (new playlist: "
      "nothing to snapshot yet)", kinds, [("fresh_selection", "fresh", 100)])
check("the source was re-read after the write and found unchanged",
      out["sources_unchanged"], True)

# 7. Second write: the stored ID is reused and the snapshot GET precedes the PUT.
sp.items[pid] = [pl_item(f"spotify:track:{i:03d}", ts(i)) for i in range(149, 49, -1)]
sp.verbs.clear()
sp.created.clear()
out = run(sp, [SRC["name"]], write=True)
check("second write reuses the stored ID: nothing POSTed",
      (out["playlist_id"], [v for v in sp.verbs if v[0] == "POST"]), (pid, []))
snap_get = next(i for i, v in enumerate(sp.verbs)
                if v == ("GET", v[1]) and v[1].startswith(f"/playlists/{pid}/items"))
item_put = sp.verbs.index(("PUT", f"/playlists/{pid}/items"))
check("the target's snapshot GET precedes its PUT", snap_get < item_put, True)
kinds = dict(((k, n) for k, _, n in duckdb.sql(
    f"SELECT kind, gap_tag, count(*) FROM '{config.PLAYLISTS_PARQUET}' "
    "GROUP BY ALL").fetchall()))
check("...and the overwritten contents land as fresh_pre_replace_snapshot",
      kinds.get("fresh_pre_replace_snapshot"), 100)

# 5. No PUT or POST ever carries a source ID — on any path above.
check("no write verb anywhere targets the source",
      [v for v in sp.writes() if SRC["id"] in v[1]], [])

# ...including when the target resolves TO a source. Stored ID first:
reset_files()
config.CAPTURE_STATE_JSON.write_text(json.dumps(
    {"fresh": {"id": SRC["id"], "name": config.FRESH_PLAYLIST_NAME}}), encoding="utf-8")
sp = FakeSp(playlists_=[SRC], items={SRC["id"]: dump})
raises("a stored target ID that is a source ID refuses",
       lambda: capture.run(sp, duckdb.connect(), [SRC["name"]], 100, True),
       "but it is a source")
check("...with no PUT or POST issued at all", sp.writes(), [])


# Then the exact-name fallback: the source was renamed to the target's name
# between being read and the write — the listing now puts it under that name.
class RenamedMidRun(FakeSp):
    def get(self, path, params=None):
        if path.startswith("/me/playlists") and any(
                v[1].startswith(f"/playlists/{SRC['id']}/items") for v in self.verbs):
            self.verbs.append(("GET", path))
            return {"items": [{"id": SRC["id"], "name": config.FRESH_PLAYLIST_NAME}],
                    "next": None}
        return super().get(path, params)


reset_files()
sp = RenamedMidRun(playlists_=[SRC], items={SRC["id"]: dump})
raises("an exact-name fallback that resolves to a source raises SystemExit",
       lambda: capture.run(sp, duckdb.connect(), [SRC["name"]], 100, True),
       "resolved to the source")
check("...with no PUT or POST targeting it",
      [v for v in sp.writes() if SRC["id"] in v[1]], [])
check("...and nothing archived or stored",
      (config.PLAYLISTS_PARQUET.exists(), config.CAPTURE_STATE_JSON.exists()),
      (False, False))

# The pre-replace snapshot must fail loudly. playlists.playlist_items answers
# [] or a partial list when a page errors, and a PUT after that overwrites a
# hand-added track that was never recorded. The target exists, holds one track
# SJ added by hand, and its /items read fails: nothing may be PUT.
class SnapshotFails(FakeSp):
    def __init__(self, answer, on, **kw):
        super().__init__(**kw)
        self.answer, self.on = answer, on

    def get(self, path, params=None):
        if path.startswith("/playlists/F/items") and self.on in path:
            self.verbs.append(("GET", path))
            return self.answer
        return super().get(path, params)


hand = [pl_item("spotify:track:hand-added", "2026-09-20T00:00:00Z")]
for label, answer, on, target in (
        ("a 502 on the snapshot read", {"_status": 502, "_body": "bad"}, "", hand),
        ("no response to the snapshot read", None, "", hand),
        # 150 tracks, page two lost: a short snapshot is as bad as none.
        ("the snapshot's second page lost", None, "offset=100",
         [pl_item(f"spotify:track:f{i}", ts(i)) for i in range(150)])):
    reset_files()
    config.CAPTURE_STATE_JSON.write_text(json.dumps(
        {"fresh": {"id": "F", "name": config.FRESH_PLAYLIST_NAME}}), encoding="utf-8")
    sp = SnapshotFails(answer, on, playlists_=[SRC],
                       items={SRC["id"]: dump, "F": target})
    raises(f"{label} refuses the replace",
           lambda: capture.run(sp, duckdb.connect(), [SRC["name"]], 100, True),
           "snapshot")
    check("...with no PUT or POST issued at all", sp.writes(), [])
    check("...and nothing archived", config.PLAYLISTS_PARQUET.exists(), False)

# An empty read must not blank the target.
reset_files()
sp = FakeSp(playlists_=[SRC], items={SRC["id"]: []})
out = run(sp, [SRC["name"]], write=True)
check("an empty source leaves the target untouched: no write verb",
      (out["playlist_id"], sp.writes()), (None, []))


# --------------------------------------------------------------------------
# 8. The client has no delete verb, and nothing here issued one.
# --------------------------------------------------------------------------

check("playlists.Spotify has no delete method", hasattr(playlists.Spotify, "delete"),
      False)
check("capture defines no delete-shaped call either",
      [n for n in dir(capture) if "delete" in n.lower() or "unfollow" in n.lower()], [])


# --------------------------------------------------------------------------
# The report's "has an export play" counts export rows only. A polled play is
# an estimate past the export's coverage and must not count.
# --------------------------------------------------------------------------

con = duckdb.connect()
con.execute("CREATE TABLE plays (spotify_track_uri VARCHAR, source_kind VARCHAR)")
con.executemany("INSERT INTO plays VALUES (?, ?)",
                [("u:a", "audio"), ("u:a", "audio"), ("u:b", "poll")])
check("export-played counts audio rows, once per track",
      capture.count_export_played(con, ["u:a", "u:b", "u:c"]), 1)


if failures:
    print(f"{len(failures)} FAILURE(S)")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("all assertions passed")
