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
    def __init__(self, existing=None, dead_ids=(), pages=None):
        self.existing = existing or []      # [{"id","name"}] user's playlists
        self.dead = set(dead_ids)
        self.pages = pages                  # optional paged /me/playlists
        self.verbs = []
        self.created = []
    def get(self, path, params=None):
        self.verbs.append(("GET", path))
        if path.startswith("/playlists/") and path.endswith("/tracks"):
            return {"items": [], "next": None}
        if path.startswith("/playlists/"):
            pid = path.split("/")[2]
            if pid in self.dead:
                return {"_status": 404, "_body": "gone"}
            return {"id": pid, "name": "whatever"}
        if path.startswith("/me/playlists"):
            if self.pages:
                return self.pages.pop(0)
            return {"items": self.existing, "next": None}
        return {}
    def post(self, path, json):
        self.verbs.append(("POST", path))
        self.created.append(json)
        return {"id": f"new-{len(self.created)}"}
    def put(self, path, json):
        self.verbs.append(("PUT", path))
        return {"snapshot_id": "snap"}


NAME = "dubstep frontier · Claude"

# 1. Stored ID that still exists: reused, nothing created
sp = FakeSp()
state = {"dubstep": {"id": "keep-me", "name": NAME}}
pid = playlists.ensure_playlist(sp, "uid", "dubstep", NAME, state)
check("stored id reused", pid, "keep-me")
check("nothing created", sp.created, [])

# 2. Stored ID deleted remotely: falls through to name-adopt
sp = FakeSp(existing=[{"id": "adopt-me", "name": NAME}], dead_ids={"keep-me"})
state = {"dubstep": {"id": "keep-me", "name": NAME}}
pid = playlists.ensure_playlist(sp, "uid", "dubstep", NAME, state)
check("dead id replaced by exact-name adoption", pid, "adopt-me")

# 3. No state, no name match: creates private with the template name
sp = FakeSp(existing=[{"id": "x", "name": "my own dubstep mix"}])
pid = playlists.ensure_playlist(sp, "uid", "dubstep", NAME, {})
check("created fresh", pid, "new-1")
check("created private", sp.created[0]["public"], False)
check("near-miss name NOT adopted",
      any(v == ("PUT", "/playlists/x/tracks") for v in sp.verbs), False)

# 3b. A name that differs only by the marker must not be adopted either.
sp = FakeSp(existing=[{"id": "y", "name": "dubstep frontier"}])
check("name without the marker NOT adopted",
      playlists.ensure_playlist(sp, "uid", "dubstep", NAME, {}), "new-1")

# 3c. Adoption is exact, so a rename by the user just makes a new one rather
#     than hijacking whatever now sits under the old name.
sp = FakeSp(existing=[{"id": "z", "name": NAME.upper()}])
check("case-different name NOT adopted",
      playlists.ensure_playlist(sp, "uid", "dubstep", NAME, {}), "new-1")

# 4. Adoption has to survive pagination — the match may be on page 2.
sp = FakeSp(pages=[
    {"items": [{"id": "p1", "name": "something else"}],
     "next": f"{playlists.SP_API}/me/playlists?offset=50"},
    {"items": [{"id": "p2", "name": NAME}], "next": None},
])
check("adoption follows pagination",
      playlists.ensure_playlist(sp, "uid", "dubstep", NAME, {}), "p2")

# 5. The invariant: no DELETE verb exists on the client at all
check("client has no delete method", hasattr(playlists.Spotify, "delete"), False)

# 6. ensure_playlist must never issue a delete-shaped call either.
sp = FakeSp(existing=[{"id": "x", "name": "unrelated"}])
playlists.ensure_playlist(sp, "uid", "dubstep", NAME, {})
check("no DELETE verb issued", [v for v in sp.verbs if v[0] == "DELETE"], [])

# 7. A create that fails must stop the run, not return something unusable.
class DeadSp(FakeSp):
    def post(self, path, json):
        return {"_status": 403, "_body": "forbidden"}


try:
    playlists.ensure_playlist(DeadSp(), "uid", "dubstep", NAME, {})
    check("failed create raises", False, True)
