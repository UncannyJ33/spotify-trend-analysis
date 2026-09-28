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
# Discovery candidates face the same floor: a stranger must clear at least what
# a library anchor clears (playlists.serving_sql holds both).
MIN_TAG_COUNT_FOR_ANCHOR = 1

# Carrying a genre is not serving it. An artist serves a playlist only when the
# spec's tags hold at least this share of their whole genre weight (counts
# clamped at 0), judged on the track's REMIXER where its title names one. The
# old rule was "carries any spec tag", and it let Halsey (indie pop 3 of 32,
# 0.09) and Ellie Goulding (indie pop + indie folk 5 of 31, 0.16) — both pop by
# any listener's ear, both heavily played — take four of six indie anchors.
#
# 0.25 is read off this library. It drops Halsey 0.09, Ellie Goulding 0.16,
# Fred again.. 0.19 and Calvin Harris 0.14 (garage), Led Zeppelin 0.04 and
# Evanescence 0.05 (heavy metal), Fall Out Boy 0.02 and Tame Impala 0.05
# (indie rock). It keeps Metallica 0.26 — heavy metal 42 split against thrash
# metal 67, the closest call it has to make — Black Sabbath 0.44, John Summit
# 0.50, NOTION 0.67, Lord Huron 0.50, Noah Kahan 0.43 and Myles Smith 0.50.
# Its known cost: The Killers (indie rock 8 of 47, 0.17) and The White Stripes
# (9 of 48, 0.19) stop anchoring indie rock, because MusicBrainz files them
# first as alternative and garage rock. Anyone moving this should re-read
# those two lists rather than the one example that prompted the move.
#
# The same bar applies to Stage 8's discovery candidates. It costs the
# garage and heavy metal playlists most of their stranger pool (18 candidates
# to 5, 28 to 5) — which is the point: the 13 and 23 it drops include
# Röyksopp, Pendulum, Papa Roach and Deftones, none of them the genre on the
# label. A short playlist beats a wrong one, as `assemble` already says.
ANCHOR_MIN_TAG_SHARE = 0.25

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
#
# `breakbeat` and `breakbeat hardcore` are absent for the same reason: broken
# beats are not run music. They carried The Prodigy into dubstep, and the cost of
# removing them lands on library artists, not strangers — jigitz (5.9 h, tagged
# breakbeat|future garage|house by hand) now classifies to neither run and comes
# back only as pins; K Theory (0.7 h), MALUGI (0.4 h) and the Chemical Brothers
# leave too. That list is the check before the change, not after it.
#
# `happy hardcore` left on 2026-09-27, SJ's call. As a NARROW tag one vote
# waived the stranger weight floor, and Stonebank got into the dubstep run on
# drum and bass(1) + happy hardcore(1), with a 2016 Monstercat happy hardcore
# record that was cut. Checked before the change, the cost is: Stonebank is the
# only cached stranger it admitted. In the library, Kayzo (0.89 h) drops from
# 0.60 to 0.50 and leaves the run, and so does his "Wait", a contested keep that
# is in Workout (only a pin keeps it); MODULATE (0.07 h) and W&W (0.00 h in the
# window) leave too. `gabber`, from the same family, admits nobody today: no
# library artist and no cached candidate carries it.
RUN_GARAGE_TAGS = (
    "speed garage", "uk garage", "bassline", "stutter house", "bass house",
    "tech house", "jackin house", "donk", "2-step", "hard house",
)
RUN_BASS_TAGS = (
    "dubstep", "brostep", "tearout", "hybrid trap", "trap edm", "drum and bass",
    "jungle", "neurofunk", "drumstep", "glitch hop", "complextro",
    "hardstyle", "colour bass", "gabber",
)

