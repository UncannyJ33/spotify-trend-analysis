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

# --------------------------------------------------------------------------
# Scopes must only ever WIDEN. Stage 8 and Stage 6 share one token, so any path
# that re-consents has to carry the other stage's grant along with it.
# --------------------------------------------------------------------------

granted = []
poll.authorize = lambda client_id, scope=poll.SCOPE: (
    granted.append(scope) or {"access_token": "tok", "scope": scope})

# 1. Widening for playlists keeps the poller's scope
granted.clear()
poll.load_tokens = lambda: {"refresh_token": "r", "scope": "user-read-recently-played"}
poll.access_token("cid", "playlist-modify-private playlist-read-private")
check("widening re-consents with the union",
      set(granted[0].split()),
      {"user-read-recently-played", "playlist-modify-private",
       "playlist-read-private"})

# 2. A failed refresh must not quietly drop back to the default scope
granted.clear()
poll.load_tokens = lambda: {
    "refresh_token": "r",
    "scope": "user-read-recently-played playlist-modify-private playlist-read-private"}


class _Resp:
    status_code = 400
    text = "nope"


poll.requests.post = lambda *a, **k: _Resp()
poll.access_token("cid", poll.SCOPE)
check("failed refresh re-consents with everything already held",
      set(granted[0].split()),
      {"user-read-recently-played", "playlist-modify-private",
       "playlist-read-private"})

# 3. A refresh response that omits `scope` must not erase the stored grant
saved = {}


class _OK:
    status_code = 200
    @staticmethod
    def json():
        return {"access_token": "new"}      # no scope, no refresh_token


poll.requests.post = lambda *a, **k: _OK()
poll.save_tokens = lambda d: saved.update(d)
poll.load_tokens = lambda: {"refresh_token": "r", "scope": "a b"}
poll.access_token("cid", "a")
check("refresh without a scope field keeps the stored grant",
      saved.get("scope"), "a b")
check("refresh without a refresh_token keeps the stored one",
      saved.get("refresh_token"), "r")

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
