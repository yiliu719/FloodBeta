# FloodBeta — CLAUDE.md

## Project Overview
FloodBeta is a physical climate risk screener for public equities. It takes a stock ticker, locates the company's facility footprint from one or more data sources, geocodes each location, overlays FEMA flood zone data, and produces a physical climate exposure score — the "flood beta" of a company's asset base.

**Target users:** Climate-focused investors, portfolio analysts, and risk researchers who want to quickly assess a public company's physical flood exposure without manual research.

**Deployment target:** Streamlit Community Cloud (keep dependencies minimal and compatible)

**Strategic context:** FloodBeta is the first module of a broader physical risk intelligence platform called HazardBeta. Architecture decisions should support future expansion to other hazard types (wildfire, earthquake, tsunami) — but FloodBeta itself stays focused on flood risk for public equities only.

---

## Stack
- **Language:** Python 3.11+
- **Frontend:** Streamlit
- **Data sources:**
  - SEC EDGAR full-text search API (free, no key required) for 10-K filings
  - EPA Facility Registry Service (FRS) API (free, no key required) for facility locations
  - FEMA Flood Map Service Center API (free, no key required) — primary flood data provider
  - `geopy` with Nominatim geocoder for address → lat/lon (skipped when provider supplies coordinates)
- **Key libraries:** `streamlit`, `requests`, `geopy`, `pandas`, `pydeck`, `python-dotenv`

---

## Project Structure
```
FloodBeta/
├── CLAUDE.md
├── README.md
├── LICENSE
├── requirements.txt
├── .gitignore
├── .env.example
├── app.py                          # Main Streamlit entry point
├── floodbeta/
│   ├── __init__.py
│   ├── geocoder.py                 # Address → lat/lon (Nominatim fallback)
│   ├── flood_data.py               # Orchestrates location provider + flood provider
│   ├── scorer.py                   # Provider-agnostic FloodBeta aggregation
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── locations/              # Asset location providers
│   │   │   ├── __init__.py
│   │   │   ├── base.py             # Abstract AssetLocationProvider + Facility schema
│   │   │   ├── edgar.py            # SEC EDGAR 10-K Item 2 extraction (moved here)
│   │   │   └── epa.py              # EPA Facility Registry Service (new)
│   │   └── flood/                  # Flood data providers
│   │       ├── __init__.py
│   │       ├── base.py             # Abstract FloodDataProvider + RiskPoint schema
│   │       └── fema.py             # FEMA NFHL implementation
└── tests/
    └── test_basic.py
```

---

## Provider Schemas

### AssetLocationProvider (providers/locations/base.py)
Every location provider must return a list of `Facility` dicts:

```python
Facility = {
    "name": str | None,      # Facility name if known (e.g. "Gigafactory Texas")
    "address": str,          # Human-readable address (city, state minimum)
    "lat": float | None,     # Pre-geocoded latitude if provider supplies it
    "lon": float | None,     # Pre-geocoded longitude if provider supplies it
    "source": str,           # Provider name e.g. "SEC EDGAR", "EPA FRS"
    "raw": dict              # Raw provider output for transparency/debugging
}

class AssetLocationProvider:
    def get_facilities(self, ticker: str) -> list[Facility]:
        raise NotImplementedError
    def get_provider_name(self) -> str:
        raise NotImplementedError
    def get_filing_info(self) -> dict | None:
        # Optional: return filing provenance (company name, date, URL)
        # Return None if not applicable (e.g. EPA doesn't have filings)
        raise NotImplementedError
```

**Key design note:** If a provider supplies `lat`/`lon`, the geocoder step is skipped for that facility — go straight to the flood zone lookup. This avoids degrading EPA's pre-geocoded coordinates to city centroids.

### FloodDataProvider (providers/flood/base.py) — unchanged
See existing implementation. RiskPoint schema and FloodDataProvider interface remain as built.

---

## Core Workflow (Updated)
```
Ticker input + user-selected location source
    → location provider (edgar.py OR epa.py) → list of Facility dicts
    → flood_data.py:
        for each facility:
            if facility has lat/lon → skip geocoder
            else → geocoder.py → lat/lon
        → flood provider → RiskPoint per facility
    → scorer.py → FloodBeta score
    → app.py → display score, map, breakdown, provenance
```

---

