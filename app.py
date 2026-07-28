"""Stage 4 — Streamlit dashboard over the Parquet dataset.

    .venv/bin/streamlit run app.py

Every chart comes from `figures.py`; nothing is plotted here. Parquet is queried
directly through DuckDB and each load is cached, so widget interaction does not
re-read the dataset.
"""

from __future__ import annotations

import duckdb
import pandas as pd
import streamlit as st

import config
import figures

st.set_page_config(page_title="Spotify genre trends", page_icon="🎧",
                   layout="wide", initial_sidebar_state="expanded")

SECONDARY_DIR = config.DATA_DIR / "secondary"


# --------------------------------------------------------------------------
# Data access — DuckDB over Parquet, cached so widgets are cheap
# --------------------------------------------------------------------------


@st.cache_data(show_spinner=False)
def load_trends(variant: str) -> pd.DataFrame:
    return duckdb.connect().execute(
        f"SELECT * FROM '{config.TAG_TRENDS_PARQUET}' WHERE variant = ?",
        [variant],
    ).df()


@st.cache_data(show_spinner=False)
def load_tag_totals(variant: str) -> pd.DataFrame:
    """Total attributed listening hours per tag — drives ranking and the floor."""
    return duckdb.connect().execute(
        f"""
        SELECT tag, sum(tag_seconds) / 3600.0 AS hours
        FROM '{config.TAG_TRENDS_PARQUET}'
        WHERE variant = ?
        GROUP BY tag ORDER BY hours DESC
        """,
        [variant],
    ).df()


@st.cache_data(show_spinner=False)
def load_secondary(name: str) -> pd.DataFrame:
    path = SECONDARY_DIR / f"{name}.parquet"
    if not path.exists():
        return pd.DataFrame()
    return duckdb.connect().execute(f"SELECT * FROM '{path}'").df()


@st.cache_data(show_spinner=False)
def load_headline(variant: str) -> dict:
    con = duckdb.connect()
    plays = con.execute(
        f"""
        SELECT count(*), sum(played_seconds)/3600.0,
               count(DISTINCT artist_name), min(month), max(month)
        FROM '{config.PLAYS_PARQUET}'
        """
    ).fetchone()
    return {"plays": plays[0], "hours": plays[1], "artists": plays[2],
            "first": plays[3], "last": plays[4]}


def missing_data_notice() -> bool:
    if config.TAG_TRENDS_PARQUET.exists():
        return False
    st.error("No `tag_trends.parquet` yet. Run the pipeline first:")
    st.code("\n".join([
        ".venv/bin/python ingest.py",
        ".venv/bin/python credits.py",
        ".venv/bin/python enrich.py",
        ".venv/bin/python analyze.py",
    ]), language="bash")
    return True


# --------------------------------------------------------------------------


def sidebar_controls(totals: pd.DataFrame, trends: pd.DataFrame) -> dict:
    st.sidebar.title("🎧 Genre trends")

    mode = "dark" if st.sidebar.toggle("Dark mode", value=False) else "light"

    st.sidebar.divider()
    st.sidebar.caption("ATTRIBUTION")
    variant_label = st.sidebar.radio(
        "Featured artists",
        ["Credit featured artists", "Album artist only"],
        help=("The export credits only the album artist. 'Credit featured "
              "artists' splits a play's time with performers named in the "
              "track title; 'Album artist only' reproduces the raw export "
              "behaviour."),
    )
    variant = ("with_features" if variant_label == "Credit featured artists"
               else "album_artist_only")

    st.sidebar.divider()
    st.sidebar.caption("FILTERS")

    months = pd.to_datetime(sorted(trends["month"].unique()))
    lo, hi = st.sidebar.select_slider(
        "Date range",
        options=list(months),
        value=(months[0], months[-1]),
        format_func=lambda d: pd.Timestamp(d).strftime("%b %Y"),
    )

    smoothed = st.sidebar.toggle(
        "Smoothed (3-month rolling mean)", value=True,
        help="Raw monthly shares are noisy; smoothing is on by default.",
    )

    max_hours = float(totals["hours"].max()) if not totals.empty else 1.0
    min_hours = st.sidebar.slider(
        "Minimum listening time per genre (hours)",
        min_value=0.0, max_value=round(max_hours * 0.25, 1),
        value=min(10.0, round(max_hours * 0.05, 1)), step=1.0,
        help="Filters long-tail genre noise out of the views.",
    )

    return {"mode": mode, "variant": variant, "lo": lo, "hi": hi,
            "smoothed": smoothed, "min_hours": min_hours}


