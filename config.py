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

# Fields that must never reach a derived artifact.
DROPPED_FIELDS = ("ip_addr",)


def ensure_dirs() -> None:
    """Create the derived-artifact directories. Safe to call repeatedly."""
    for d in (DATA_DIR, CACHE_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)
