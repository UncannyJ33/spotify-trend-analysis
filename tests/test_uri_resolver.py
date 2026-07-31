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
        self.last = params
        return {"tracks": {"items": self.items}}


def item(uri, name, *artists):
    return {"uri": uri, "name": name, "artists": [{"name": a} for a in artists]}


playlists.append_jsonl = lambda path, rec: None      # keep the test off disk

# Relevance order is preserved exactly as Spotify returned it — with
# ListenBrainz popularity dead this ordering IS the popularity signal.
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
check("search is scoped to the artist", sp.last["q"], 'artist:"Virtual Riot"')

# A search for one artist returns other people's tracks; they must be dropped.
sp = FakeSp([item("spotify:track:bad", "Virtual Riot Tribute", "Karaoke Crew"),
             item("spotify:track:ok",  "Energy Drink",         "Virtual Riot")])
check("wrong-artist hit dropped",
      [t["spotify_track_uri"] for t in playlists.sp_artist_tracks(sp, "Virtual Riot", {})],
      ["spotify:track:ok"])

# A featured credit still counts: the artist is genuinely on the track.
sp = FakeSp([item("spotify:track:f", "Collab", "Someone Else", "Virtual Riot")])
check("featured credit accepted",
      [t["spotify_track_uri"] for t in playlists.sp_artist_tracks(sp, "Virtual Riot", {})],
      ["spotify:track:f"])

# Stylisation folds the same way Stage 2 folds it: A$AP vs ASAP.
sp = FakeSp([item("spotify:track:3", "Praise the Lord", "A$AP Rocky")])
check("stylised artist name matches",
      [t["spotify_track_uri"] for t in playlists.sp_artist_tracks(sp, "ASAP Rocky", {})],
      ["spotify:track:3"])

# A hit with no URI is unplayable and must not reach a playlist.
sp = FakeSp([{"uri": None, "name": "Ghost", "artists": [{"name": "Virtual Riot"}]},
             item("spotify:track:real", "Real", "Virtual Riot")])
check("uri-less hit dropped",
      [t["spotify_track_uri"] for t in playlists.sp_artist_tracks(sp, "Virtual Riot", {})],
      ["spotify:track:real"])

# Nothing acceptable -> empty, and the miss is cached so it is asked once.
sp = FakeSp([item("spotify:track:5", "Different Song", "Someone Else")])
cache = {}
check("no usable hit returns empty",
      playlists.sp_artist_tracks(sp, "Nobody At All", cache), [])
check("miss was cached", playlists.normalise("Nobody At All") in cache, True)
playlists.sp_artist_tracks(sp, "Nobody At All", cache)
check("cached miss not re-searched", sp.calls, 1)

# A cache hit must return the same shape a live call does.
sp = FakeSp([item("spotify:track:c", "Cached", "Virtual Riot")])
cache = {}
live = playlists.sp_artist_tracks(sp, "Virtual Riot", cache)
cached = playlists.sp_artist_tracks(sp, "Virtual Riot", cache)
check("cache hit spends no request", sp.calls, 1)
check("cache hit has the same shape as a live call", cached, live)

# An error envelope from the client must not be mistaken for results.
class ErrSp:
    calls = 0
    def get(self, path, params=None):
        return {"_status": 401, "_body": "expired"}


check("error envelope yields no tracks",
      playlists.sp_artist_tracks(ErrSp(), "Virtual Riot", {}), [])


# --------------------------------------------------------------------------
# The client itself: 429 handling, error envelopes, and the missing verb.
# --------------------------------------------------------------------------

check("client has no delete method", hasattr(playlists.Spotify, "delete"), False)
check("client exposes only the verbs this stage needs",
      {v for v in ("get", "post", "put", "delete", "patch")
       if hasattr(playlists.Spotify, v)}, {"get", "post", "put"})


class _R:
    def __init__(self, status, headers=None, body='{"ok":true}'):
        self.status_code = status
        self.headers = headers or {}
        self.text = body
    def json(self):
        import json as _j
        return _j.loads(self.text)


calls = []
slept = []
playlists.time.sleep = lambda s: slept.append(s)

# 429 once, then success: the Retry-After is honoured and the call retried.
seq = [_R(429, {"Retry-After": "3"}), _R(200)]
playlists.requests.request = lambda m, u, **kw: (calls.append((m, u)), seq.pop(0))[1]
sp = playlists.Spotify("tok")
check("429 is retried", sp.get("/search"), {"ok": True})
check("Retry-After honoured", 3 in slept, True)
check("retry actually re-issued the request", len(calls), 2)

# A bad Retry-After must not hang the run.
slept.clear()
seq = [_R(429, {"Retry-After": "99999"}), _R(200)]
playlists.Spotify("tok").get("/search")
check("absurd Retry-After is capped", max(slept) <= 30, True)

# 4xx comes back as an inspectable envelope, not an exception and not None.
playlists.requests.request = lambda m, u, **kw: _R(404, body="gone")
env = playlists.Spotify("tok").get("/playlists/nope")
check("error returns an envelope", env["_status"], 404)

# A network failure returns None rather than raising into the caller.
def _boom(*a, **k):
    raise playlists.requests.RequestException("down")


playlists.requests.request = _boom
check("network failure returns None", playlists.Spotify("tok").get("/search"), None)

# An empty 204-style body must not blow up json parsing.
playlists.requests.request = lambda m, u, **kw: _R(200, body="")
check("empty body parses to {}", playlists.Spotify("tok").put("/x", json={}), {})

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
