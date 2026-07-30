# Spotify Listening History — Trend Analysis

Take your own Spotify Extended Streaming History export, turn it into a queryable
DuckDB + Parquet dataset, and find out **which direction your taste is actually
moving** — which genres are climbing, which are quietly dying, and what you'd
probably like next if the trend keeps going.

Everything runs locally. Your listening history never leaves your machine and
never enters git.

The end product is a single self-contained HTML file you can open offline, plus
an interactive dashboard if you'd rather poke at it:

```
.venv/bin/python report.py --open          # → output/report.html
.venv/bin/streamlit run app.py --server.address 127.0.0.1
```

## What it answers

- **What has my listening actually been made of, month by month?** Share of
  listening *time* per genre, not play counts — a track you sat through counts
  more than one you skipped at 31 seconds.
- **What's rising and what's dying?** Each genre gets a trailing-12-month slope
  and a `rising` / `declining` / `flat` classification, gated so a genre sitting
  at 0.1% can't post "+540% a year" off a rounding error.
- **Where is it heading?** A damped projection of each genre's share, plus a gap
  analysis: genres climbing fast that you've barely explored.
- **What should I listen to next?** A recommender scored against where your taste
  is *going* rather than where it has been — see [Stage 5](#stage-5--trajectory-aware-recommendations),
  which is the part of this repo with an actual idea in it.
- **How has my discovery changed?** New artists per month, how concentrated your
  listening is in your top 1% of artists, and skip rate by genre.

For scale, the author's library runs 92,186 music plays across 2,521 artists over
seven years — about 3,950 hours. Numbers quoted throughout are from that dataset
and are there to show what the output looks like, not because yours will match.

## 1. Get your data out of Spotify

This is the slow part, so start it now.

Go to **[spotify.com/account/privacy](https://www.spotify.com/account/privacy/)**
and request **Extended streaming history**. Not "Account data" — that one is a
different, much smaller download capped at roughly the last year, and it will not
work here.

Spotify says up to 30 days; in practice it's usually a few days. You'll get an
email with a zip containing files named `Streaming_History_Audio_YYYY-YYYY_N.json`
(plus video and podcast files, which the pipeline handles and filters).

Unzip it into the project root so you have:

```
spotify-trend-analysis/
└── Spotify Extended Streaming History/
    ├── Streaming_History_Audio_2019-2021_0.json
    └── ...
```

Or put it anywhere and set `SPOTIFY_EXPORT_DIR` in a `.env` file — see
`.env.example`.

## 2. Install

Python 3.12. Everything runs through a project-local virtualenv; no global installs.

```bash
git clone https://github.com/UncannyJ33/spotify-trend-analysis.git
cd spotify-trend-analysis
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Six dependencies: DuckDB, requests, Plotly, Streamlit, pandas, python-dotenv.
No API keys are required for anything except the optional Stage 6 poller.

## 3. Run it

```bash
.venv/bin/python ingest.py        # seconds     — read the export, normalise, de-dupe
.venv/bin/python credits.py       # seconds     — recover featured artists from track titles
.venv/bin/python enrich.py        # ~1 hr/3k artists — genre tags from MusicBrainz (network)
.venv/bin/python analyze.py       # seconds     — monthly genre shares, trends, slopes
.venv/bin/python report.py --open # seconds     — the HTML report
```

Run those five in order the first time. **Stage 2 (`enrich.py`) is the long one**
— it's throttled to one MusicBrainz request per second because that's their rate
limit, so budget roughly an hour per 3,000 distinct artists. It is fully
resumable: every resolution is appended to `.cache/` and fsynced, so Ctrl-C and
re-run picks up exactly where it stopped, and later re-runs only spend requests
on artists they've never seen. You can also cap a session with `--limit 200`.

Every stage prints a report when it finishes — row counts, coverage, what got
dropped and why. Read it. That output is how you know a stage worked; there's no
test suite standing between you and a silently wrong number.

Then the optional extras:

```bash
.venv/bin/python recommend.py     # who to listen to next (network)
.venv/bin/python forecast.py      # projection + under-explored genre gaps
.venv/bin/python poll.py          # keep history current between exports (network, needs a Spotify app)
```

Re-run `report.py` afterwards and those sections appear in the HTML. Everything
downstream of Stage 3 degrades gracefully — if you skip the forecast, the report
simply omits that section rather than failing.

### Refreshing later

Request a fresh export every few months, drop it in, and run stages 1 → 4 again.
Ingestion is idempotent: the same export produces byte-identical files, and a
newer one just picks up the new plays. There's no incremental state to drift, and
no scheduler — this is built for occasional manual re-runs, not continuous
operation.

### All commands

| Stage | Command | Output |
|-------|---------|--------|
| 1. Ingest | `python ingest.py` | `data/plays_raw.parquet`, `data/plays.parquet` |
| 1b. Credits | `python credits.py` (`--review`) | `data/track_credits.parquet` |
| 2. Enrich | `python enrich.py` (`--limit N`, `--report`, `--retry-errors`) | `data/artist_tags.parquet`, `.cache/` |
| 3. Analyse | `python analyze.py` | `data/tag_trends.parquet`, `data/secondary/*` |
| 4. Dashboard | `streamlit run app.py --server.address 127.0.0.1` | interactive |
| 4b. Report | `python report.py` (`--open`) | `output/report.html` |
| 5. Recommend | `python recommend.py` (`--lambda N`, `--top N`) | `data/recommendations.parquet` |
| 6. Poll | `python poll.py` (`--status`, `--logout`) | `data/polled_plays.parquet` |
| 7. Forecast | `python forecast.py` (`--horizon N`) | `data/forecast.parquet`, `data/genre_gaps.parquet` |

All prefixed with `.venv/bin/`. Stages 2, 5 and 6 use the network; the rest are local.

**Bind the dashboard to localhost.** Streamlit listens on every interface by
default and prints an external URL on your public IP. This page renders your
personal listening history, so `--server.address 127.0.0.1` is part of the
command rather than an optional extra. Drop it only if you actually want the page
reachable from your LAN.

## 4. Reading the output

The report opens with your headline numbers and a stacked area chart of your top
genres over time. Some notes on how to read the rest without fooling yourself:

**Shares, not hours.** Every genre number is a share of that month's listening
time. A genre can "decline" while you listen to exactly as much of it, because
something else grew. That's usually the honest framing of taste — attention is
zero-sum — but it means a falling line is not proof you stopped listening.

**Smoothing is on by default.** Raw monthly shares are unreadable noise,
especially in months you barely listened. A 3-month rolling mean is applied
before anything is plotted. The dashboard has a toggle if you want the raw series.

**`rising` / `declining` / `flat` / `negligible`.** Classification comes from the
trailing-12-month slope of the smoothed share, relative to the genre's own size,
so a 2% genre and a 30% genre are judged on the same footing. Anything averaging
under 0.5% of your listening is classed `negligible` rather than given a trend —
without that floor the rankings fill with microscopic genres posting enormous
percentages off nothing. In the author's data that leaves 16 rising, 17
declining, 3 flat, and 475 genres correctly ignored.

**One play spreads across several genres.** Your time is split twice, and both
splits are normalised so listening time is never created or destroyed: first
across the performers on the track, then across each artist's genres in
proportion to how strongly MusicBrainz's community tagged them, capped at the top
8. So a play of one song adds fractions to several genres rather than picking a
winner.

**Featured artists are a toggle, not a decision.** Spotify's export credits only
the *album* artist, so features are invisible — in the author's data that hides
about 28% of listening time. `credits.py` recovers performers named in the track
title, and the dashboard lets you switch attribution on and off, because both
views are defensible. Both are computed and stored; the toggle costs nothing.

**The forecast is direction, not prophecy.** It extrapolates each genre's
trailing slope with a monthly damping factor and renormalises so shares still sum
to 1. Read it as "this is still climbing" or "this has topped out." An undamped
straight line would cheerfully predict 140% of your listening.

## 5. Your data stays local

`.gitignore` excludes the raw export, the zip it came in, every derived Parquet,
the enrichment cache, the rendered report and `.env`. Code and configuration are
tracked; your listening history is not.

The export contains an `ip_addr` field. It's dropped during Stage 1 and does not
exist in any derived artifact — Stage 1 asserts this and prints a privacy check
on every run.

Verify at any time:

```bash
git status --short                              # nothing from the export
git check-ignore -v "Spotify Extended Streaming History/"
```

Nothing is ever uploaded. The three network stages send only artist names and
MusicBrainz IDs to MusicBrainz and ListenBrainz; the poller talks to Spotify
about your own account. `output/report.html` contains your data — it's gitignored,
so think before you share the file itself.

## Design notes

The interesting parts, and the reasons they're built the way they are. If you're
cloning this to build something similar, these are the walls to know about before
you hit them.

### Stage 1 — Ingest and normalize

DuckDB reads the export JSON directly and writes Parquet. Nothing is staged
through pandas.

Two tables: **`plays_raw`** keeps every row unfiltered, because skip behaviour is
interesting on its own; **`plays`** is music only (`spotify_track_uri IS NOT NULL`,
which excludes podcasts and audiobooks) and real listens only
(`ms_played >= 30000`).

Idempotency is enforced by writing under a *total* row order (`ORDER BY ALL`). A
partial sort key leaves ties for DuckDB's parallel sort to break arbitrarily, and
byte-identical re-runs silently stop holding.

The export lists some events more than once. Since `ts` is the play *end* time,
an identical `(ts, ms_played, track)` triple cannot be two distinct listens, so
full-row duplicates are collapsed. Rows sharing that identity but differing in
other metadata are kept, and the count is reported.

### Stage 1b — Track credits

The export's only artist field is the album artist, so featured performers are
credited nowhere. In the author's data that hides about **28% of listening time**:
681 performers appear solely inside track titles and 407 never appear as an album
artist at all. `credits.py` parses `(feat. X)` / `(with X)` out of the title and
records one row per performer with a `credit_type`.

No weights are stored — Stage 3 decides what a feature is worth at query time, so
album-artist-only is simply the weight-0 case and stays available.

It only catches features named in the *title*; features living solely in
Spotify's track metadata stay invisible. This is a floor, not a fix. Run
`credits.py --review` to eyeball what the regex extracted.

### Stage 2 — Genre enrichment

One MusicBrainz search per artist yields both the MBID and a tag vector. Tags are
filtered against MusicBrainz's canonical genre vocabulary (~2,180 terms, fetched
once) because raw search tags carry noise like `usa`, `english`, `2010s` and
`gen z` alongside real genres.

Two deviations from the obvious design, both forced by the APIs:

- ListenBrainz's `bulk-tag-lookup` is keyed on **recording** MBIDs, not artist
  MBIDs, so it cannot consume an artist resolution. Using it would mean resolving
  every track to a recording MBID first — the per-track explosion this design set
  out to avoid.
- The search must query `artist:"X" OR alias:"X"` and *rank* the candidates rather
  than taking the first exact match. Searching the name alone silently loses every
  renamed artist — "Kanye West" returns a tribute band, because the entry is now
  "Ye" with the old name demoted to an alias. And an obscure artist holding a name
  as an *alias* can outrank the artist whose name it actually is: "Wale" returned
  a percussionist at score 100 ahead of the US rapper at 82. A primary-name match
  therefore beats an alias match.

Artists that resolve to an MBID but carry no tags are backfilled from their
**release-group** tags, which are frequently populated even when the artist page
is not.

**Spotify is not used as a fallback**, despite being the obvious candidate. Its
artist endpoint used to return a `genres` array and no longer does:
`/v1/artists/{id}` answers 200 with `genres`, `popularity` and `followers` all
absent (verified against Drake, Taylor Swift, Metallica and ArrDee), batch
`/v1/artists` and `/top-tracks` answer 403, and search never carried genres.
There is no genre data left there to fall back to. ListenBrainz's metadata lookup
would serve, but returns 401 without an Authorization header — the no-auth
guarantee covers only the `labs` host.

Resolution is strict — an exact normalised match against name or alias, never a
guess — and everything else lands on a review list you can inspect. Names are
folded on accents, case and stylisation so `A$AP Rocky` and `ASAP Rocky` collapse
together. The author's run resolved 2,605 artists, sent 315 to review, and
covered **92.3% of listening time** — the number that matters, since one
unresolved artist you play constantly hurts more than a hundred you played once.
`enrich.py --report` prints yours.

MusicBrainz is throttled to roughly 1 req/sec with a descriptive User-Agent; a
503 is answered with a real backoff, since MusicBrainz sends `Retry-After: 0` and
trusting it means no backoff at all.

### Stage 3 — Analysis

Share of listening time per tag per month, weighted by `ms_played`, under two
normalised weightings: a play's time splits across its performers, then each
artist's share splits across their genres by MusicBrainz vote count, capped at 8.

The cap matters. Ye carries 58 tags; splitting evenly would give each 1/58 while a
two-tag artist gives each a half, systematically burying well-tagged artists.

Trend classification gates on absolute size before computing relative change.
Without that floor a genre sitting at a fraction of a percent posts "+540% a year"
off a 0.3pp move and swamps the rankings.

`tag_trends.parquet` is the contract every downstream stage reads — long format,
keyed by `(variant, tag, month)`. If you want to build your own surface on top of
this, that's the table to query.

### Stage 4 — Output surfaces

Every chart lives in `figures.py` as a function returning a Plotly figure; the
dashboard and the report both import from it, so chart code is never written
twice.

`report.py` renders those same functions into `output/report.html` — one file, no
server, works from a `file://` URL with the network off. It lands around 4.8 MB.
Plotly is inlined **once**: passing `include_plotlyjs='inline'` per figure would
embed the ~3.5 MB library a dozen times over. Every figure is rendered twice,
light and dark, and CSS reveals whichever matches the reader's system, because a
baked-in Plotly figure cannot recolour itself.

Colour is anchored to the canonical global genre ranking rather than to list
position, so filtering out one genre never repaints the ones still on screen.

### Stage 5 — Trajectory-aware recommendations

A conventional recommender scores candidates against listening history, which
means it recommends your past back to you. The author's history is 30% hip hop
and that share is falling hard, so matching it points precisely where the
listening is leaving. Candidates are scored against a trajectory-weighted taste
vector instead:

```
weight(genre) = current_share x (1 + LAMBDA x relative_annual_change)
```

`LAMBDA = 2.0` was picked by sweep. At 0 the top ten holds six hip-hop artists
led by MC Eiht — gangsta rap being the fastest-declining genre in that library.
At 2 it holds none, led by Depeche Mode, while still grounded in real similarity.
`--lambda 0` is kept as the honest baseline; the dashboard exposes the dial and
re-ranks in ~0.03s because every input is cached locally. Try both — the
comparison tells you more about your own listening than either list alone.

Candidates come from ListenBrainz `similar-artists` — collaborative filtering
over real listening sessions, no auth. Spotify's equivalents are withdrawn:
`/v1/recommendations` returns 404, and `related-artists`, `top-tracks` and
`new-releases` return 403.

### Stage 6 — History poller

The export is a snapshot; without this everything freezes at its generation date.
Two limits belong to Spotify's `recently-played` endpoint and are surfaced rather
than hidden:

- **No play duration.** It reports `played_at` and full `duration_ms`, never how
  much was heard. Since the analysis weights by `ms_played`, polled rows carry an
  estimate flagged `ms_played_estimated`. Spotify only lists a track once it
  passes ~30s, so the estimate holds for the 30s floor but overstates anything
  abandoned at 45 seconds.
- **Fifty items, one page.** The author's history averages ~36 plays a day, so
  polling less often than daily silently drops plays. The run warns when a page
  comes back full — the signal that older plays fell off before it arrived.

When a real export later covers the same period, it supersedes every polled row
in that window, replacing the estimates with true durations.

Auth is Authorization Code with PKCE, so no client secret is used. **This is the
one thing that needs a Spotify developer app**: create one at
[developer.spotify.com](https://developer.spotify.com/dashboard), set its
redirect URI to exactly `http://127.0.0.1:3000`, and put the client ID in `.env`
as `SPOTIFY_CLIENT_ID`. Nothing else in the pipeline needs credentials.

The endpoint does return real *track* artists, unlike the export, so it's stored —
a later stage could use it to repair the album-artist flaw properly.

### Stage 7 — Forecast and gaps

Projects each genre's share forward under its trailing slope, damped by
`MOMENTUM_DECAY` per month and renormalised. A straight-line extrapolation of a
share would happily predict 140% of listening; read the output as direction and
rough magnitude, not as a model.

The gap analysis finds genres climbing fast while served by few artists — where
your listening is already heading but has barely explored — and joins them to
Stage 5's candidates. Ranked rather than cut at a fixed artist count, because the
thinnest-served rising genre in the author's data still has 13 artists, so any
hard threshold either admits everything or nothing.

## Tuning it

`config.py` holds every threshold in one place, commented with why each value is
what it is. The ones worth touching:

| Constant | Default | What it changes |
|----------|---------|-----------------|
| `MIN_MS_PLAYED` | 30,000 | What counts as a real listen rather than a skip |
| `TOP_N_TAGS_PER_ARTIST` | 8 | How many genres an artist's time spreads across |
| `ROLLING_WINDOW_MONTHS` | 3 | Smoothing window |
| `SLOPE_WINDOW_MONTHS` | 12 | Trailing window for the trend slope |
| `MIN_SHARE_FOR_TREND` | 0.005 | Floor below which a genre is `negligible` |
| `TREND_REL_THRESHOLD` | 0.15 | How much movement counts as rising or declining |
| `TRAJECTORY_LAMBDA` | 2.0 | How hard recommendations lean on trajectory vs similarity |

Change one, re-run `analyze.py`, re-run `report.py`. Stage 2's cache is untouched
by any of this, so you never pay the enrichment cost twice.

## Troubleshooting

**`... not found — run the earlier stages first.`** A stage's input Parquet is
missing. The stages are ordered for a reason; run them in sequence.

**Stage 2 looks frozen.** It's throttled to ~1 request/second by design. Watch
`.cache/artist_resolution.jsonl` grow. Ctrl-C is safe — it resumes.

**A lot of artists land in review.** Expected for classical, regional, and
heavily-stylised names. Check the time-weighted coverage from `enrich.py --report`
rather than the raw count; if that number is high, the analysis is sound.

**Streamlit prints an external URL on your public IP.** You dropped
`--server.address 127.0.0.1`.

**The poller says `SPOTIFY_CLIENT_ID missing`.** See Stage 6 above — that stage,
and only that stage, needs a developer app.

## Known data flaws

- `master_metadata_album_artist_name` is the **album** artist, not the track
  artist. On compilations, features and soundtracks it misattributes the play.
  Stage 1b recovers part of this; per-artist totals remain skewed.
- `ts` is UTC. Monthly buckets are therefore UTC months.
- Rows from the Video history files that carry a track URI are eligible for
  `plays`, but in practice all are short snippets the 30-second floor removes.
- Genre tags are crowd-sourced from MusicBrainz. They're inconsistent at the
  edges, and an artist nobody bothered to tag contributes nothing.

## License

No license file yet — treat it as all rights reserved unless that changes. Fork
it for your own listening history; ask before republishing.