def page_trends(ctrl: dict) -> None:
    trends = load_trends(ctrl["variant"])
    totals = load_tag_totals(ctrl["variant"])

    # Ranking is computed on the FULL dataset, never the filtered view, so the
    # colour a genre gets does not change when a filter removes another genre.
    ranked_all = totals["tag"].tolist()
    eligible = totals[totals["hours"] >= ctrl["min_hours"]]["tag"].tolist()
    ranked = [t for t in ranked_all if t in set(eligible)]

    d = trends.copy()
    d["month"] = pd.to_datetime(d["month"])
    d = d[(d["month"] >= ctrl["lo"]) & (d["month"] <= ctrl["hi"])]
    d = d[d["tag"].isin(ranked)]

    head = load_headline(ctrl["variant"])
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Listening", f"{head['hours']:,.0f} h")
    c2.metric("Plays", f"{head['plays']:,}")
    c3.metric("Artists", f"{head['artists']:,}")
    c4.metric("Genres shown", f"{len(ranked):,}")

    st.plotly_chart(
        figures.stacked_area_top_tags(d, ranked_all, smoothed=ctrl["smoothed"],
                                      mode=ctrl["mode"]),
        use_container_width=True,
    )

    # Relief for the light-mode contrast warning: the numbers are always
    # reachable as text, not only as colour.
    with st.expander("Show the numbers behind this chart"):
        table = (d[d["tag"].isin(ranked_all[:figures.MAX_SERIES])]
                 .pivot_table(index="month", columns="tag",
                              values="smoothed_share" if ctrl["smoothed"] else "share")
                 .sort_index(ascending=False))
        st.dataframe((table * 100).round(2), use_container_width=True)

    st.divider()
    st.subheader("Rising and declining")
    st.plotly_chart(figures.rising_declining(d, mode=ctrl["mode"]),
                    use_container_width=True)

    st.divider()
    st.subheader("Individual trajectories")
    default = ranked[:6]
    picked = st.multiselect("Genres", options=ranked, default=default,
                            max_selections=12)
    st.plotly_chart(
        figures.tag_trajectories(d, picked, ranked_all,
                                 smoothed=ctrl["smoothed"], mode=ctrl["mode"]),
        use_container_width=True,
    )


def page_secondary(ctrl: dict) -> None:
    st.subheader("Secondary metrics")
    st.caption("Cheap counters that describe listening behaviour rather than taste.")

    disc = load_secondary("discovery_rate")
    conc = load_secondary("repeat_concentration")
    skip = load_secondary("skip_rate_by_tag")

    if not disc.empty:
        st.plotly_chart(figures.discovery_rate(disc, mode=ctrl["mode"]),
                        use_container_width=True)
    st.divider()
    if not conc.empty:
        st.plotly_chart(figures.repeat_concentration(conc, mode=ctrl["mode"]),
                        use_container_width=True)
        with st.expander("Show the numbers"):
            st.dataframe(conc, use_container_width=True)
    st.divider()
    if not skip.empty:
        st.plotly_chart(figures.skip_rate_by_tag(skip, mode=ctrl["mode"]),
                        use_container_width=True)
        st.caption(
            "Derived from `plays_raw`, so the sub-30s rows the analysis "
            "otherwise drops are included — those rows are the skips."
        )


def main() -> None:
    if missing_data_notice():
        return

    trends_probe = load_trends(config.DEFAULT_VARIANT)
    if trends_probe.empty:
        st.error("`tag_trends.parquet` is empty — re-run analyze.py.")
        return

    totals_probe = load_tag_totals(config.DEFAULT_VARIANT)
    ctrl = sidebar_controls(totals_probe, trends_probe)

    st.sidebar.divider()
    page = st.sidebar.radio("Page", ["Genre trends", "Secondary metrics"])

    if page == "Genre trends":
        page_trends(ctrl)
    else:
        page_secondary(ctrl)

    st.sidebar.divider()
    st.sidebar.caption(
        "Artist attribution uses the album artist, which misattributes "
        "compilations and features. See README for the known data flaws."
    )


if __name__ == "__main__":
    main()
