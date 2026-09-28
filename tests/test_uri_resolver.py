"""tests/test_uri_resolver.py — search result validation, not vibes.

The search is playlists.sp_artist_tracks_credited, shared by Stages 8 and 10.
What it keeps is a fact about the track; whether a stage may OFFER a hit is
tested with that stage (Stage 8: test_discovery_lead.py).
"""
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
    return {"uri": uri, "name": name, "duration_ms": 200_000,
            "artists": [{"name": a, "id": f"id:{a}"} for a in artists]}


def uris(tracks):
    return [t["spotify_track_uri"] for t in tracks]


search = playlists.sp_artist_tracks_credited
appended = []
playlists.append_jsonl = lambda path, rec: appended.append((path.name, rec))

# Relevance order is preserved exactly as Spotify returned it — with
# ListenBrainz popularity dead this ordering IS the popularity signal.
sp = FakeSp([item("spotify:track:1", "Energy Drink", "Virtual Riot"),
             item("spotify:track:2", "Idols",        "Virtual Riot")])
cache = {}
got = search(sp, "Virtual Riot", cache)
check("relevance order preserved", uris(got), ["spotify:track:1", "spotify:track:2"])
check("track names carried", [t["track_name"] for t in got],
      ["Energy Drink", "Idols"])
check("artist is the one we asked for, not the credit string",
      {t["artist_name"] for t in got}, {"Virtual Riot"})
check("search is scoped to the artist", sp.last["q"], 'artist:"Virtual Riot"')

# A search for one artist returns other people's tracks; they must be dropped.
sp = FakeSp([item("spotify:track:bad", "Virtual Riot Tribute", "Karaoke Crew"),
             item("spotify:track:ok",  "Energy Drink",         "Virtual Riot")])
check("wrong-artist hit dropped", uris(search(sp, "Virtual Riot", {})),
      ["spotify:track:ok"])

# A featured credit is KEPT here, with the credit order that says it is a
# feature: the artist is genuinely on the track. Stage 8 then refuses to offer
# it (candidate_led); keeping it is what lets that stage tell.
sp = FakeSp([item("spotify:track:f", "Collab", "Someone Else", "Virtual Riot")])
got = search(sp, "Virtual Riot", {})
check("featured credit kept by the search", uris(got), ["spotify:track:f"])
check("...with every credit, lead first",
      got[0]["artists"], [{"name": "Someone Else", "id": "id:Someone Else"},
                          {"name": "Virtual Riot", "id": "id:Virtual Riot"}])

# Stylisation folds the same way Stage 2 folds it: A$AP vs ASAP.
sp = FakeSp([item("spotify:track:3", "Praise the Lord", "A$AP Rocky")])
check("stylised artist name matches", uris(search(sp, "ASAP Rocky", {})),
      ["spotify:track:3"])

# A hit with no URI is unplayable and must not reach a playlist.
sp = FakeSp([{"uri": None, "name": "Ghost", "artists": [{"name": "Virtual Riot"}]},
             item("spotify:track:real", "Real", "Virtual Riot")])
check("uri-less hit dropped", uris(search(sp, "Virtual Riot", {})),
      ["spotify:track:real"])

# Nothing acceptable -> empty, and the miss is cached so it is asked once.
sp = FakeSp([item("spotify:track:5", "Different Song", "Someone Else")])
cache = {}
check("no usable hit returns empty", search(sp, "Nobody At All", cache), [])
check("miss was cached", playlists.normalise("Nobody At All") in cache, True)
search(sp, "Nobody At All", cache)
check("cached miss not re-searched", sp.calls, 1)

# A cache hit must return the same shape a live call does.
sp = FakeSp([item("spotify:track:c", "Cached", "Virtual Riot")])
cache = {}
live = search(sp, "Virtual Riot", cache)
cached = search(sp, "Virtual Riot", cache)
check("cache hit spends no request", sp.calls, 1)
check("cache hit has the same shape as a live call", cached, live)

