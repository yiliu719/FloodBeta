# FloodBeta — CLAUDE.md

## Project Overview
FloodBeta is a physical climate risk screener for public equities. It takes a stock ticker, extracts the company's major facility locations from SEC 10-K filings, geocodes them, and overlays flood zone data to produce a physical climate exposure score — the "flood beta" of a company's asset base.

**Target users:** Climate-focused investors, portfolio analysts, and risk researchers who want to quickly assess a public company's physical flood exposure without manual research.

**Deployment target:** Streamlit Community Cloud (keep dependencies minimal and compatible)

**Strategic context:** FloodBeta is the first module of a broader physical risk intelligence platform called HazardBeta. Architecture decisions should support future expansion to other hazard types (wildfire, earthquake, tsunami) and other asset classes (private infrastructure, real assets) — but FloodBeta itself stays focused on flood risk for public equities only.

---

## Stack
- **Language:** Python 3.11+
- **Frontend:** Streamlit
- **Data sources:**
  - SEC EDGAR full-text search API (free, no key required) for 10-K filings
  - FEMA National Flood Hazard Layer (NFHL) REST API (free, no key required) — primary flood data provider
    - Endpoint: `https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query` (layer 28 = "Flood Hazard Zones")
    - **Not** `msc.fema.gov` — that host serves the human-facing Map Service Center website and returns `Service not found` for this path
  - `geopy` with Nominatim geocoder for address → lat/lon
- **Key libraries:** `streamlit`, `requests`, `geopy`, `pandas`, `pydeck` (for map viz), `python-dotenv`

---

## Project Structure
```
FloodBeta/
├── CLAUDE.md               # This file
├── README.md               # Product-facing readme (written like a product brief)
├── LICENSE                 # MIT
├── requirements.txt        # All dependencies pinned
├── .gitignore              # Python gitignore (already set up)
├── .env.example            # Template for any future API keys
├── app.py                  # Main Streamlit entry point
├── floodbeta/
│   ├── __init__.py
│   ├── edgar.py            # SEC EDGAR: ticker → 10-K → facility locations
│   ├── geocoder.py         # Address → lat/lon using geopy
│   ├── flood_data.py       # Flood zone lookup — routes to configured provider
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── base.py         # Abstract base class: FloodDataProvider + normalized output schema
│   │   └── fema.py         # FEMA implementation of FloodDataProvider
│   └── scorer.py           # Aggregates normalized risk scores into FloodBeta score
└── tests/
    └── test_basic.py       # Basic smoke tests
```

---

## Data Provider Architecture (Critical)
The scoring methodology is split into two layers. **Never conflate them.**

### Layer 1 — Provider normalization (provider-specific, lives in providers/)
Each provider converts its raw output format into a **normalized RiskPoint schema**.
This is where provider-specific logic lives — zone names, depth values, raster lookups, etc.

```python
# providers/base.py — normalized output schema every provider must return
# This is the ONLY format scorer.py ever sees

from typing import TypedDict

# Risk labels a provider may emit. Kept here, not in scorer.py, so every
# provider agrees on the vocabulary.
RISK_LABELS = ("Low", "Moderate", "High", "Unknown")


class RiskPoint(TypedDict):
    """Normalized per-facility risk record."""

    lat: float          # Facility latitude
    lon: float          # Facility longitude
    risk_score: float   # 0.0 (no risk) -> 1.0 (highest risk), provider normalized
    risk_label: str     # Human-readable: "Low" / "Moderate" / "High" / "Unknown"
    source: str         # Provider name e.g. "FEMA", "First Street"
    raw: dict           # Raw provider output, preserved for debugging/transparency


# Abstract base class — all providers must implement this
class FloodDataProvider:
    """Interface every flood data provider must implement."""

    def get_risk_point(self, lat: float, lon: float) -> RiskPoint:
        raise NotImplementedError

    def get_provider_name(self) -> str:
        raise NotImplementedError
```

`RiskPoint` is a `TypedDict`, so it is a plain `dict` at runtime — providers
build and return dict literals as before, and the annotation only documents
and type-checks the shape. `risk_label` must be one of `RISK_LABELS`.

### Layer 2 — FloodBeta scoring (provider-agnostic, lives in scorer.py)
scorer.py receives a list of RiskPoints and aggregates them into a single FloodBeta score.
It never sees zone names, depth values, or any provider-specific data — only normalized risk_scores.

```python
# FloodBeta = mean(risk_scores) across all facilities
# Weighted equally per facility (future: weight by facility size/revenue if data available)

FloodBeta score ranges:
  0.0 – 0.2  →  Low exposure
  0.2 – 0.5  →  Moderate exposure
  0.5 – 1.0  →  High exposure
```

---

## Provider-Specific Normalization Rules

### FEMA National Flood Hazard Layer (current)
FEMA outputs categorical flood zone labels in `FLD_ZONE`, with a `ZONE_SUBTY`
qualifier. Normalize to risk_score as follows:

| FEMA Zone | Description | risk_score |
|-----------|-------------|------------|
| AE, A, AO, AH, A1-30 | 100-year floodplain (SFHA) | 1.0 |
| AE (floodway) | Highest velocity flood path — arrives as `FLD_ZONE=AE` + `ZONE_SUBTY=FLOODWAY` | 1.0 |
| V, VE, V1-30 | Coastal high hazard — SFHA *with* wave action | 1.0 |
| AR, A99 | SFHA pending or behind planned protection | 1.0 |
| X (shaded) / B | 500-year / moderate risk | 0.3 |
| X (levee-protected) | Dry only because a levee holds — residual risk is real | 0.3 |
| X (unshaded) / C | Minimal risk | 0.05 |
| D / "AREA NOT INCLUDED" / unrecognized / no data | Undetermined | 0.1 |

