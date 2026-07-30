"""Stage 4b — render the analysis into one self-contained offline HTML file.

    .venv/bin/python report.py
    .venv/bin/python report.py --open

Imports the same functions from `figures.py` that the dashboard uses, so chart
code exists in exactly one place. Plotly is inlined once — passing
`include_plotlyjs='inline'` for every figure would embed the ~3.5MB library
eight times over.

Every figure is rendered twice, light and dark, and CSS shows whichever matches
the reader's system. A baked-in Plotly figure cannot recolour itself, so honest
dark mode means two renders.

Output: output/report.html — no network, no server, works from a file:// URL.
"""

from __future__ import annotations

import argparse
import webbrowser
from datetime import datetime, timezone

import duckdb
import pandas as pd

import config
import figures

OUT = config.OUTPUT_DIR / "report.html"


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    sources = {
        "plays": config.PLAYS_PARQUET,
        "plays_raw": config.PLAYS_RAW_PARQUET,
        "tag_trends": config.TAG_TRENDS_PARQUET,
        "artist_resolution": config.DATA_DIR / "artist_resolution.parquet",
        "track_credits": config.DATA_DIR / "track_credits.parquet",
    }
    for name, path in sources.items():
        if not path.exists():
            raise SystemExit(f"{path} not found — run the earlier stages first.")
        con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM '{path}'")

    optional = {
        "forecast": config.DATA_DIR / "forecast.parquet",
        "genre_gaps": config.DATA_DIR / "genre_gaps.parquet",
        "recommendations": config.RECOMMENDATIONS_PARQUET,
        "discovery_rate": config.DATA_DIR / "secondary/discovery_rate.parquet",
        "repeat_concentration": config.DATA_DIR / "secondary/repeat_concentration.parquet",
        "skip_rate_by_tag": config.DATA_DIR / "secondary/skip_rate_by_tag.parquet",
    }
    for name, path in optional.items():
        if path.exists():
            con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM '{path}'")
    return con


def has(con: duckdb.DuckDBPyConnection, view: str) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM duckdb_views() WHERE view_name = ?", [view]).fetchone()[0])


# --------------------------------------------------------------------------
# HTML pieces
# --------------------------------------------------------------------------


def esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# MusicBrainz genre tags are lowercase throughout, which is fine inside a chart
# legend and clumsy in running prose — "gave way to edm" reads as a typo.
ACRONYMS = {"edm", "r&b", "uk", "us", "usa", "idm", "dnb", "ebm", "nyc",
            "dj", "adhd", "lo-fi", "emo"}


def pretty(tag: str) -> str:
    """Genre name fit for prose. Only acronyms are touched; nothing else."""
    return " ".join(w.upper() if w in ACRONYMS else w for w in str(tag).split())


def sentence(text: str) -> str:
    """Capitalise the first letter without flattening the rest."""
    return text[:1].upper() + text[1:] if text else text


def swatch(tag: str, cmap_light: dict, cmap_dark: dict) -> str:
    """A genre name carrying its own chart colour — the report's one flourish.

    The reader meets the mapping in the prose before the first chart, so the
    legends below are already familiar rather than something to decode.
    """
    lo = cmap_light.get(tag, "#52514e")
    da = cmap_dark.get(tag, "#c3c2b7")
    return (f'<b class="g" style="--gl:{lo};--gd:{da}">'
            f'<i class="dot"></i>{esc(pretty(tag))}</b>')


