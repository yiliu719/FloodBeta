# 🌊 FloodBeta

**Physical flood risk exposure for public equities — from SEC-reported facility locations.**

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Streamlit](https://img.shields.io/badge/built%20with-Streamlit-red.svg)](https://streamlit.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

🔗 **[Live Demo →](https://floodbeta.streamlit.app)**

---

## What it does

FloodBeta answers a question that most investment workflows currently ignore: **how exposed is a company's physical asset base to flood risk?**

Given a stock ticker, FloodBeta:
1. Locates the company's facility footprint from publicly available data sources
2. Geocodes each location to coordinates
3. Queries FEMA's National Flood Hazard Layer (NFHL) for the flood zone at each facility
4. Aggregates results into a single **FloodBeta score** — a 0–1 index of physical flood exposure across the company's asset base

The result is a score, a facility map, and a per-facility breakdown — all traceable back to the original data source.
<img width="1568" height="714" alt="image" src="https://github.com/user-attachments/assets/3059c330-dd3f-4878-b88e-c8224f0e8b5a" />
<img width="1568" height="307" alt="image" src="https://github.com/user-attachments/assets/e5eb5894-7383-46a7-a5c3-67c78d5b29f3" />


---

## The FloodBeta Score

FloodBeta is modeled on the concept of financial beta: instead of measuring sensitivity to market movements, it measures a company's asset-base sensitivity to flood hazard.

Each facility is assigned a risk score based on its FEMA flood zone:

| FEMA Zone | Description | Risk Score |
|-----------|-------------|------------|
| AE, A, AO, AH, A1-30 | 100-year floodplain | 1.0 |
| V, VE, V1-30 | Coastal high-hazard (wave action) | 1.0 |
| AR, A99 | Protected by federal flood control | 1.0 |
| X (shaded) / B | 500-year / moderate risk | 0.3 |
| X (levee-protected) | Reduced risk due to levee | 0.3 |
| X (unshaded) / C | Minimal risk | 0.05 |
| Unknown / undetermined | No data available | 0.1 |

**FloodBeta = unweighted mean of facility risk scores**

| Score Range | Label |
|------------|-------|
| 0.0 – 0.2 | 🟢 Low |
| 0.2 – 0.5 | 🟡 Moderate |
| 0.5 – 1.0 | 🔴 High |

The score is provider-agnostic by design: the underlying architecture normalizes any flood data source to a common 0–1 risk scale, so future providers can be swapped in without changing the scoring logic.

---

## Example Results

| Ticker | Company | Facilities | FloodBeta | Label |
|--------|---------|-----------|-----------|-------|
| TSLA | Tesla, Inc. | 5 | 0.10 | 🟢 Low |
| LMT | Lockheed Martin | 22 | 0.14 | 🟢 Low |
| DAR | Darling Ingredients | 74 | 0.13 | 🟢 Low |

*Tesla's Austin Gigafactory and Lathrop Megafactory both sit behind levees — scored 0.30 (Moderate) individually. Lockheed's Cape Canaveral facility scores 1.0 (High) as a coastal V-zone site, visible as a red pin on the map.*

---

## Data Sources

All current data sources are free and publicly accessible — no API keys required.

| Source | Use | Notes |
|--------|-----|-------|
| [SEC EDGAR](https://www.sec.gov/cgi-bin/browse-edgar) | Facility location extraction | `data.sec.gov/submissions` API |
| [FEMA NFHL](https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer) | Flood zone lookup | Layer 28: Flood Hazard Zones |
| [OpenStreetMap / Nominatim](https://nominatim.openstreetmap.org/) | Geocoding | 1 req/sec rate limit |

---

## Architecture

FloodBeta is built around two parallel provider abstractions designed for extensibility:

```
Ticker
  └─ Asset Location Pipeline
  │    └─ providers/locations/
  │         ├─ base.py          Abstract AssetLocationProvider
  │         └─ edgar.py         SEC EDGAR: 10-K Item 2 extraction (current)
  │
  └─ Flood Risk Pipeline  
       └─ geocoder.py           Address → lat/lon (Nominatim/OSM)
       └─ flood_data.py         Orchestrates geocoder + flood provider
       └─ providers/flood/
       │    ├─ base.py          Abstract FloodDataProvider + RiskPoint schema
       │    └─ fema.py          FEMA NFHL implementation (current)
       └─ scorer.py             Provider-agnostic aggregation (0–1 floats only)
       └─ app.py                Streamlit UI
```

**Two-layer scoring architecture:**

**Layer 1 — Provider normalization:** Each flood data provider converts its raw output into a normalized `RiskPoint` with a 0–1 `risk_score`. Provider-specific logic stays inside `providers/flood/`. This separation exists because FEMA's categorical zone system doesn't generalize to the continuous inundation depth outputs from commercial providers — a depth-based provider needs its own normalization curve before the scorer can consume it.

**Layer 2 — Scoring:** `scorer.py` aggregates `RiskPoints` using only normalized floats — it never sees zone names or depth values. Adding a new flood data provider requires only a new file in `providers/flood/`, no changes to scoring logic.

---

## Running Locally

**Prerequisites:** Python 3.11+

```bash
git clone https://github.com/yiliu719/FloodBeta.git
cd FloodBeta

python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
# Edit .env and add your contact email (required by SEC EDGAR and Nominatim)
# SEC_USER_AGENT="FloodBeta/0.1 (your@email.com)"
# NOMINATIM_USER_AGENT="FloodBeta/0.1 (your@email.com)"

streamlit run app.py
```

Navigate to `http://localhost:8501` and enter a ticker.

**Running tests:**
```bash
python -m pytest tests/ -s
```

---

## Limitations

**Facility location coverage**
The current implementation extracts facility locations from SEC 10-K filings (Item 2: Properties). This works well for capital-intensive companies that enumerate specific locations — manufacturers, defense contractors, industrials. It works less well for holding companies, pure-play tech, and retailers that describe properties in aggregate prose. Future releases will supplement SEC data with additional public sources (see Roadmap).

**Geocoding precision**
All locations resolve to city centroids, not building coordinates. A city centroid may fall in a different FEMA flood zone than the actual facility. This limitation is disclosed in the UI on every result. Street-level geocoding via OpenStreetMap Overpass API is planned for named facilities.

**US facilities only**
FEMA NFHL covers the United States only. International facility locations are extracted but cannot be flood-scored and are marked Unknown.

**Spelling in source filings**
Location extraction depends on correct spelling in the source document. Misspellings in SEC filings will fail geocoding and be flagged as unresolved in the results table.

---

## Roadmap

FloodBeta is the first module of a planned **HazardBeta** platform — a physical risk intelligence suite for public equities.

**FloodBeta — Near Term**
- [ ] Additional facility location sources:
  - EPA Facility Registry Service (FRS) — free, covers ~4M US industrial facilities
  - EIA plant-level location data — energy sector
  - SEC EDGAR lease exhibits — deeper parsing of the same filings
- [ ] Street-level geocoding via OSM Overpass API for named facilities
- [ ] Facility weighting by revenue or asset value

**FloodBeta — Flood Data Providers**
- [x] FEMA NFHL (free, US only)
- [ ] Commercial flood data providers (e.g. First Street Foundation, CoreLogic, Moody's RMS) — depth-based scoring via HAZUS-aligned depth-damage curves

**HazardBeta Platform (future repos)**
- [ ] FireBeta — wildfire exposure via USGS/CAL FIRE data
- [ ] QuakeBeta — seismic exposure via USGS ShakeMap
- [ ] hazardbeta-core — shared provider abstractions and geocoder utilities

---

## About

## About

Built by [Yi Liu](https://www.linkedin.com/in/yi-liu-484321b5/) as a proof-of-concept 
exploring how physical climate risk data can be surfaced in an 
investment workflow.

Yi is a coastal engineer and Columbia Business School MBA candidate 
(Class of 2028) with a background in flood modeling, storm surge 
forecasting, and climate risk quantification. FloodBeta is a 
side project applying that domain expertise to a product question: 
what would a physical risk screener for public equities look like 
if it were built on open data?

Feedback and contributions welcome.

---

## License

MIT — see [LICENSE](LICENSE)
