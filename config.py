"""Shared paths and constants.

Every stage imports from here so there is exactly one definition of where things
live. Override any path with an environment variable of the same name if you
keep the export somewhere else.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent


def _path_from_env(var: str, default: Path) -> Path:
    raw = os.getenv(var)
    return Path(raw).expanduser().resolve() if raw else default


# --- Inputs (gitignored: this is personal listening history) ----------------
EXPORT_DIR = _path_from_env(
    "SPOTIFY_EXPORT_DIR", PROJECT_ROOT / "Spotify Extended Streaming History"
)

# --- Derived artifacts (all gitignored) ------------------------------------
DATA_DIR = _path_from_env("SPOTIFY_DATA_DIR", PROJECT_ROOT / "data")
CACHE_DIR = _path_from_env("SPOTIFY_CACHE_DIR", PROJECT_ROOT / ".cache")
OUTPUT_DIR = _path_from_env("SPOTIFY_OUTPUT_DIR", PROJECT_ROOT / "output")

# Hand-written answers to Stage 2's review list. Gitignored like the rest of the
# personal data, since it is a list of artists you listen to; the tracked
# `artist_overrides.example.csv` documents the format.
ARTIST_OVERRIDES_CSV = _path_from_env(
    "SPOTIFY_ARTIST_OVERRIDES", PROJECT_ROOT / "artist_overrides.csv"
)

PLAYS_RAW_PARQUET = DATA_DIR / "plays_raw.parquet"
PLAYS_PARQUET = DATA_DIR / "plays.parquet"
ARTIST_TAGS_PARQUET = DATA_DIR / "artist_tags.parquet"
TAG_TRENDS_PARQUET = DATA_DIR / "tag_trends.parquet"

# --- Ingest rules -----------------------------------------------------------
# A "real listen" per the project spec. Anything shorter is a skip or a scrub.
MIN_MS_PLAYED = 30_000

# --- Analysis parameters ----------------------------------------------------
# The analysis horizon: the last month the EXPORT covers. Polled plays past it
# are provisional — estimated ms_played, NULL reason_end, and a 50-item page
# that can silently drop plays — so no trend, share or window is anchored on
# them. ingest.merge_polled's coverage cut, applied to time. Every
# "(SELECT max(month) FROM plays)" in a stage is this instead.
ANALYSIS_HORIZON_SQL = "(SELECT max(month) FROM plays WHERE NOT ms_played_estimated)"

# What a featured credit is worth relative to the album artist's 1.0. Stage 3
# computes BOTH variants and stores them side by side, so the dashboard can
# toggle without re-running anything. 0.0 reproduces the spec's original
# album-artist-only behaviour exactly.
CREDIT_VARIANTS = {"album_artist_only": 0.0, "with_features": 0.5}
DEFAULT_VARIANT = "with_features"

# Cap on genres per artist. Ye carries 58 tags; splitting his time evenly
# across all of them would give each 1/58 while a two-tag artist gives each a
# half, systematically burying well-tagged artists. Tags are weighted by
# MusicBrainz vote count and capped here.
TOP_N_TAGS_PER_ARTIST = 8

ROLLING_WINDOW_MONTHS = 3      # smoothing applied before anything is plotted
SLOPE_WINDOW_MONTHS = 12       # trailing window for the trend slope

# A tag is "flat" unless its trailing-year slope moves its own share by more
# than this fraction. Relative rather than absolute, so a 2% tag and a 30% tag
# are judged on the same footing.
TREND_REL_THRESHOLD = 0.15

# --- Stage 5: recommendations ----------------------------------------------
RECOMMENDATIONS_PARQUET = DATA_DIR / "recommendations.parquet"

# Seeds are drawn from recent listening, not all-time. Seeding on the whole
# history would recommend against who you were in 2019.
SEED_WINDOW_MONTHS = 18
N_SEEDS = 120

# Candidates are cheap to generate and expensive to tag, so only the strongest
# survive to the enrichment step.
MAX_CANDIDATES_TO_TAG = 400

# How hard to lean on trajectory. 0.0 scores candidates against current taste
# alone; higher values push toward genres that are climbing. The relative
# annual change is clamped before use so one explosive tag cannot dominate.
# 2.0 chosen by sweep, not by feel. At 0 the recommender is conventional and
# hands back six hip-hop artists in the top ten — including gangsta rap, the
# fastest-declining thing in this library. At 2 the list is electronic-forward
# with the strongest hip-hop candidate pushed to #11, while still grounded in
# real listening similarity.
TRAJECTORY_LAMBDA = 2.0
TRAJECTORY_CLAMP = (-0.9, 2.0)

# Final score is similarity^ALPHA * trajectory_fit^BETA. Similarity keeps
# results plausible; trajectory fit is what makes them forward-looking.
SIMILARITY_ALPHA = 0.5
TRAJECTORY_BETA = 1.0

# Floor on a tag's trailing-year share before it is classified at all. Relative
# change is meaningless against a near-zero denominator: a genre drifting from
# 0.05% to 0.3% scores "+540% a year" off a 0.3pp move and swamps the ranking.
# Anything below this is classed 'negligible' rather than rising or declining.
MIN_SHARE_FOR_TREND = 0.005  # 0.5% of a month's listening

# --- Stage 8: gap playlists --------------------------------------------------
PLAYLISTS_PARQUET = DATA_DIR / "playlists.parquet"      # archive of every run
PLAYLIST_STATE_JSON = DATA_DIR / "playlist_state.json"  # gap tag -> playlist id

N_PLAYLISTS = 4            # hard cap agreed with the user: never more than 4
PLAYLIST_SIZE = 25
ANCHOR_TRACKS = 6          # familiar tracks from library artists serving the gap
TRACKS_PER_ARTIST = 2      # one act must not own a playlist
ANCHOR_WINDOW_MONTHS = 18  # anchors ranked on recent listening, like SEED_WINDOW_MONTHS

# Which playlists to build, when the gap ranking is not the right answer. Same
# split as .env and artist_overrides.csv: the real file is gitignored because it
# is a statement of personal taste, and the tracked *.example.csv documents the
# format. Absent, Stage 8 falls back to the top N_PLAYLISTS gap genres.
PLAYLIST_OVERRIDES_CSV = _path_from_env(
    "SPOTIFY_PLAYLIST_OVERRIDES", PROJECT_ROOT / "playlist_overrides.csv"
)

# An anchor artist must carry the genre with at least this much community
# support. MusicBrainz tag counts go negative on downvotes and Stage 2 clamps
# them at 0, so a 0 means "nobody stands behind this tag" — REAPER carries
# `heavy metal` at 0 and anchored a metal playlist on the strength of it.
# Candidates are unaffected; this gates anchors only, where a wrong genre is
# most visible because the listener already knows the track.
MIN_TAG_COUNT_FOR_ANCHOR = 1

# The title marker is for the user's eyes in their own library: anything
# carrying it is pipeline-managed and safe to regenerate; anything without it
# is hand-made and must never be touched.
PLAYLIST_NAME_TEMPLATE = "{genre} frontier · Claude"
PLAYLIST_DESCRIPTION_TEMPLATE = (
    "Rising, under-explored genre in your listening: {genre}. "
    "A few anchors you know, the rest neighbours you don't. "
    "Built by spotify-trend-analysis · refreshed {date}"
)
# Pinned genres are a personal choice, not a trend finding — several are
# actively declining in the history. Saying "rising" about them would be a
# lie the playlist tells its own owner every time they open it.
PLAYLIST_DESCRIPTION_PINNED_TEMPLATE = (
    "{genre} — a genre you asked for rather than one the trend analysis found. "
    "A few anchors you know, the rest neighbours you don't. "
    "Built by spotify-trend-analysis · refreshed {date}"
)

# Weight given to a genre supplied by hand in artist_overrides.csv. MusicBrainz
# counts are vote tallies; a person listing an artist's genres is not voting, so
# every hand tag carries the same weight and the artist's listening time splits
# evenly across them. Must be >= MIN_TAG_COUNT_FOR_ANCHOR or hand-tagged artists
# could never anchor a Stage 8 playlist.
OVERRIDE_TAG_COUNT = 1

# --- Stage 9: consolidating hand-made playlists ------------------------------
# Unlike Stage 8, this stage reads playlists a person built. It creates a third
# playlist and never modifies either source.

CONSOLIDATE_REVIEW_CSV = DATA_DIR / "consolidate_review.csv"  # machine output

# Hand-written answers to the review list, same split as artist_overrides.csv:
# the real file is gitignored because it is a statement of personal taste, and
# the tracked *.example.csv documents the format. An override always outranks a
# computed score.
CONSOLIDATE_OVERRIDES_CSV = _path_from_env(
    "SPOTIFY_CONSOLIDATE_OVERRIDES", PROJECT_ROOT / "consolidate_overrides.csv"
)

# Bands on rap_share = rap_weight / (rap_weight + electronic_weight). Chosen
# against this library's real spread, where the two naive rules both fail: a veto
# list drops Daft Punk (1 rap tag against 92 electronic) and an allow list keeps
# Kendrick Lamar (58 against 2). Weighting puts them at 0.01 and 0.97, so the
# thresholds sit in genuinely empty space rather than cutting through a cluster.
CONSOLIDATE_KEEP_BELOW = 0.25
CONSOLIDATE_DROP_ABOVE = 0.60

# What a featured credit's genre is worth against the primary artist's 1.0.
# Spotify's `artists` array does not say who is featured, so position is the only
# signal available: artists[0] is primary, the rest are treated as features. A
# rapper guesting on an electronic track therefore lands in review rather than
# being dropped outright. Genuine co-headlines ("Chase & Status, Stormzy") are
# under-weighted by this; tune once a real run has been read.
CONSOLIDATE_FEATURE_WEIGHT = 0.5

# Tag -> family. Order matters: rap is tested first and wins, because this
# library's vocabulary genuinely overlaps -- `hardcore hip hop` (48 artists)
# matches both lists, and it is rap. The blocklist then removes tags that match
# an electronic pattern while being nothing of the kind: `garage rock` is not
# UK garage and `hardcore punk` is not hardcore techno. Anything matching neither
# list is off-family (rock, metal, pop) and goes to review, not to a guess.
CONSOLIDATE_RAP_PATTERNS = (
    "hip hop", "hip-hop", "rap", "trap", "drill", "grime", "crunk",
    "g-funk", "boom bap", "turntablism",
)
CONSOLIDATE_EDM_PATTERNS = (
    "house", "techno", "trance", "dubstep", "garage", "drum and bass", "dnb",
    "jungle", "breakbeat", "breaks", "big beat", "electro", "electronic",
    "electronica", "edm", "bass", "idm", "hardstyle", "gabber", "hardcore",
    "downtempo", "synthwave", "eurodance", "rave", "glitch", "trip hop",
)
# Tags that match CONSOLIDATE_EDM_PATTERNS but are not electronic music.
# `dance` is deliberately NOT a pattern: it matches `dancehall`, which is not.
CONSOLIDATE_EDM_BLOCKLIST = (
    "garage rock", "garage punk", "hardcore punk", "post-hardcore",
    "melodic hardcore", "metalcore", "deathcore", "grindcore",
)

# --- Stage 10: running playlists ---------------------------------------------
# Unlike Stage 8, which discovers genres you are drifting toward, this stage
# builds for a purpose: music that sustains a run. Two playlists, because this
# library splits cleanly into two high-intensity clusters that suit different
# runs — steady 4x4 garage for distance, drop-driven bass for intervals.
#
# There is no BPM here and there cannot be. Spotify's /audio-features and
# /audio-analysis both answer 403 since the February 2026 rename, and Deezer's
# ISRC lookup — which matches this library perfectly — carries a real BPM on
# only 12% of tracks. Raw tempo would also be the wrong signal even if it were
# available: Zomboy's "Nuclear (Hands Up)" measures 87 BPM because its drums are
# half-time, and it is exactly the kind of track that carries a run. What is
# being modelled is sustained intensity, not tempo.

RUNNING_STATE_JSON = DATA_DIR / "running_state.json"   # label -> playlist id

# Hand answers for this stage: pinned tracks and vetoes. Same split as the other
# four override files — the real one is gitignored because it states personal
# taste, and running_overrides.example.csv documents the format.
RUNNING_OVERRIDES_CSV = _path_from_env(
    "SPOTIFY_RUNNING_OVERRIDES", PROJECT_ROOT / "running_overrides.csv"
)

# The two clusters. Every tag here was checked against the MusicBrainz genre
# vocabulary: `riddim`, `uk bass`, `bass music` and `4x4 garage` are NOT
# MusicBrainz genres and were removed, since they can never match and cannot be
# supplied by hand either. Riddim artists arrive via dubstep/brostep/tearout.
# `electro house` is deliberately ABSENT. It reads as a neighbour of speed
# garage and is not one: it admitted 43 artists — MSTRKRFT, Benny Benassi,
# Justice, Digitalism — and because discovery seeds on the cluster's own top
# artists, seeding on Justice and Tiësto returned Mr. Oizo, Boys Noize and
# Basement Jaxx as "speed garage". A tag that is merely adjacent poisons the
# discovery pool far beyond the tracks it admits directly.
RUN_GARAGE_TAGS = (
    "speed garage", "uk garage", "bassline", "stutter house", "bass house",
    "tech house", "jackin house", "donk", "2-step",
    "hard house", "breakbeat hardcore",
)
RUN_BASS_TAGS = (
    "dubstep", "brostep", "tearout", "hybrid trap", "trap edm", "drum and bass",
    "jungle", "neurofunk", "drumstep", "breakbeat", "glitch hop", "complextro",
    "hardstyle", "colour bass", "happy hardcore", "gabber",
)

# Genres that are electronic but do NOT sustain a run. An include-list alone is
# not enough: ILLENIUM carries dubstep(3) and trap edm(3), so any include-list
# admits him, while melodic dubstep(3) and future bass(2) are why the playlist
# sags. Membership is therefore a weighted share, not a set test.
RUN_DRAG_TAGS = (
    "melodic dubstep", "future bass", "chillstep", "deep house",
    "progressive house", "melodic house", "melodic techno", "ambient",
    "downtempo", "trip hop", "chillout", "lo-fi", "pop", "dance-pop",
    "electropop", "synth-pop", "hip hop", "rap", "pop rap",
    "contemporary r&b", "alternative r&b", "r&b", "soul", "rock",
    "alternative rock", "indie rock", "heavy metal", "latin", "reggaeton",
    "folk", "singer-songwriter",
)
# Bare `trap` appears on 60 of this playlist's tracks and is ambiguous: it names
# both rap-trap and EDM-trap. It is deliberately in NEITHER list, so it can
# neither qualify nor disqualify an artist — the specific tags (`trap edm`,
# `hybrid trap` on one side, `hip hop`, `pop rap` on the other) decide. Same
# rule as `dance` in the Stage 9 patterns, and for the same reason.

# Tags matching a cluster pattern that are nothing of the kind. Same job as
# CONSOLIDATE_EDM_BLOCKLIST: `garage rock` is not UK garage and `hardcore punk`
# is not happy hardcore.
RUN_TAG_BLOCKLIST = (
    "garage rock", "garage punk", "hardcore punk", "post-hardcore",
    "melodic hardcore", "metalcore", "deathcore", "grindcore", "jazz house",
)

# cluster_weight / (cluster_weight + drag_weight), weighted by tag_count.
# 0.60 rather than 0.50 because the borderline cases in this library are
# genuinely borderline: ILLENIUM lands at 0.55 and is the sag being removed.
RUN_MIN_INTENSITY_SHARE = 0.60

# How far either side of the line to print in the report, so the marginal calls
# are visible rather than silent.
RUN_BORDERLINE_BAND = 0.15

# Fill to time, not to a track count: the ask was "an hour and a half to two
# hours", and Spotify returns duration_ms on every item, so there is no reason
# to approximate it with a count. 105 min is ~33 tracks at this library's
# 3.15-minute median.
# Four hours, not the run's length. A playlist sized to the run is a playlist
# heard end to end every time; at 30-45 minutes a session, four hours is five or
# six runs before anything repeats. The pool supports it without loosening the
# genre filter — 220 dubstep and 181 garage tracks qualify from the history
# alone, 11.8 h and 9.2 h uncapped — so length costs variety, not precision.
RUN_TARGET_MINUTES = 240
RUN_KNOWN_FRACTION = 0.6      # rest is discovery; more new music, by request
RUN_TRACKS_PER_ARTIST = 3     # one act must not own a playlist
# Discovery stays at 2. Three tracks from an artist you already play is more of
# a good thing; three from a stranger is three chances to break a run.
RUN_DISCOVERY_TRACKS_PER_ARTIST = 2
# 36 months, not 18. Past 36 the pool stops growing (+0.2 h dubstep, +0.0 h
# garage), and the older material is the part that has NOT been heard lately —
# which is the whole complaint being answered.
RUN_WINDOW_MONTHS = 36

# A known track's rank. Listening time says you choose it; the trackdone rate
# says you let it finish. Both are needed: Luude's "Pachamama" has more plays
# than his Blair Muir remix and less than half the completion.
RUN_MIN_TRACKDONE_RATE = 0.45

# Discovery is seeded on the CLUSTER's own top artists, not on Stage 5's
# library-wide candidate list. Stage 5 seeds across all taste, so its
# electronic candidates skew canonical — a first dry run offered Basement Jaxx,
# Busy P and Mr. Oizo as speed garage, and The Prodigy as dubstep. Asking
# ListenBrainz "who is like Blair Muir" instead returns the right neighbourhood.
RUN_DISCOVERY_SEEDS = 20         # top cluster artists to ask about
RUN_MAX_CANDIDATES_TO_TAG = 150  # MusicBrainz lookups per cluster, at 1.1s each

# A share alone cannot judge a STRANGER. Boys Noize and Mr. Oizo carry exactly
# one cluster tag — `tech house` at count 1 — and no drag tags at all, so the
# ratio is 1/1 and they scored a perfect 1.00 as speed garage. Discovery
# candidates must therefore clear an absolute weight too, not just a ratio.
#
# Deliberately NOT applied to library artists, and the asymmetry is the point:
# an artist with 36 hours of listening has earned the benefit of a thin tag
# vector, and hand-supplied tags all carry OVERRIDE_TAG_COUNT = 1 so a floor of
# 2 would exclude every artist answered by hand. A stranger has earned nothing —
# and a wrong discovery track is the expensive mistake here, because it lands
# mid-run on a listener with no relationship to it.
RUN_MIN_CANDIDATE_CLUSTER_WEIGHT = 2

# ...but the floor applies only to BROAD tags. One vote for `speed garage` or
# `bass house` is a real statement about an artist; one vote for `tech house` is
# not — French electro acts carry it too, which is exactly how Boys Noize and
# Mr. Oizo scored 1.00. A candidate carrying any NARROW cluster tag qualifies at
# a single vote; one carrying only these needs the floor.
#
# Chris Lorenzo is the honest cost of this rule: he carries `tech house(1)` and
# nothing else, which is tag-identical to Boys Noize, so no rule can admit one
# and refuse the other. He is lost until MusicBrainz knows more about him.
RUN_BROAD_TAGS = frozenset({
    "tech house", "hard house", "breakbeat hardcore",
    "drum and bass", "breakbeat", "glitch hop", "complextro",
})

# Live recordings are refused outright. Crowd noise, a rambling intro and a
# tempo the drummer chose on the night all break a run in a way the genre
# filter cannot see — the track is perfectly on-genre and still wrong.
#
# Structural, not a substring: a bare /live/ would take Zeds Dead's "Alive" and
# Dustycloud's "Alive", both of which are in these playlists and both of which
# belong. The marker has to sit where a pressing note sits — after " - ", inside
# a bracket, or in front of a venue.
RUN_LIVE_TITLE_RE = (
    r"(?i)( - live\b|\(live\b|\[live\b"
    r"|\blive (?:at|from|in|session|version)\b|\bunplugged\b)"
)

RUN_PLAYLIST_NAME_TEMPLATE = "{label} run · Claude"
RUN_PLAYLIST_DESCRIPTION_TEMPLATE = (
    "{label} — high-intensity tracks for running, {known} from your library "
    "and {new} you have not heard. No BPM filter: Spotify removed the tempo "
    "endpoints, and half-time drums make the number lie anyway. "
    "Built by spotify-trend-analysis · refreshed {date}"
)

# Fields that must never reach a derived artifact.
DROPPED_FIELDS = ("ip_addr",)


def ensure_dirs() -> None:
    """Create the derived-artifact directories. Safe to call repeatedly."""
    for d in (DATA_DIR, CACHE_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)