def table(df: pd.DataFrame, cols: dict[str, str], fmts: dict | None = None) -> str:
    """A compact data table. Numeric cells get mono figures via CSS, not markup."""
    fmts = fmts or {}
    head = "".join(f"<th>{esc(v)}</th>" for v in cols.values())
    rows = []
    for _, r in df.iterrows():
        cells = []
        for key in cols:
            val = r[key]
            fn = fmts.get(key)
            cells.append(f"<td>{fn(val) if fn else esc(val)}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return (f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def figure_pair(fn, *args, **kwargs) -> str:
    """Render one figure for both themes; CSS reveals the matching one."""
    out = []
    for mode in ("light", "dark"):
        fig = fn(*args, mode=mode, **kwargs)
        fig.update_layout(autosize=True)
        html = fig.to_html(
            full_html=False,
            include_plotlyjs="inline" if not figure_pair.plotly_done else False,
            config={"displayModeBar": False, "responsive": True},
        )
        figure_pair.plotly_done = True
        out.append(f'<div class="fig {mode}-only">{html}</div>')
    return "".join(out)


figure_pair.plotly_done = False


CSS = """
:root{
  --surface:#fcfcfb; --plane:#f9f9f7; --ink:#0b0b0b; --ink2:#52514e;
  --muted:#898781; --rule:#e1e0d9; --accent:#2a78d6; --down:#e34948;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  --sans:system-ui,-apple-system,"Segoe UI",sans-serif;
}
@media (prefers-color-scheme:dark){
  :root{
    --surface:#1a1a19; --plane:#0d0d0d; --ink:#fff; --ink2:#c3c2b7;
    --muted:#898781; --rule:#2c2c2a; --accent:#3987e5; --down:#e66767;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);font-family:var(--sans);
     font-size:16px;line-height:1.6;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:0 24px 96px}

/* Theme-paired figures: only one render is ever visible. */
.dark-only{display:none}
@media (prefers-color-scheme:dark){
  .light-only{display:none}
  .dark-only{display:block}
}

/* ---- header ---- */
header{padding:88px 0 40px;border-bottom:1px solid var(--rule)}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.18em;
         text-transform:uppercase;color:var(--muted);margin:0 0 28px}
h1{font-size:clamp(30px,5.2vw,54px);line-height:1.12;letter-spacing:-.033em;
   font-weight:680;margin:0;max-width:19ch}
.lede{margin:26px 0 0;max-width:62ch;color:var(--ink2);font-size:17px}
.stamp{font-family:var(--mono);font-size:12px;color:var(--muted);margin-top:30px}

/* colour-keyed genre name: the one flourish in the whole document */
.g{font-weight:640;color:var(--gl);white-space:nowrap}
@media (prefers-color-scheme:dark){ .g{color:var(--gd)} }
.dot{display:inline-block;width:.52em;height:.52em;border-radius:2px;
     background:currentColor;margin-right:.4em;vertical-align:baseline}

/* ---- stats ---- */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
       gap:1px;background:var(--rule);border:1px solid var(--rule);margin:40px 0 0}
.stat{background:var(--surface);padding:20px 22px}
.stat dt{font-family:var(--mono);font-size:10.5px;letter-spacing:.13em;
         text-transform:uppercase;color:var(--muted);margin:0 0 8px}
.stat dd{margin:0;font-family:var(--mono);font-size:26px;font-weight:600;
         letter-spacing:-.02em}

/* ---- sections ---- */
section{padding-top:76px}
h2{font-size:12px;font-family:var(--mono);letter-spacing:.17em;
   text-transform:uppercase;color:var(--muted);margin:0 0 6px;font-weight:600}
h2+p.title{font-size:clamp(22px,3vw,30px);line-height:1.22;letter-spacing:-.02em;
           font-weight:660;margin:0 0 14px;max-width:26ch}
section>p{max-width:66ch;color:var(--ink2);margin:0 0 22px}
.fig{background:var(--surface);border:1px solid var(--rule);padding:8px 4px;
     margin:26px 0 0}
figcaption,.note{font-family:var(--mono);font-size:11.5px;color:var(--muted);
                 margin-top:10px;max-width:76ch;line-height:1.65}

/* ---- tables ---- */
.scroll{overflow-x:auto;border:1px solid var(--rule);background:var(--surface);
        margin-top:26px}
table{border-collapse:collapse;width:100%;font-size:14px}
th{font-family:var(--mono);font-size:10.5px;letter-spacing:.11em;
   text-transform:uppercase;color:var(--muted);text-align:left;font-weight:600;
   padding:12px 16px;border-bottom:1px solid var(--rule);white-space:nowrap}
td{padding:11px 16px;border-bottom:1px solid var(--rule);vertical-align:top}
tbody tr:last-child td{border-bottom:0}
td:not(:first-child){font-family:var(--mono);font-variant-numeric:tabular-nums;
                     font-size:13px;white-space:nowrap}
.up{color:var(--accent);font-weight:600}
.dn{color:var(--down);font-weight:600}

/* ---- caveats ---- */
.caveats{border-left:2px solid var(--rule);padding:2px 0 2px 22px;margin-top:26px}
.caveats h3{font-family:var(--mono);font-size:11px;letter-spacing:.14em;
            text-transform:uppercase;color:var(--muted);margin:0 0 10px;
            font-weight:600}
.caveats p{margin:0 0 12px;color:var(--ink2);font-size:14.5px;max-width:70ch}
.caveats p:last-child{margin-bottom:0}
.caveats b{color:var(--ink);font-weight:620}

footer{margin-top:88px;padding-top:26px;border-top:1px solid var(--rule);
       font-family:var(--mono);font-size:11.5px;color:var(--muted)}
a{color:inherit}
@media (max-width:640px){
  header{padding-top:56px}
  section{padding-top:56px}
  .stat dd{font-size:22px}
}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""


# --------------------------------------------------------------------------


def build(con: duckdb.DuckDBPyConnection) -> str:
    v = config.DEFAULT_VARIANT
    q1 = lambda s: con.execute(s).fetchone()  # noqa: E731

    plays, hours, artists, tracks = q1(
        "SELECT count(*), sum(played_seconds)/3600.0, "
        "count(DISTINCT artist_name), count(DISTINCT spotify_track_uri) FROM plays")
    lo, hi = q1("SELECT min(ts), max(ts) FROM plays")
    months = q1("SELECT count(DISTINCT month) FROM plays")[0]
    covered = q1(
        """
        SELECT 100.0 * sum(w.listening_hours) FILTER (WHERE r.status='resolved')
             / sum(w.listening_hours)
        FROM (SELECT artist_name, sum(played_seconds)/3600.0 AS listening_hours
              FROM track_credits GROUP BY 1) w
        LEFT JOIN artist_resolution r USING (artist_name)
        """)[0]

    trends = con.execute(
        f"SELECT * FROM tag_trends WHERE variant = '{v}'").df()
    trends["month"] = pd.to_datetime(trends["month"])
    totals = con.execute(
        f"SELECT tag, sum(tag_seconds)/3600.0 AS hours FROM tag_trends "
        f"WHERE variant='{v}' GROUP BY 1 ORDER BY hours DESC").df()
    ranked = totals["tag"].tolist()
    eligible = totals[totals["hours"] >= 10]["tag"].tolist()
    d = trends[trends["tag"].isin(eligible)]

    cmap_l = figures.build_color_map(ranked, "light", display_tags=ranked[:8])
    cmap_d = figures.build_color_map(ranked, "dark", display_tags=ranked[:8])
    sw = lambda t: swatch(t, cmap_l, cmap_d)  # noqa: E731

    # The two poles of the story, pulled from the data rather than hardcoded.
    latest = con.execute(
        f"""SELECT tag, smoothed_share, slope_pp_per_year FROM tag_trends
            WHERE variant='{v}' AND month=(SELECT max(month) FROM tag_trends)
            ORDER BY smoothed_share DESC""").df()
    top_riser = latest.nlargest(1, "slope_pp_per_year").iloc[0]
    top_faller = latest.nsmallest(1, "slope_pp_per_year").iloc[0]
    hh_peak = q1(
        f"""SELECT max(smoothed_share) FROM tag_trends
            WHERE variant='{v}' AND tag='{top_faller['tag']}'""")[0]

    P = []
    A = P.append

    # ---- header -----------------------------------------------------------
    A('<header><p class="eyebrow">Spotify listening history</p>')
    A(f'<h1>{esc(sentence(pretty(top_faller["tag"])))} gave way to '
      f'{esc(pretty(top_riser["tag"]))}.</h1>')
    # Span is measured, not written in. A hardcoded "seven years" is invisibly
    # wrong for anyone running this against their own, shorter export.
    span = f"{months} months" if months < 24 else f"{months // 12} years"
    A(f'<p class="lede">{span} of listening, {plays:,} plays, and one '
      f'decisive turn. {sw(top_faller["tag"])} peaked near '
      f'{100*hh_peak:.0f}% of listening time and now sits at '
      f'{100*top_faller["smoothed_share"]:.1f}%. {sw(top_riser["tag"])} moved '
      f'the other way. Genre names carry their chart colour throughout.</p>')
    A(f'<p class="stamp">{lo:%d %b %Y} — {hi:%d %b %Y} · {months} months · '
      f'generated {datetime.now(timezone.utc):%d %b %Y}</p>')
    A('<dl class="stats">')
    for label, value in (
        ("Listening", f"{hours:,.0f} h"),
        ("Plays", f"{plays:,}"),
        ("Artists", f"{artists:,}"),
        ("Tracks", f"{tracks:,}"),
        ("Time resolved", f"{covered:.1f}%"),
    ):
        A(f'<div class="stat"><dt>{label}</dt><dd>{value}</dd></div>')
    A('</dl></header>')

    # ---- the shift --------------------------------------------------------
    A('<section><h2>The shift</h2>')
    A('<p class="title">Share of listening time by genre, month by month.</p>')
    A('<p>Weighted by time played rather than play count, and smoothed over '
      'three months. The band labelled Other holds every genre outside the top '
      'eight — with hundreds of genres in play, it is expected to be large.</p>')
    A(figure_pair(figures.stacked_area_top_tags, d, ranked, smoothed=True))
    A('</section>')

    # ---- movers -----------------------------------------------------------
    A('<section><h2>Direction</h2>')
    A('<p class="title">What is rising, and what is falling away.</p>')
    A('<p>Change in each genre\'s share of listening over the trailing twelve '
      'months, measured on the smoothed series. Genres holding less than half a '
      'percent are left unclassified: relative change against a near-zero base '
      'produces enormous percentages from movements of no consequence.</p>')
    A(figure_pair(figures.rising_declining, d))
    A('</section>')

    # ---- trajectories -----------------------------------------------------
    A('<section><h2>Trajectories</h2>')
    A('<p class="title">The largest genres, on one scale.</p>')
    A('<p>A shared vertical axis, so the panels are comparable to each other '
      'rather than each stretched to fill its own frame.</p>')
    A(figure_pair(figures.tag_trajectories,
                  d, [t for t in ranked if t in eligible][:6], ranked,
                  smoothed=True))
    A('</section>')

    # ---- forecast ---------------------------------------------------------
    if has(con, "forecast"):
        fc = con.execute(
            "SELECT tag, share_now, share_future, change FROM forecast "
            "ORDER BY share_future DESC LIMIT 10").df()
        horizon = q1("SELECT max(horizon_months) FROM forecast")[0]
        A('<section><h2>Projection</h2>')
        A(f'<p class="title">Where the lines point, {horizon} months out.</p>')
        A('<p>Each genre\'s trailing slope carried forward, damped month by '
          'month and renormalised. This is a straight line with the brakes on, '
          'not a model — read direction and rough magnitude, nothing finer.</p>')
        A(table(fc.assign(tag=fc["tag"].map(pretty)), {"tag": "genre", "share_now": "now",
                     "share_future": "projected", "change": "change"},
                {"share_now": lambda x: f"{100*x:.1f}%",
                 "share_future": lambda x: f"{100*x:.1f}%",
                 "change": lambda x: (f'<span class="{"up" if x>0 else "dn"}">'
                                      f'{100*x:+.1f} pp</span>')}))
        A('</section>')

    # ---- gaps + recommendations ------------------------------------------
    if has(con, "genre_gaps"):
        gaps = con.execute(
            "SELECT tag, share_now, rel_change_per_year, n_artists, hours "
            "FROM genre_gaps ORDER BY gap_score DESC LIMIT 8").df()
        A('<section><h2>Unexplored</h2>')
        A('<p class="title">Genres climbing fast that you barely know.</p>')
        A('<p>Ranked by growth weighted against how few artists serve it. These '
          'are the places the listening is already moving toward without having '
          'looked around much.</p>')
        A(table(gaps.assign(tag=gaps["tag"].map(pretty)), {"tag": "genre", "rel_change_per_year": "growth",
                       "share_now": "share", "n_artists": "artists known",
                       "hours": "hours"},
                {"rel_change_per_year": lambda x: f'<span class="up">{100*x:+.0f}%</span>',
                 "share_now": lambda x: f"{100*x:.1f}%",
                 "n_artists": lambda x: f"{int(x)}",
                 "hours": lambda x: f"{x:.0f} h"}))

        if has(con, "recommendations"):
            recs = con.execute(
                """
                WITH g AS (SELECT tag FROM genre_gaps ORDER BY gap_score DESC LIMIT 8)
                SELECT g.tag, r.artist_name, r.matched_genres, r.via_artists
                FROM g JOIN recommendations r
                  ON list_contains(str_split(r.matched_genres, ', '), g.tag)
                QUALIFY row_number() OVER (PARTITION BY g.tag ORDER BY r.score DESC) <= 3
                ORDER BY g.tag, r.artist_name
                """).df()
            if not recs.empty:
                A('<p style="margin-top:44px">Artists carrying those genres that '
                  'have never appeared in the history, surfaced from listeners '
                  'with overlapping taste and scored toward the rising '
                  'direction.</p>')
                A(table(recs.assign(tag=recs["tag"].map(pretty)), {"tag": "genre", "artist_name": "try",
                               "matched_genres": "tagged", "via_artists": "similar to"}))
        A('</section>')

    # ---- behaviour --------------------------------------------------------
    if has(con, "discovery_rate"):
        A('<section><h2>Behaviour</h2>')
        A('<p class="title">How the listening happens, not what it is.</p>')
        A('<p>New artists reached each month. The opening months are inflated '
          'by definition — when the record starts, everyone is new.</p>')
        A(figure_pair(figures.discovery_rate,
                      con.execute("SELECT * FROM discovery_rate").df()))
        if has(con, "repeat_concentration"):
            A('<p style="margin-top:44px">Concentration: the share of each '
              'year\'s listening held by its busiest one percent of artists. '
              'Falling means listening spread wider.</p>')
            A(figure_pair(figures.repeat_concentration,
                          con.execute("SELECT * FROM repeat_concentration").df()))
        if has(con, "skip_rate_by_tag"):
            A('<p style="margin-top:44px">Skip rate by genre, taken from the '
              'unfiltered table so the sub-30-second plays the analysis '
              'otherwise drops are counted — those plays are the skips.</p>')
            A(figure_pair(figures.skip_rate_by_tag,
                          con.execute("SELECT * FROM skip_rate_by_tag").df()))
        A('</section>')

    # ---- caveats ----------------------------------------------------------
    A('<section><h2>Read with this in mind</h2>')
    A('<p class="title">What the numbers cannot tell you.</p>')
    A('<div class="caveats">')
    A('<h3>Artist attribution</h3><p>The export carries one artist field and it '
      'is the <b>album</b> artist, not the track artist. Featured performers are '
      'credited nowhere, which hides roughly a quarter of listening time. '
      'Performers named in track titles are recovered and credited here, but a '
      'feature that appears only in Spotify\'s metadata stays invisible.</p>')
    A('<h3>Genre granularity</h3><p>Electronic music is tagged far more finely '
      'in MusicBrainz than rap is. Some of the electronic rise is therefore '
      'sharper labelling rather than pure movement. The <b>direction is real; '
      'the magnitude is somewhat overstated</b>.</p>')
    A(f'<h3>Coverage</h3><p>{covered:.1f}% of listening time resolves to a known '
      'artist. What remains is mostly small or very new acts that music '
      'databases have not catalogued.</p>')
    A('<h3>Projection</h3><p>The forward-looking table is a damped straight '
      'line. A trend that ran for a year rarely runs another at full strength, '
      'which is what the damping accounts for — but it is still arithmetic, not '
      'a forecast model.</p>')
    A('</div></section>')

    A(f'<footer>Generated offline from local Parquet. No network, no server. '
      f'Charts come from the same functions the dashboard uses.<br>'
      f'{plays:,} plays · {months} months · {hi:%d %b %Y}</footer>')

    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Listening history — genre trends</title>'
            f'<style>{CSS}</style></head><body><div class="wrap">'
            f'{"".join(P)}</div></body></html>')


def main() -> None:
    ap = argparse.ArgumentParser(description="Stage 4b static HTML report")
    ap.add_argument("--open", action="store_true", help="open it when written")
    args = ap.parse_args()

    config.ensure_dirs()
    con = connect()
    html = build(con)
    OUT.write_text(html, encoding="utf-8")

    size = OUT.stat().st_size / 1_048_576
    print(f"Wrote {OUT}  ({size:.1f} MB, self-contained)")
    if args.open:
        webbrowser.open(OUT.as_uri())


if __name__ == "__main__":
    main()
