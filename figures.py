"""Stage 4 — every chart in the project, as functions returning Plotly figures.

The dashboard and the static report both import from here. Chart code is never
written twice.

Colour rules worth not breaking:
  - Categorical hues are assigned in fixed slot order and never cycled. A ninth
    series folds into "Other" rather than inventing a hue.
  - Colour follows the tag, not its rank. The map is built once from the global
    ranking, so filtering the view never repaints the surviving series.
  - Light and dark are separate validated step sets, not an automatic flip.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

# --- Validated palettes ----------------------------------------------------
# Both sets clear the lightness band, chroma floor, adjacent-pair CVD
# separation and normal-vision floor. On the light surface three slots fall
# below 3:1 contrast, which obliges the relief rule: the dashboard ships a
# table view alongside every chart.
CATEGORICAL = {
    "light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
              "#e87ba4", "#008300", "#4a3aa7", "#e34948"],
    "dark": ["#3987e5", "#d95926", "#199e70", "#c98500",
             "#d55181", "#008300", "#9085e9", "#e66767"],
}

THEME = {
    "light": {
        "surface": "#fcfcfb", "plane": "#f9f9f7",
        "text": "#0b0b0b", "text_secondary": "#52514e", "muted": "#898781",
        "grid": "#e1e0d9", "axis": "#c3c2b7", "other": "#c3c2b7",
        "rising": "#2a78d6", "declining": "#e34948",
    },
    "dark": {
        "surface": "#1a1a19", "plane": "#0d0d0d",
        "text": "#ffffff", "text_secondary": "#c3c2b7", "muted": "#898781",
        "grid": "#2c2c2a", "axis": "#383835", "other": "#52514e",
        "rising": "#3987e5", "declining": "#e66767",
    },
}

FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'
MAX_SERIES = 8
OTHER = "Other"


def build_color_map(ranked_tags: list[str], mode: str = "light",
                    display_tags: list[str] | None = None) -> dict[str, str]:
    """Fixed tag -> hue map anchored to the canonical global ranking.

    `ranked_tags` must always be the ranking over the WHOLE dataset, never the
    filtered view — that anchor is what keeps a hue attached to the genre rather
    than to its current position on screen.

    Eight hues cannot cover hundreds of genres, so the global top eight hold
    theirs permanently, and a genre promoted from below the cut by a filter is
    handed a hue that is still free. Series already on screen are never
    repainted.
    """
    palette = CATEGORICAL[mode]
    cmap = {t: palette[i] for i, t in enumerate(ranked_tags[:MAX_SERIES])}

    if display_tags:
        used = {cmap[t] for t in display_tags if t in cmap}
        free = [c for c in palette if c not in used]
        for t in display_tags:
            if t not in cmap and free:
                cmap[t] = free.pop(0)

    cmap[OTHER] = THEME[mode]["other"]
    return cmap


def _base_layout(fig: go.Figure, mode: str, title: str, *,
                 height: int = 460, ylabel: str = "", xlabel: str = "") -> go.Figure:
    t = THEME[mode]
    fig.update_layout(
        title=dict(text=title, font=dict(size=15, color=t["text"], family=FONT), x=0),
        height=height,
        paper_bgcolor=t["surface"],
        plot_bgcolor=t["surface"],
        font=dict(family=FONT, color=t["text_secondary"], size=12),
        margin=dict(l=56, r=24, t=52, b=44),
        hoverlabel=dict(font_family=FONT, font_size=12),
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="left", x=0, font=dict(size=11)),
    )
    axis = dict(showgrid=True, gridcolor=t["grid"], gridwidth=1,
                linecolor=t["axis"], zeroline=False,
                tickfont=dict(color=t["muted"], size=11),
                title_font=dict(color=t["muted"], size=11))
    fig.update_xaxes(**axis, title_text=xlabel)
    fig.update_yaxes(**axis, title_text=ylabel)
    return fig


def _prep(df: pd.DataFrame, share_col: str) -> pd.DataFrame:
    d = df.copy()
    d["month"] = pd.to_datetime(d["month"])
    d[share_col] = d[share_col].fillna(0.0)
    return d.sort_values("month")


# --------------------------------------------------------------------------
# Primary views
# --------------------------------------------------------------------------


def stacked_area_top_tags(df: pd.DataFrame, ranked_tags: list[str], *,
                          smoothed: bool = True, mode: str = "light",
                          top_n: int = MAX_SERIES) -> go.Figure:
    """Share of listening time by genre over time.

    Capped at eight hues; everything below the cut is aggregated into a single
    neutral "Other" band rather than given a generated colour.
    """
    share_col = "smoothed_share" if smoothed else "share"
    d = _prep(df, share_col)
    top_n = min(top_n, MAX_SERIES)

    # Band on the genres actually present after filtering, ordered canonically.
    # Slicing the global ranking directly would leave the chart empty whenever a
    # filter excluded the global leaders.
    present = set(d["tag"].unique())
    keep = [t for t in ranked_tags if t in present][:top_n]
    cmap = build_color_map(ranked_tags, mode, display_tags=keep)
    t = THEME[mode]

    d["band"] = d["tag"].where(d["tag"].isin(keep), OTHER)
    grouped = d.groupby(["month", "band"], as_index=False)[share_col].sum()

    fig = go.Figure()
    for band in keep + [OTHER]:
        sub = grouped[grouped["band"] == band]
        if sub.empty or sub[share_col].sum() == 0:
            continue
        fig.add_trace(go.Scatter(
            x=sub["month"], y=sub[share_col] * 100,
            name=band, mode="lines", stackgroup="one",
            fillcolor=cmap.get(band, t["other"]),
            # A 2px stroke in the surface colour separates adjacent bands so
            # the boundary reads even where two hues sit close together.
            line=dict(width=2, color=t["surface"]),
            hovertemplate=f"<b>{band}</b><br>%{{x|%b %Y}} · %{{y:.1f}}%<extra></extra>",
        ))

    label = "3-month rolling mean" if smoothed else "raw monthly"
    fig = _base_layout(fig, mode, f"Share of listening time by genre — {label}",
                       height=480, ylabel="share of listening (%)")
    fig.update_layout(hovermode="x unified")
    return fig


def tag_trajectories(df: pd.DataFrame, tags: list[str], ranked_tags: list[str], *,
                     smoothed: bool = True, mode: str = "light",
                     n_cols: int = 3) -> go.Figure:
    """Small multiples: one panel per selected genre, on a shared y-scale.

    Each panel holds a single series, so identity comes from the panel title and
    no colour comparison is being asked of the reader.
    """
    from plotly.subplots import make_subplots

    share_col = "smoothed_share" if smoothed else "share"
    d = _prep(df, share_col)
    tags = [t for t in tags if t][:12]
    if not tags:
        return _base_layout(go.Figure(), mode, "Select one or more genres")

    cmap = build_color_map(ranked_tags, mode, display_tags=tags)
    t = THEME[mode]
    n_rows = (len(tags) + n_cols - 1) // n_cols
    fig = make_subplots(rows=n_rows, cols=n_cols, subplot_titles=tags,
                        shared_yaxes=True, vertical_spacing=0.13,
                        horizontal_spacing=0.06)

    ymax = max((d[d["tag"].isin(tags)][share_col].max() or 0) * 100, 0.1)
    for i, tag in enumerate(tags):
        r, c = divmod(i, n_cols)
        sub = d[d["tag"] == tag]
        colour = cmap.get(tag, THEME[mode]["rising"])
        fig.add_trace(go.Scatter(
            x=sub["month"], y=sub[share_col] * 100, name=tag,
            mode="lines", line=dict(width=2, color=colour),
            fill="tozeroy", fillcolor=_fade(colour),
            showlegend=False,
            hovertemplate=f"<b>{tag}</b><br>%{{x|%b %Y}} · %{{y:.2f}}%<extra></extra>",
        ), row=r + 1, col=c + 1)

    fig = _base_layout(fig, mode, "Genre trajectories", height=210 * n_rows + 90)
    fig.update_yaxes(range=[0, ymax * 1.1], ticksuffix="%")
    for ann in fig.layout.annotations:
        ann.font.update(size=12, color=t["text"], family=FONT)
    fig.update_layout(hovermode="x")
    return fig


def rising_declining(df: pd.DataFrame, *, mode: str = "light",
                     top_n: int = 12) -> go.Figure:
    """Trailing-year movers, as a diverging horizontal bar.

    Blue and red are the diverging pair — poles of one measure, not two
    categories — and the sign is stated in the label as well as the colour.
    """
    t = THEME[mode]
    d = (df[df["trend_class"].isin(["rising", "declining"])]
         .drop_duplicates("tag")
         .assign(pp=lambda x: x["slope_pp_per_year"])
         .reindex(columns=["tag", "pp", "trend_class", "mean_recent_share"])
         .dropna(subset=["pp"]))
    if d.empty:
        return _base_layout(go.Figure(), mode, "No classified trends yet")

    d = d.reindex(d["pp"].abs().sort_values(ascending=False).index).head(top_n)
    d = d.sort_values("pp")

    fig = go.Figure(go.Bar(
        x=d["pp"], y=d["tag"], orientation="h",
        marker=dict(
            color=[t["rising"] if v > 0 else t["declining"] for v in d["pp"]],
            line=dict(width=2, color=t["surface"]),
        ),
        text=[f"{v:+.2f} pp/yr" for v in d["pp"]],
        textposition="outside",
        textfont=dict(color=t["text_secondary"], size=11, family=FONT),
        hovertemplate=("<b>%{y}</b><br>%{x:+.2f} pp per year"
                       "<br>now %{customdata:.1%} of listening<extra></extra>"),
        customdata=d["mean_recent_share"],
    ))
    fig = _base_layout(fig, mode, "Rising and declining genres — trailing 12 months",
                       height=max(360, 34 * len(d) + 130),
                       xlabel="change in share (percentage points per year)")
    fig.update_yaxes(showgrid=False)
    fig.add_vline(x=0, line_width=1, line_color=t["axis"])
    return fig


# --------------------------------------------------------------------------
# Secondary metrics
# --------------------------------------------------------------------------


def discovery_rate(df: pd.DataFrame, *, mode: str = "light") -> go.Figure:
    t = THEME[mode]
    d = df.copy()
    d["month"] = pd.to_datetime(d["month"])
    d = d.sort_values("month")
    fig = go.Figure(go.Scatter(
        x=d["month"], y=d["new_artists"], mode="lines",
        line=dict(width=2, color=CATEGORICAL[mode][0]),
        fill="tozeroy", fillcolor=_fade(CATEGORICAL[mode][0]),
        hovertemplate="%{x|%b %Y}<br><b>%{y}</b> new artists<extra></extra>",
    ))
    fig = _base_layout(fig, mode, "Discovery rate — artists heard for the first time",
                       ylabel="new artists per month")
    fig.update_layout(hovermode="x unified")
    fig.add_annotation(
        x=0.5, y=1.0, xref="paper", yref="paper", showarrow=False,
        text="the first months are inflated — every artist is new when the data starts",
        font=dict(size=10, color=t["muted"], family=FONT), yshift=-6,
    )
    return fig


def repeat_concentration(df: pd.DataFrame, *, mode: str = "light") -> go.Figure:
    t = THEME[mode]
    d = df.sort_values("year")
    fig = go.Figure(go.Bar(
        x=d["year"].astype(str), y=d["top_1pct_share"] * 100,
        marker=dict(color=CATEGORICAL[mode][0],
                    line=dict(width=2, color=t["surface"])),
        text=[f"{v*100:.0f}%" for v in d["top_1pct_share"]],
        textposition="outside",
        textfont=dict(color=t["text_secondary"], size=11, family=FONT),
        hovertemplate=("<b>%{x}</b><br>top 1%% of artists held %{y:.1f}%% "
                       "of listening<extra></extra>"),
    ))
    fig = _base_layout(fig, mode,
                       "Repeat concentration — share held by the top 1% of artists",
                       ylabel="share of the year's listening (%)")
    fig.update_xaxes(showgrid=False)
    return fig


def skip_rate_by_tag(df: pd.DataFrame, *, mode: str = "light",
                     min_hours: float = 20, top_n: int = 15) -> go.Figure:
    t = THEME[mode]
    d = df[df["hours"] >= min_hours].nlargest(top_n, "skip_rate").sort_values("skip_rate")
    if d.empty:
        return _base_layout(go.Figure(), mode, "No genres above the hours threshold")
    fig = go.Figure(go.Bar(
        x=d["skip_rate"] * 100, y=d["tag"], orientation="h",
        marker=dict(color=CATEGORICAL[mode][1],
                    line=dict(width=2, color=t["surface"])),
        text=[f"{v*100:.0f}%" for v in d["skip_rate"]],
        textposition="outside",
        textfont=dict(color=t["text_secondary"], size=11, family=FONT),
        hovertemplate=("<b>%{y}</b><br>%{x:.1f}%% of plays skipped"
                       "<br>%{customdata:,.0f} h listened<extra></extra>"),
        customdata=d["hours"],
    ))
    fig = _base_layout(fig, mode,
                       f"Skip rate by genre (>= {min_hours:.0f} h listened)",
                       height=max(360, 30 * len(d) + 130),
                       xlabel="share of plays skipped (%)")
    fig.update_yaxes(showgrid=False)
    return fig


def _fade(hex_colour: str, alpha: float = 0.14) -> str:
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"