# An error envelope from the client must not be mistaken for results.
class ErrSp:
    calls = 0
    def get(self, path, params=None):
        return {"_status": 401, "_body": "expired"}


check("error envelope yields no tracks", search(ErrSp(), "Virtual Riot", {}), [])


# --------------------------------------------------------------------------
# Only an ANSWER is cached. A 429 or a dropped connection used to be written
# as "this artist has no tracks", and an append-only cache never asks again.
# --------------------------------------------------------------------------

appended.clear()


class SeqSp:
    """Answers each /search with the next canned response."""
    def __init__(self, *resps):
        self.resps = list(resps)
        self.calls = 0
    def get(self, path, params=None):
        self.calls += 1
        return self.resps.pop(0)


ok_page = {"tracks": {"items": [item("spotify:track:ok", "Energy Drink", "Virtual Riot")]}}
cache = {}
sp = SeqSp({"_status": 429, "_body": "slow down"}, ok_page)
check("a 429 yields no tracks", search(sp, "Virtual Riot", cache), [])
check("...and is not cached", (appended, cache), ([], {}))
check("...so the next call re-asks, and gets the answer",
      uris(search(sp, "Virtual Riot", cache)), ["spotify:track:ok"])
check("...having asked twice", sp.calls, 2)
check("the answer is cached with its status, in the credited file",
      appended, [("spotify_artist_tracks_credited.jsonl",
                  {"key": playlists.normalise("Virtual Riot"),
                   "artist": "Virtual Riot", "status": 200,
                   "tracks": [{"track_name": "Energy Drink",
                               "spotify_track_uri": "spotify:track:ok",
                               "duration_ms": 200_000,
                               "artists": [{"name": "Virtual Riot",
                                            "id": "id:Virtual Riot"}]}]})])

appended.clear()
sp = SeqSp(None)
check("no response at all yields no tracks, and caches nothing",
      (search(sp, "Virtual Riot", {}), appended), ([], []))

# An empty 200 is an answer: Spotify does not carry them. Asked once.
sp = SeqSp({"tracks": {"items": []}})
cache = {}
search(sp, "Nobody At All", cache)
search(sp, "Nobody At All", cache)
check("an empty 200 is cached, with its status, and asked once",
      (appended[-1][1]["status"], appended[-1][1]["tracks"], sp.calls), (200, [], 1))

# Stage 8's first cache is retired, not migrated: no search writes to it.
check("the retired spotify_artist_tracks.jsonl is never written",
      {name for name, _ in appended}, {"spotify_artist_tracks_credited.jsonl"})


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
check("a 429 the retry clears does not mark the client rate-limited",
      sp.rate_limited, False)

# A bad Retry-After must not hang the run.
slept.clear()
seq = [_R(429, {"Retry-After": "99999"}), _R(200)]
playlists.Spotify("tok").get("/search")
check("absurd Retry-After is capped", max(slept) <= 30, True)

# Still 429 after the one wait: a quota lockout. The envelope says so and the
# client remembers it, so Stage 8 stops searching instead of spending the
# lockout on more 429s.
calls.clear()
seq = [_R(429, {"Retry-After": "3"}), _R(429, {"Retry-After": "3"})]
playlists.requests.request = lambda m, u, **kw: (calls.append((m, u)), seq.pop(0))[1]
sp = playlists.Spotify("tok")
check("a 429 that outlives the retry returns a 429 envelope",
      sp.get("/search")["_status"], 429)
check("...after exactly one retry", len(calls), 2)
check("...and marks the client rate-limited", sp.rate_limited, True)

# 4xx comes back as an inspectable envelope, not an exception and not None.
playlists.requests.request = lambda m, u, **kw: _R(404, body="gone")
sp = playlists.Spotify("tok")
env = sp.get("/playlists/nope")
check("error returns an envelope", env["_status"], 404)
check("an ordinary error is not a rate limit", sp.rate_limited, False)

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
