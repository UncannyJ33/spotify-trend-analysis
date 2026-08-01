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
ANCHOR_TRACKS = 5          # familiar tracks from library artists serving the gap
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

# --- Stage 9: consolidating two hand-made playlists --------------------------
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

# Fields that must never reach a derived artifact.
DROPPED_FIELDS = ("ip_addr",)


def ensure_dirs() -> None:
    """Create the derived-artifact directories. Safe to call repeatedly."""
    for d in (DATA_DIR, CACHE_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)
