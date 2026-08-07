"""
Main Streamlit entry point for FloodBeta.

Drives the end-to-end pipeline: takes a ticker from the user, calls edgar.py
to extract facility locations from the latest 10-K, hands them to
flood_data.py (which geocodes and routes to the configured provider), and
aggregates the result via scorer.py. Presents the score, map, per-facility
breakdown, and filing provenance.

Positional contract: flood_data.get_risk_points() returns exactly one
RiskPoint per input address, in order, so facilities[i] pairs with
risk_points[i]. Neither list is ever filtered independently — rows are built
by index first, and only the assembled rows are filtered for display.
"""

import os

import pandas as pd
import pydeck as pdk
import streamlit as st
from dotenv import load_dotenv

from floodbeta import edgar, flood_data, scorer
from floodbeta.providers.fema import FEMAFloodProvider

# SEC and Nominatim both reject requests without a descriptive, contactable
# user agent. See .env.example.
CREDENTIAL_KEYS = ("SEC_USER_AGENT", "NOMINATIM_USER_AGENT")

st.set_page_config(page_title="FloodBeta", page_icon="🌊", layout="wide")


def load_credentials() -> None:
    """Populate credential env vars from .env locally, st.secrets on Cloud.

    Streamlit Community Cloud has no .env; it exposes st.secrets instead.
    Local development has no secrets.toml. Both paths are attempted and
    neither is required, so the same code runs in both places.

    Precedence: **secrets win over environment variables.** Reading
    st.secrets at all causes Streamlit to export every secret into
    os.environ, overwriting values already there — so a secrets.toml entry
    beats an exported shell variable or .env, and the setdefault below is a
    no-op in that case. It is kept as a fallback in case that export
    behavior changes. Locally, where no secrets file exists, .env wins
    because nothing overwrites it.

    Empty values are skipped deliberately. edgar.py and geocoder.py read
    these with os.environ.get(key, DEFAULT), which returns the empty string
    rather than DEFAULT once the key exists — writing "" would replace the
    placeholder user agent with nothing and turn a clear setup message into
    an opaque 403.
    """
    load_dotenv()

    for key in CREDENTIAL_KEYS:
        try:
            value = st.secrets.get(key, "")
        except Exception:
            # Raises when no secrets.toml exists, which is the normal local
            # case. The exception type has changed across Streamlit versions
            # (FileNotFoundError, now StreamlitSecretNotFoundError), so this
            # deliberately catches broadly rather than pinning a version.
            value = ""

        if value:
            os.environ.setdefault(key, str(value))


# Must run before any pipeline call reads these variables.
load_credentials()

# Risk palette, shared by the score banner and the map pins.
RISK_HEX = {
    "High": "#d64545",
    "Moderate": "#e0a13a",
    "Low": "#3f9d5a",
    "Unknown": "#8a8f98",
}
RISK_RGB = {
    "High": [214, 69, 69],
    "Moderate": [224, 161, 58],
    "Low": [63, 157, 90],
    "Unknown": [138, 143, 152],
}
INSUFFICIENT_HEX = "#8a8f98"

NOT_FOUND_NOTE = "Location not found"


def fema_zone(risk_point: dict) -> str:
    """Read the FEMA zone out of a RiskPoint's preserved raw payload.

    Display-only. `raw` exists precisely so the UI can show what the
    provider actually returned; no scoring decision is made from it.
    """
    features = risk_point.get("raw", {}).get("features") or []
    if not features:
        return "—"
    return features[0].get("attributes", {}).get("FLD_ZONE") or "—"


def build_rows(facilities: list, risk_points: list) -> list:
    """Pair facilities to RiskPoints by index into display rows."""
    if len(facilities) != len(risk_points):
        raise ValueError(
            f"Pipeline desync: {len(facilities)} facilities but "
            f"{len(risk_points)} risk points — these must stay parallel."
        )

    rows = []
    for facility, point in zip(facilities, risk_points):
        geocoded = point.get("geocoded", True)
        rows.append(
            {
                "Facility": facility["name"] or "—",
                "Address": facility["address"],
                "FEMA Zone": fema_zone(point) if geocoded else "—",
                # An ungeocoded facility carries a placeholder score that the
                # scorer excludes; showing it would imply it counted. NaN
                # rather than None keeps the column float-typed, so Streamlit
                # renders it blank instead of the literal text "None".
                "Risk Score": point["risk_score"] if geocoded else float("nan"),
                "Risk Label": point["risk_label"],
                "Note": "" if geocoded else NOT_FOUND_NOTE,
                "_lat": point["lat"],
                "_lon": point["lon"],
                "_geocoded": geocoded,
            }
        )
    return rows


def run_pipeline(ticker: str) -> dict:
    """edgar -> flood_data -> scorer. Returns a dict with `error` on failure."""
    try:
        report = edgar.get_facility_report(ticker)
    except edgar.EdgarError as exc:
        return {"ticker": ticker, "error": str(exc)}

    provider = FEMAFloodProvider()
    risk_points = flood_data.get_risk_points(report["addresses"], provider)
    result = scorer.calculate_floodbeta(risk_points)

    return {
        "ticker": ticker,
        "error": None,
        "report": report,
        "rows": build_rows(report["facilities"], risk_points),
        "result": result,
    }