except SystemExit:
    check("failed create raises", True, True)


# --------------------------------------------------------------------------
# playlist_items — the pre-replace snapshot must capture what we overwrite.
# --------------------------------------------------------------------------


class ItemSp:
    def __init__(self, pages):
        self.pages = pages
        self.calls = 0
    def get(self, path, params=None):
        self.calls += 1
        return self.pages.pop(0)


def track(uri, name, *artists):
    return {"track": {"uri": uri, "name": name,
                      "artists": [{"name": a} for a in artists]}}


sp = ItemSp([{"items": [track("u1", "One", "A"), track("u2", "Two", "B", "C")],
              "next": f"{playlists.SP_API}/playlists/p/tracks?offset=100"},
             {"items": [track("u3", "Three", "D")], "next": None}])
got = playlists.playlist_items(sp, "p")
check("snapshot follows pagination", [r["uri"] for r in got], ["u1", "u2", "u3"])
check("multi-artist credit joined", got[1]["artist_name"], "B, C")
check("track name captured", got[0]["track_name"], "One")

# A local/removed track comes back as a null `track` and must not crash.
sp = ItemSp([{"items": [{"track": None}, track("u9", "Real", "E")], "next": None}])
got = playlists.playlist_items(sp, "p")
check("null track row tolerated", [r["uri"] for r in got], [None, "u9"])

# An error envelope must yield nothing rather than a partial snapshot.
class ErrSp:
    def get(self, path, params=None):
        return {"_status": 500, "_body": "boom"}


check("error yields an empty snapshot", playlists.playlist_items(ErrSp(), "p"), [])


# --------------------------------------------------------------------------
# write_archive — the playlist is a rendering; this Parquet is the record, so
# it must accumulate runs rather than replace them, and land in total order.
# --------------------------------------------------------------------------

import pathlib
import tempfile

import duckdb

tmp = pathlib.Path(tempfile.mkdtemp())
playlists.config.PLAYLISTS_PARQUET = tmp / "playlists.parquet"
con = duckdb.connect()


def row(run_date, kind, pos, uri):
    return {"run_date": run_date, "kind": kind, "gap_tag": "dubstep",
            "playlist_id": "pid", "position": pos, "slot": "anchor",
            "artist_name": "A", "track_name": "T", "spotify_track_uri": uri,
            "source": "plays"}


playlists.write_archive(con, [row("2026-07-31", "selection", 1, "u1"),
                              row("2026-07-31", "selection", 0, "u0")])
n = con.execute(f"SELECT count(*) FROM '{playlists.config.PLAYLISTS_PARQUET}'").fetchone()[0]
check("first run written", n, 2)

playlists.write_archive(con, [row("2026-08-01", "selection", 0, "u0")])
n = con.execute(f"SELECT count(*) FROM '{playlists.config.PLAYLISTS_PARQUET}'").fetchone()[0]
check("second run APPENDS, does not replace", n, 3)

order = con.execute(
    f"SELECT run_date, position FROM '{playlists.config.PLAYLISTS_PARQUET}'").fetchall()
check("stored in total order", order,
      [("2026-07-31", 0), ("2026-07-31", 1), ("2026-08-01", 0)])

# A pre-replace snapshot carries a null slot; the schema must accept it.
playlists.write_archive(con, [dict(row("2026-08-01", "pre_replace_snapshot", 0, "old"),
                                   slot=None)])
kinds = con.execute(
    f"SELECT DISTINCT kind FROM '{playlists.config.PLAYLISTS_PARQUET}' ORDER BY 1"
).fetchall()
check("snapshot rows coexist with selections", [k[0] for k in kinds],
      ["pre_replace_snapshot", "selection"])

before = con.execute(f"SELECT count(*) FROM '{playlists.config.PLAYLISTS_PARQUET}'").fetchone()[0]
playlists.write_archive(con, [])
after = con.execute(f"SELECT count(*) FROM '{playlists.config.PLAYLISTS_PARQUET}'").fetchone()[0]
check("empty write is a no-op, not a truncation", after, before)

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
