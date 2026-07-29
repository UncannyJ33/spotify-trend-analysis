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

# Fields that must never reach a derived artifact.
DROPPED_FIELDS = ("ip_addr",)


def ensure_dirs() -> None:
    """Create the derived-artifact directories. Safe to call repeatedly."""
    for d in (DATA_DIR, CACHE_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)