# Genres that are electronic but do NOT sustain a run. An include-list alone is
# not enough: ILLENIUM carries dubstep(3) and trap edm(3), so any include-list
# admits him, while melodic dubstep(3) and future bass(2) are why the playlist
# sags. Membership is therefore a weighted share, not a set test.
#
# The last four are the soft or off-shape side of a cluster's own neighbours,
# each named for the act that proved it: `liquid funk` is drum and bass with the
# intensity taken out (Netsky), `big beat` is The Prodigy's home genre (21 votes,
# more than his breakbeat), `future garage` is garage's ambient offshoot, and
# `psytrance` (18 votes) had Infected Mushroom next in line for a dubstep slot.
# All four are in the MusicBrainz genre vocabulary, so they can match and be
# supplied by hand.
RUN_DRAG_TAGS = (
    "melodic dubstep", "future bass", "chillstep", "deep house",
    "progressive house", "melodic house", "melodic techno", "ambient",
    "downtempo", "trip hop", "chillout", "lo-fi", "pop", "dance-pop",
    "electropop", "synth-pop", "hip hop", "rap", "pop rap",
    "contemporary r&b", "alternative r&b", "r&b", "soul", "rock",
    "alternative rock", "indie rock", "heavy metal", "latin", "reggaeton",
    "folk", "singer-songwriter",
    "liquid funk", "big beat", "future garage", "psytrance",
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
# A target the known fill tries to reach, not a guaranteed floor. When the
# budget alone stops short (dubstep, 09-26: 0.597), one more track goes in if it
# clears RUN_TOPUP_MIN_SCORE and fits the whole target, and discovery gets what
# is left (running.fill_known). With no such track, or a known pool smaller than
# the budget, the known share stays under this and discovery fills the rest.
RUN_KNOWN_FRACTION = 0.6      # rest is discovery; more new music, by request
RUN_TRACKS_PER_ARTIST = 3     # one act must not own a playlist
# A `prefer` row in running_overrides.csv names a playlist the listener built
# by hand for running (Workout · Claude). Its members are a run-fit prior the
# score cannot see: a member's score is multiplied by 1 + this, so it beats a
# non-member only when within 25% of it — a near-tie breaker, not a pin. The
# evaluation's weak known tracks (Pretty Low, Buried A Friend) are exactly the
# near-ties it exists to settle, and neither is in Workout. Vetoes still win.
RUN_PREFER_MARGIN = 0.25
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

# When discovery cannot fill its share, known tracks take the time back — but
# only ones scoring at least this. Matt Sassari's "Give It To Me - Full Vocal
# Mix" (0.186) went in as filler. A playlist may therefore come in under
# RUN_TARGET_MINUTES, and the report prints by how much: a few minutes short
# beats minutes of what the listener skips.
RUN_TOPUP_MIN_SCORE = 0.20

# Discovery is seeded on the CLUSTER's own top artists, not on Stage 5's
# library-wide candidate list. Stage 5 seeds across all taste, so its
# electronic candidates skew canonical — a first dry run offered Basement Jaxx,
# Busy P and Mr. Oizo as speed garage, and The Prodigy as dubstep. Asking
# ListenBrainz "who is like Blair Muir" instead returns the right neighbourhood.
# Seeds are counted by ANSWER: an artist ListenBrainz knows nothing about is
# skipped and the next one down takes the slot, so 20 means 20 real lists.
RUN_DISCOVERY_SEEDS = 20         # cluster artists with a non-empty answer
RUN_MAX_CANDIDATES_TO_TAG = 150  # MusicBrainz lookups per cluster, at 1.1s each

# A seed must have been LISTENED to, not just credited. Todd Edwards has 0.54 h
# in the window — his only known-pool tracks are two Daft Punk edits he is
# featured on — yet a join per tag row made him garage seed #17, and his
# neighbours brought thirteen tracks (Crazy Love, Beauty And A Beat, Don't Stop
# The Music). The one-row-per-artist join and this floor both remove him.
# An hour, not a share of the cluster's score: a 1% floor would pass Sammy Virji
# (1.43 h) by 0.001 and fail Knock2 (1.48 h), both of whom belong.
RUN_MIN_SEED_HOURS = 1.0

# The FIRST discovery supply after hand-named acts is the listener's own
# cluster artists' tracks he has never started. Once the seed fixes cut Todd
# Edwards' chain, garage discovery fell from 21 tracks to about 9, and the known
# top-up would have quietly turned RUN_KNOWN_FRACTION into ~0.9; the evaluation's
# evidence is that an unplayed Sammy Virji record ("Up & Down") was a keep where
# the strangers were not. SJ's call (2026-09-26): when new music runs short,
# fill with more of his own, and never keep a weak stranger just to hold the
# quota. So library artists take up to FRACTION of the discovery budget BEFORE
# strangers, one track each, then whatever strangers leave, up to
# TRACKS_PER_ARTIST each. ARTISTS is how far down the seed ranking to look.
RUN_LIBRARY_DISCOVERY_ARTISTS = 30
RUN_LIBRARY_DISCOVERY_FRACTION = 0.25    # 0 gives SJ's literal "only when short"
RUN_LIBRARY_DISCOVERY_TRACKS_PER_ARTIST = 2

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
    "tech house", "hard house", "drum and bass", "glitch hop", "complextro",
})

# ...and a stranger's SHARE must clear a higher line than a library artist's.
# 0.60 is right for the library, where listening has already vouched for the
# artist; for a stranger it admitted Netsky (drum and bass 10 against liquid
# funk 3: 0.77), Rusko, Modestep and Basement Jaxx, four acts the evaluation
# wanted out. 0.85 refuses all four and keeps every discovery keep —
# Pendulum 0.97, Bassnectar 0.92, NERO 0.89 — and also refuses Slushii and
# Pixel Terror (0.67), which the normalised seed ranking would otherwise lift.
# Library classification (build_artist_clusters) stays on RUN_MIN_INTENSITY_SHARE.
RUN_MIN_CANDIDATE_SHARE = 0.85

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

