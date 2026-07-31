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
on_genre = {playlists._title_key("Pure Dubstep"), playlists._title_key("Also Dubstep")}

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
check("input is not mutated", tracks[0], {"spotify_track_uri": "uri:1",
                                          "track_name": "Mega Hit"})

# --------------------------------------------------------------------------
# Title folding. Spotify and MusicBrainz disagree constantly about the tail of
# a title; the two catalogues must be compared on the song, not the pressing.
# --------------------------------------------------------------------------

for spotify_title, mb_title, label in [
    ("Pure Dubstep - Extended Mix", "Pure Dubstep", "dash suffix"),
    ("Pure Dubstep (Radio Edit)",   "Pure Dubstep", "parenthesised suffix"),
    ("Pure Dubstep [VIP]",          "Pure Dubstep", "bracketed suffix"),
    ("Enjoy the Silence - 2006 Remaster", "Enjoy the Silence", "remaster suffix"),
    ("Praise the Lord",             "Praise The Lord", "case only"),
]:
    check(f"title folds: {label}",
          playlists.choose_tracks([{"spotify_track_uri": "u",
                                    "track_name": spotify_title}],
                                  {playlists._title_key(mb_title)},
                                  k=1)[0]["genre_matched"], True)

check("a genuinely different song does not match",
      playlists.choose_tracks([{"spotify_track_uri": "u", "track_name": "Other Song"}],
                              {playlists._title_key("Pure Dubstep")},
                              k=1)[0]["genre_matched"], False)

# A title that is nothing but a suffix must not fold away to the empty string
# and then match everything.
check("degenerate title does not collapse to a wildcard",
      playlists._title_key("- Remaster") == "", False)

# --------------------------------------------------------------------------
# mb_genre_recordings: caching and the shape of a MusicBrainz answer.
# --------------------------------------------------------------------------


class FakeHttp:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.calls = []

    def get(self, url, params=None):
        self.calls.append(params)
        outer = self

        class R:
            status_code = outer.status
            @staticmethod
            def json():
                return outer.payload
        return R()


playlists.append_jsonl = lambda path, rec: None      # keep the test off disk

http = FakeHttp({"recordings": [{"title": "Neon Angel"},
                                {"title": "Energy Drink (VIP)"},
                                {"title": ""}]})
cache = {}
got = playlists.mb_genre_recordings(http, "mbid-vr", "dubstep", cache)
check("titles folded to comparison keys",
      got, {playlists._title_key("Neon Angel"), playlists._title_key("Energy Drink")})
check("empty title dropped", "" in got, False)
check("query is arid AND tag", http.calls[0]["query"],
      'arid:mbid-vr AND tag:"dubstep"')
check("answer cached", "mbid-vr::dubstep" in cache, True)

playlists.mb_genre_recordings(http, "mbid-vr", "dubstep", cache)
check("cache hit spends no request", len(http.calls), 1)

# The same artist for a DIFFERENT gap genre is a different question.
playlists.mb_genre_recordings(http, "mbid-vr", "techno", cache)
check("cache is keyed on (artist, tag)", len(http.calls), 2)

# "Nobody tagged this artist's recordings dubstep" is an answer, not a retry.
empty = FakeHttp({"recordings": []})
c2 = {}
check("empty answer returns empty", playlists.mb_genre_recordings(empty, "m", "t", c2), set())
playlists.mb_genre_recordings(empty, "m", "t", c2)
check("empty answer is cached, not re-asked", len(empty.calls), 1)

# A failed request must NOT cache — that is a missing answer, not an empty one.
dead = FakeHttp({}, status=503)
c3 = {}
check("failed request returns empty", playlists.mb_genre_recordings(dead, "m", "t", c3), set())
check("failed request not cached", c3, {})

if failures:
    print(f"{len(failures)} FAILURE(S)"); sys.exit(1)
print("all assertions passed")