## UI Changes (app.py)
Add a **location source selector** to the input form:
```
[ Ticker input    ] [ Source: SEC EDGAR ▼ ] [ Screen ]
                           EPA FRS
                           SEC EDGAR
```

- Default to SEC EDGAR (existing behavior)
- When EPA FRS is selected, use `epa.py` as the location provider
- Filing provenance section should adapt: show filing info for SEC EDGAR, show "EPA Facility Registry" with a link for EPA FRS
- Session state cache key must include both ticker AND selected source — changing source should re-run the pipeline

---

## EPA FRS Implementation Notes (providers/locations/epa.py)

**API endpoint:**
```
https://ofmpub.epa.gov/frs_public2/frs_rest_services.get_facilities
?facility_name={company_name}
&state_abbr={optional}
&output=JSON
```

**Company name challenge:** EPA registrations use legal entity names that differ from SEC ticker names. Strategy:
1. Use the company name returned by SEC EDGAR's ticker lookup as the search term
2. If no results, try stripping common suffixes ("Inc.", "Corp.", "LLC", "Ltd.")
3. Return all matched facilities — don't filter by state or facility type
4. Include the matched EPA name in the `raw` field for transparency

**Pre-geocoded coordinates:** EPA FRS returns lat/lon for most facilities. Always populate `lat`/`lon` in the Facility dict when EPA provides them — do not discard and re-geocode.

**Rate limiting:** EPA FRS has no documented rate limit but add `time.sleep(0.5)` between requests as courtesy.

---

## Migration: edgar.py → providers/locations/edgar.py
The existing `floodbeta/edgar.py` must be moved to `floodbeta/providers/locations/edgar.py` and refactored to implement `AssetLocationProvider`:

- `get_facilities(ticker)` replaces `get_facility_report(ticker)` as the primary interface
- `get_filing_info()` returns the filing provenance dict (company name, form, date, URL) that `get_facility_report()` currently returns
- Internal logic (EDGAR API calls, Item 2 parsing, address extraction) stays unchanged
- `Facility` dicts should populate `lat=None`, `lon=None` since EDGAR provides city/state only
- All existing tests must continue to pass after the move

**Import compatibility:** Add a shim in `floodbeta/__init__.py` or update imports in `flood_data.py` and `tests/` — do not leave broken imports anywhere.

---

## FloodBeta Score Methodology — unchanged
See existing CLAUDE.md. Scoring logic in scorer.py is not affected by this refactor.

## Zone Scoring Table — unchanged
See existing CLAUDE.md. FEMA provider logic is not affected by this refactor.

---

## Commands
```bash
pip install -r requirements.txt
streamlit run app.py
python -m pytest tests/
```

---

## Development Principles
- **Pre-geocoded coordinates take priority** — never discard provider-supplied lat/lon in favor of re-geocoding
- **Working demo over perfect architecture** — get EPA working end-to-end before polishing
- **Fail gracefully** — if EPA returns no matches, show a clear message; don't crash
- **Transparency** — UI must show which source produced the facilities
- **Session state cache includes source** — ticker + source together are the cache key
- **All existing tests must pass** — the edgar.py migration must not break the test suite

---

## Things to Avoid
- Don't use paid APIs
- Don't store user queries or results
- Don't use a database
- Don't add authentication
- Don't expand scope to non-flood hazards in this repo
- Don't re-geocode when a provider already supplies lat/lon
- Don't break existing edgar.py functionality during the migration
- Don't put FEMA zone logic in scorer.py
- Don't put scoring aggregation logic in any provider

---

## Future Vision (HazardBeta Platform)
```
HazardBeta (umbrella org)
├── FloodBeta       ← this repo
├── FireBeta        (wildfire — USGS/CAL FIRE)
├── QuakeBeta       (seismic — USGS ShakeMap)
└── hazardbeta-core (shared base classes, geocoder utils)
```

Additional location sources on the roadmap:
- EIA plant-level data (energy sector)
- SEC EDGAR lease exhibits (deeper parsing)

Adjacent product concept: HarvestBeta (agricultural/commodity climate impact) — separate product, not a HazardBeta module.

---

## README Tone
Product brief style. Explain what problem it solves, how the score works, how to run it, and limitations. Avoid "This is a Python script that..." — prefer "FloodBeta helps investors quantify..."