**Zone X requires reading `ZONE_SUBTY`** — the zone code alone cannot separate
the three X cases. Treat X as 0.3 when the subtype contains `0.2 PCT` (the
500-year floodplain) or `LEVEE` (e.g. `AREA WITH REDUCED FLOOD RISK DUE TO
LEVEE`); otherwise 0.05.

Zone matching is case- and whitespace-insensitive. Numbered legacy zones
(A1-A30, V1-V30) come from older FIRMs and are equivalent to their unnumbered
forms.

**Overlapping polygons resolve to the highest risk present.** A point on a
zone boundary can intersect several polygons; the query returns all of them
and the provider takes the maximum risk_score rather than whichever the
service happened to list first. Flood screening should err conservative.

**ArcGIS reports failures with HTTP 200 and an `error` key in the body**, so
the status code alone is not a success signal. Any failure — network error,
error body, malformed JSON, or a point outside NFHL coverage — yields
risk_score 0.1 / "Unknown", never an exception. Note that no coverage means
*no data*, which is not the same as low risk.

### Future providers — inundation depth / water surface elevation rasters
Providers like First Street Foundation, NOAA inundation models, and USGS flood products
output continuous variables (depth in feet or water surface elevation), not zone labels.
Normalize using a depth-damage curve consistent with FEMA HAZUS methodology:

| Inundation Depth | risk_score | Rationale |
|-----------------|------------|-----------|
| 0 ft | 0.0 | No flooding |
| 0–1 ft | 0.2 | Nuisance flooding, minimal structural damage |
| 1–3 ft | 0.5 | Significant ground-floor asset damage |
| 3–6 ft | 0.8 | Major structural damage |
| 6+ ft | 1.0 | Catastrophic |

This curve is defensible because it aligns with FEMA's own depth-damage functions.
Document the source in any UI that displays scores from depth-based providers.

---

## Core Workflow (Pipeline)
```
Ticker input
    → edgar.py: fetch latest 10-K, extract facility/property addresses
    → geocoder.py: convert addresses to lat/lon coordinates
    → flood_data.py: route to configured provider → returns list of RiskPoints
    → scorer.py: aggregate RiskPoints into FloodBeta score (0.0–1.0)
    → app.py: display score, map, and per-facility breakdown in Streamlit UI
```

---

## Commands
```bash
# Install dependencies
pip install -r requirements.txt

# Run the app locally
streamlit run app.py

# Run tests
python -m pytest tests/
```

---

## Development Principles
- **Working demo over perfect architecture** — prioritize getting something runnable end-to-end before optimizing
- **Hard separation between provider normalization and scoring** — scorer.py must never contain provider-specific logic; providers must never contain scoring aggregation logic
- **Fail gracefully** — if SEC EDGAR returns no locations, or a provider returns no data, show a clear message rather than crashing
- **Transparency over black box** — always show the user what facilities were found, which provider was used, and how the score was calculated
- **Keep it simple** — no database, no auth, no backend server; pure Streamlit + API calls
- **Rate limit awareness** — EDGAR and Nominatim both have rate limits; add `time.sleep()` between batch requests
- **Preserve raw provider output** — always include `raw` field in RiskPoint so users and developers can audit the underlying data

---

## Things to Avoid
- Don't use paid APIs — all data sources must be free and publicly accessible
- Don't store user queries or results — this is a stateless demo tool
- Don't over-engineer the scoring model — it should be explainable in one paragraph
- Don't use a database — keep state in Streamlit session state only
- Don't add authentication — this is a public portfolio demo
- Don't expand scope to non-flood hazards in this repo — that belongs in future HazardBeta modules
- Don't expand asset coverage beyond SEC-reported facilities — commodity/agricultural asset coverage is out of scope for FloodBeta
- Don't put FEMA zone logic in scorer.py — it belongs in providers/fema.py
- Don't put scoring aggregation logic in any provider — it belongs in scorer.py

---

## Future Vision (HazardBeta Platform)
FloodBeta is module one of a planned HazardBeta platform. Future repos under a HazardBeta GitHub organization:

```
HazardBeta (umbrella org / landing page)
├── FloodBeta       ← this repo
├── FireBeta        (wildfire exposure — USGS/CAL FIRE data)
├── QuakeBeta       (seismic exposure — USGS ShakeMap)
├── hazardbeta-core (shared library — base classes, geocoder utils, scorer primitives)
```

Adjacent product concepts (separate products, not HazardBeta modules):
- Agricultural/commodity climate impact tool ("HarvestBeta") — answers questions like
  "how did spring drought impact the peach harvest and hence futures prices?"
  Fundamentally different data model and user; keep separate.

Architecture decisions in FloodBeta should not block these expansions, but should not
prematurely implement them either.

---

## README Tone
The README.md should be written like a product brief, not a code readme. It should explain:
1. What problem this solves and for whom
2. How the FloodBeta score works (methodology — both layers)
3. How to run it locally
4. Current data provider (FEMA) and limitations
5. Future directions (reference HazardBeta vision)

Avoid: "This is a Python script that..."
Prefer: "FloodBeta helps investors quantify..."