def render_score(result: dict) -> None:
    score = result["score"]
    label = result["label"]
    colour = INSUFFICIENT_HEX if score is None else RISK_HEX.get(label, INSUFFICIENT_HEX)
    display = "—" if score is None else f"{score:.2f}"

    left, right = st.columns([1, 2])
    with left:
        st.markdown(
            f"""
            <div style="line-height:1;">
              <div style="font-size:0.85rem;letter-spacing:0.08em;
                          text-transform:uppercase;opacity:0.7;">FloodBeta</div>
              <div style="font-size:4.5rem;font-weight:700;color:{colour};">
                {display}</div>
              <div style="font-size:1.4rem;font-weight:600;color:{colour};">
                {label}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with right:
        counted, total = result["geocoded_count"], result["facility_count"]
        a, b = st.columns(2)
        a.metric("Facilities found", total)
        b.metric("Facilities scored", f"{counted}/{total}")
        if score is None:
            st.warning(
                "No facility could be placed on the map, so no score was "
                "calculated. A number here would be misleading."
            )
        elif counted < total:
            st.info(
                f"{total - counted} facility(ies) could not be geocoded and "
                "are excluded from the score. They are listed below."
            )


def render_map(rows: list) -> None:
    mapped = [row for row in rows if row["_geocoded"]]
    if not mapped:
        st.caption("No mapped facilities to display.")
        return

    frame = pd.DataFrame(
        [
            {
                "lat": row["_lat"],
                "lon": row["_lon"],
                "facility": row["Facility"],
                "address": row["Address"],
                "zone": row["FEMA Zone"],
                "label": row["Risk Label"],
                "colour": RISK_RGB.get(row["Risk Label"], RISK_RGB["Unknown"]),
            }
            for row in mapped
        ]
    )

    st.pydeck_chart(
        pdk.Deck(
            map_style=pdk.map_styles.CARTO_LIGHT,
            initial_view_state=pdk.ViewState(
                latitude=float(frame["lat"].mean()),
                longitude=float(frame["lon"].mean()),
                zoom=3.2,
            ),
            layers=[
                pdk.Layer(
                    "ScatterplotLayer",
                    data=frame,
                    get_position="[lon, lat]",
                    get_fill_color="colour",
                    get_radius=42000,
                    pickable=True,
                    opacity=0.75,
                    stroked=True,
                    get_line_color=[255, 255, 255],
                    line_width_min_pixels=1,
                )
            ],
            tooltip={
                "html": "<b>{facility}</b><br/>{address}<br/>"
                "Zone {zone} — {label}",
            },
        )
    )


def render_table(rows: list) -> None:
    frame = pd.DataFrame(rows).drop(columns=["_lat", "_lon", "_geocoded"])
    st.dataframe(
        frame,
        hide_index=True,
        width="stretch",
        column_config={
            "Risk Score": st.column_config.NumberColumn(format="%.2f"),
        },
    )


def render_provenance(report: dict) -> None:
    st.markdown(
        f"**{report['company']}** — {report['form']} filed {report['filing_date']}"
    )
    st.link_button("View filing on SEC EDGAR", report["url"])


st.title("🌊 FloodBeta")
st.caption(
    "Physical flood risk exposure for public equities, from SEC-reported "
    "facility locations."
)

with st.form("ticker_form"):
    raw_ticker = st.text_input(
        "Ticker", placeholder="TSLA", help="Leading '$' is fine — it is stripped."
    )
    submitted = st.form_submit_button("Screen")

if submitted and raw_ticker.strip():
    ticker = edgar.normalize_ticker(raw_ticker)
    cached = st.session_state.get("pipeline")

    # Only re-run when the ticker actually changed.
    if not cached or cached["ticker"] != ticker:
        with st.spinner(f"Screening {ticker} — reading 10-K, geocoding, querying FEMA…"):
            st.session_state["pipeline"] = run_pipeline(ticker)
elif submitted:
    st.warning("Enter a ticker to screen.")

pipeline = st.session_state.get("pipeline")

if pipeline:
    if pipeline["error"]:
        st.error(pipeline["error"])
    elif not pipeline["rows"]:
        st.warning(
            f"No facility locations could be extracted from {pipeline['ticker']}'s "
            "latest 10-K. Property disclosures vary widely between filers."
        )
        render_provenance(pipeline["report"])
    else:
        render_score(pipeline["result"])
        st.divider()

        left, right = st.columns([3, 2])
        with left:
            st.subheader("Facility map")
            render_map(pipeline["rows"])
        with right:
            st.subheader("Filing")
            render_provenance(pipeline["report"])

        st.subheader("Per-facility breakdown")
        render_table(pipeline["rows"])

    st.divider()
    # Transparency disclosures required by CLAUDE.md.
    st.caption(
        "**Precision:** city-level only — scores reflect city centroids, "
        "not building locations."
    )
    st.caption(
        "**Data source:** FEMA NFHL via hazards.fema.gov. Updated annually."
    )