# A DISCOVERY track may run at most 4.5 minutes; known tracks are exempt. A
# stranger's track is heard cold, mid-run, and a long one is a long bet: the cap
# took Bass Head (6.4 min), Dead Limit, Experience, Destiny and Where's Your
# Head At out of the dubstep run. Deviance and Vindicate were keeps and went
# too — that is the price. It also stands in for the release-year gate this
# stage deliberately lacks, since Spotify's release_date is often a reissue
# date. Known tracks stay exempt because the listener has already vouched for
# every minute: Zomboy's "Nuclear" runs 5.3 minutes and is the model run track.
RUN_MAX_DISCOVERY_MS = 270_000

# A song has two identities, and Stage 10 needs both. The SONG is the folded
# title (playlists._title_key): "Drugs I Like (AVELLO Remix)" and "Drugs I
# Like" are one song. The VERSION drops only the notes that name a PRESSING
# of the same recording — remaster (with or without a year), radio/extended/
# original mix or edit, feat./ft./featuring/with credits, mono, stereo, single
# and album version, explicit, clean — and keeps everything else, so a remix,
# VIP, flip, rework, bootleg, a person's edit or "sped up" is its own version.
# Folding on the song alone kept a garage original's remix out of dubstep and
# split "tell you straight" (70 + 44 plays) into two half-counted rows.
#
# A note counts only as a WHOLE segment: a bracket, or a run of " - " segments
# ending the title. "Song - Clean Bandit Remix" is not a clean pressing.
# `with` is read as a credit only in Spotify's "(with X)" bracket: a dash
# segment starting "With" can as easily be a remixer's name.
# The same pattern runs in DuckDB (RE2) and Python, so it stays inside what
# both accept: no lookaround, no backreferences, no \b inside a class, and
# [0-9] and literal spaces rather than \d and \s, whose Unicode reach differs.
_RUN_NOTE = (
    r"(?:[0-9]{4} (?:- )?)?(?:digital )?remaster(?:ed)?(?: [0-9]{4})?(?: version)?"
    r"|radio (?:edit|mix|version)|extended (?:mix|version|edit)"
    r"|original (?:mix|version)|(?:mono|stereo)(?: version| mix)?"
    r"|single version|album version|explicit|clean"
)
_RUN_NOTE_BRACKET = (
    r" *[(\[] *(?:" + _RUN_NOTE
    + r"|(?:feat\. *|ft\. *|(?:feat|ft|featuring|with) +)[^()\[\]]+) *[)\]]"
)
RUN_PRESSING_NOTE_RE = (
    r"(?i)" + _RUN_NOTE_BRACKET
    + r"|(?: +[-–—] +(?:" + _RUN_NOTE
    + r"|(?:feat\. *|ft\. *|(?:feat|ft|featuring) +)[^()\[\]–—-]+))+"
    + r"(?:" + _RUN_NOTE_BRACKET + r")* *$"
)
# One version per song per playlist, and completion picks it — but not on a
# handful of plays. A remix finished 2 times out of 2 is not evidence against
# an original finished 6 out of 10.
RUN_MIN_VERSION_PLAYS = 3

# Named by the cluster's TITLE, not its label (running.CLUSTERS has both): the
# label is the identity and never changes; the title is display and may.
RUN_PLAYLIST_NAME_TEMPLATE = "{title} run · Claude"
RUN_PLAYLIST_DESCRIPTION_TEMPLATE = (
    "{title} — high-intensity tracks for running, {known} from your library "
    "and {new} you have not heard. No BPM filter: Spotify removed the tempo "
    "endpoints, and half-time drums make the number lie anyway. "
    "Built by spotify-trend-analysis · refreshed {date}"
)
# Names a run playlist carried before a rename, keyed on the cluster LABEL.
# The garage run became "garage & house" on 2026-09-26, because tech house and
# John Summit stay in it. With the stored ID lost, an exact match on the new
# name alone would find nothing and create a second playlist; these are tried
# after it, exactly, case and all. The rename itself happens on the next
# --write, in place: same ID, same URL, same followers.
RUN_PLAYLIST_LEGACY_NAMES = {"speed garage": ("speed garage run · Claude",)}

# --- Stage 11: capture — the dump, read-only ---------------------------------
# Whatever SJ saves into (a playlist named on the command line, or Liked
# Songs) is READ here and never written. Fresh · Claude is the pipeline-owned
# rendering. The source is a CLI flag, not a constant, for Stage 9's reason:
# personal playlist names stay out of tracked config.
CAPTURE_PARQUET = DATA_DIR / "capture.parquet"
CAPTURE_STATE_JSON = DATA_DIR / "capture_state.json"   # "fresh" -> id; sources
FRESH_SIZE = 100           # one PUT; the newest 100 held 57% of the dump's 2026 plays
FRESH_PLAYLIST_NAME = "Fresh · Claude"
FRESH_PLAYLIST_DESCRIPTION_TEMPLATE = (
    "The {n} newest tracks you saved, newest first ({hours:.1f} h). "
    "Built by spotify-trend-analysis · refreshed {date}")

# Fields that must never reach a derived artifact.
DROPPED_FIELDS = ("ip_addr",)


def ensure_dirs() -> None:
    """Create the derived-artifact directories. Safe to call repeatedly."""
    for d in (DATA_DIR, CACHE_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)
