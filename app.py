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

from floodbeta import flood_data, scorer
from floodbeta.providers.flood.fema import FEMAFloodProvider
from floodbeta.providers.locations import edgar, epa

SOURCE_EDGAR = "SEC EDGAR"
SOURCE_EPA = "EPA FRS"
SOURCES = [SOURCE_EDGAR, SOURCE_EPA]

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

# Per-facility coordinate provenance. A run can mix both: EPA supplies
# coordinates for most facilities but not all, and the rest fall through to
# Nominatim city centroids. A blanket claim either way would be wrong.
PRECISION_GEOCODED = "city-level (geocoded)"
PRECISION_NONE = "—"

# EPA name matching returns every business registered under a similar name,
# so a large single-state count is a signal to check the list, not a win.
NOISE_THRESHOLD = 50


def precision_label(facility: dict, geocoded: bool) -> str:
    """Where this facility's coordinate actually came from."""
    if not geocoded:
        return PRECISION_NONE
    if facility.get("lat") is None or facility.get("lon") is None:
        return PRECISION_GEOCODED
    source = facility.get("source") or "provider"
    # "EPA FRS" reads better as "EPA" in a per-row label.
    return f"facility-level ({'EPA' if source.startswith('EPA') else source})"


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
                "Precision": precision_label(facility, geocoded),
                "Note": "" if geocoded else NOT_FOUND_NOTE,
                "_lat": point["lat"],
                "_lon": point["lon"],
                "_geocoded": geocoded,
            }
        )
    return rows


STATE_VALIDATION_MESSAGE = (
    "Select at least one state, or check 'Search all states'"
)


def resolve_states(source: str, selected, search_all: bool):
    """Decide the EPA state scope. Returns (states, validation_error).

    `states` of None means nationwide; the provider expands that to every
    jurisdiction. A validation error means the pipeline must not run.

    Nationwide is opt-in: with no states chosen and the box unchecked, the
    run is blocked rather than silently starting a four-minute scan. The
    checkbox overrides the selector when both are set.

    Only EPA consumes states — SEC EDGAR reads a filing and has no state
    dimension, so it is never blocked by this.
    """
    if source != SOURCE_EPA:
        return None, None
    if search_all:
        return None, None
    if selected:
        return list(selected), None
    return None, STATE_VALIDATION_MESSAGE


def make_location_provider(source: str, states: list | None = None):
    """Build the location provider for the selected source."""
    if source == SOURCE_EPA:
        return epa.EpaLocationProvider(states=states)
    return edgar.EdgarLocationProvider()


def run_pipeline(ticker: str, source: str, states: list | None = None) -> dict:
    """location provider -> flood_data -> scorer.

    Returns a dict carrying `error` on failure rather than raising, so the
    UI can show a clean message instead of a stack trace.
    """
    location_provider = make_location_provider(source, states)

    try:
        facilities = location_provider.get_facilities(ticker)
    except (edgar.EdgarError, epa.EpaError) as exc:
        return {"ticker": ticker, "source": source, "error": str(exc)}

    flood_provider = FEMAFloodProvider()
    risk_points = flood_data.get_risk_points_for_facilities(
        facilities, flood_provider
    )

    return {
        "ticker": ticker,
        "source": source,
        "error": None,
        "provider": location_provider,
        "filing_info": location_provider.get_filing_info(),
        "rows": build_rows(facilities, risk_points),
        "result": scorer.calculate_floodbeta(risk_points),
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


def render_provenance(pipeline: dict) -> None:
    """Provenance adapts to the source: a filing for EDGAR, a registry for EPA.

    get_filing_info() returns None for EPA by contract, which is the signal
    that there is no filing to cite.
    """
    info = pipeline.get("filing_info")

    if info:
        st.markdown(f"**{info['company']}** — {info['form']} filed {info['filing_date']}")
        st.link_button("View filing on SEC EDGAR", info["url"])
        return

    provider = pipeline.get("provider")
    matched = getattr(provider, "matched_name", None)
    st.markdown("**EPA Facility Registry Service**")
    if matched:
        st.caption(f"Matched on registered name: “{matched}”")
    searched = getattr(provider, "searched_states", ())
    if searched:
        scope = "all states" if len(searched) > 10 else ", ".join(searched)
        st.caption(f"Searched: {scope}")
    st.link_button("About EPA FRS", epa.REGISTRY_URL)


def render_precision_note(rows: list) -> None:
    """State the actual precision mix rather than a blanket claim.

    A run can be entirely provider coordinates, entirely geocoded centroids,
    or any mix of the two, so the counts are computed from the rows rather
    than inferred from which source was selected.
    """
    provider_count = sum(
        1 for row in rows if row["Precision"].startswith("facility-level")
    )
    geocoded_count = sum(
        1 for row in rows if row["Precision"] == PRECISION_GEOCODED
    )

    parts = []
    if provider_count:
        parts.append(f"{provider_count} facility-level (provider coordinates)")
    if geocoded_count:
        parts.append(f"{geocoded_count} city-level (geocoded to a city centroid)")

    if not parts:
        st.caption("**Precision:** no facility could be located.")
        return

    st.caption(
        f"**Precision:** {', '.join(parts)}. "
        "City-level points reflect a city centroid, not a building location — "
        "see the Precision column for which is which."
    )


def render_source_notices(pipeline: dict) -> None:
    """Surface EPA-specific caveats that would otherwise be invisible."""
    provider = pipeline.get("provider")
    if pipeline.get("source") != SOURCE_EPA or provider is None:
        return

    noisy = {
        state: count
        for state, count in (getattr(provider, "state_counts", None) or {}).items()
        if count > NOISE_THRESHOLD
    }
    if noisy:
        st.warning(
            "Large number of results — EPA name matching may include "
            "third-party businesses registered under similar names. Review "
            "the facility list carefully."
        )

    if getattr(provider, "truncated", False):
        st.warning(
            f"Results capped at {provider.max_facilities} facilities. The score "
            "reflects that subset, not the full registered footprint."
        )
    skipped = getattr(provider, "skipped_states", [])
    if skipped:
        st.warning(
            f"{len(skipped)} state(s) could not be queried "
            f"({', '.join(skipped[:8])}{'…' if len(skipped) > 8 else ''}) — "
            "EPA FRS rate limiting. Facilities there are missing from the score."
        )


st.title("🌊 FloodBeta")
st.caption(
    "Physical flood risk exposure for public equities, from disclosed "
    "facility locations."
)

with st.form("ticker_form"):
    left, right = st.columns([2, 1])
    with left:
        raw_ticker = st.text_input(
            "Ticker", placeholder="TSLA", help="Leading '$' is fine — it is stripped."
        )
    with right:
        source = st.selectbox(
            "Location source",
            SOURCES,
            index=SOURCES.index(SOURCE_EDGAR),
            help="SEC EDGAR reads 10-K Item 2 (city-level). EPA FRS returns "
            "registered facilities with real coordinates.",
        )

    epa_states = st.multiselect(
        "EPA states",
        epa.US_STATES,
        default=[],
        placeholder="Select states to search",
        help="EPA FRS requires a state per request and allows only 12 requests "
        "per minute, so each state adds about five seconds. Ignored for "
        "SEC EDGAR.",
    )
    search_all_states = st.checkbox(
        "Search all states (slow — ~4 min)",
        value=False,
        help="Queries all 52 jurisdictions. Overrides the state selector.",
    )
    submitted = st.form_submit_button("Screen")

if submitted and raw_ticker.strip():
    ticker = edgar.normalize_ticker(raw_ticker)
    states, validation_error = resolve_states(source, epa_states, search_all_states)

    if validation_error:
        st.warning(validation_error)
    else:
        # Cache key is ticker + source + state scope: changing any of them
        # must re-run rather than showing the previous selection's results.
        cache_key = (
            ticker,
            source,
            tuple(states) if states else (),
            bool(search_all_states),
        )
        cached = st.session_state.get("pipeline")

        if not cached or cached.get("cache_key") != cache_key:
            spinner = f"Screening {ticker} via {source}…"
            if source == SOURCE_EPA and states is None:
                spinner += " searching all states — this takes several minutes"
            with st.spinner(spinner):
                result = run_pipeline(ticker, source, states)
            result["cache_key"] = cache_key
            st.session_state["pipeline"] = result
elif submitted:
    st.warning("Enter a ticker to screen.")

pipeline = st.session_state.get("pipeline")

if pipeline:
    if pipeline["error"]:
        st.error(pipeline["error"])
    elif not pipeline["rows"]:
        if pipeline["source"] == SOURCE_EPA:
            st.warning(
                f"No EPA-registered facilities matched {pipeline['ticker']}. EPA "
                "records use legal entity names that may differ from the SEC "
                "name; try narrowing to a state where the company operates."
            )
        else:
            st.warning(
                f"No facility locations could be extracted from "
                f"{pipeline['ticker']}'s latest 10-K. Property disclosures vary "
                "widely between filers."
            )
        render_provenance(pipeline)
        render_source_notices(pipeline)
    else:
        render_score(pipeline["result"])
        render_source_notices(pipeline)
        st.divider()

        left, right = st.columns([3, 2])
        with left:
            st.subheader("Facility map")
            render_map(pipeline["rows"])
        with right:
            st.subheader("Source")
            render_provenance(pipeline)

        st.subheader("Per-facility breakdown")
        render_table(pipeline["rows"])

    st.divider()
    # Transparency disclosures required by CLAUDE.md.
    render_precision_note(pipeline.get("rows") or [])
    st.caption(
        "**Data source:** FEMA NFHL via hazards.fema.gov. Updated annually."
    )
