"""
Abstract base class for flood data providers, plus the normalized
RiskPoint output schema every provider must return.

RiskPoint is the only format scorer.py ever sees. All provider-specific
normalization logic (zone names, depth values, raster lookups) lives in the
subclass; scoring aggregation lives in scorer.py and never here.
"""

from __future__ import annotations

from typing import NotRequired, TypedDict

# Risk labels a provider may emit. Kept here, not in scorer.py, so every
# provider agrees on the vocabulary.
RISK_LABELS = ("Low", "Moderate", "High", "Unknown")


class RiskPoint(TypedDict):
    """Normalized per-facility risk record.

    `geocoded` defaults to True: a TypedDict cannot carry a runtime default,
    so it is declared NotRequired and an absent key means True. Providers
    never need to set it — a provider only ever returns a RiskPoint for a
    coordinate it was handed, so at the provider level the value is always
    True and omitting it is correct.

    The field exists for flood_data.py, which sits above the providers and
    does know about failed geocodes. It attaches geocoded=False for
    facilities that never got a coordinate, so scorer.py can exclude them
    from the mean while still counting them in facility_count. Without that,
    a geocoding gap would enter the average as a real measurement and
    misreport missing data as a finding.
    """

    lat: float          # Facility latitude
    lon: float          # Facility longitude
    risk_score: float   # 0.0 (no risk) -> 1.0 (highest risk), provider normalized
    risk_label: str     # Human-readable: "Low" / "Moderate" / "High" / "Unknown"
    source: str         # Provider name e.g. "FEMA", "First Street"
    raw: dict           # Raw provider output, preserved for debugging/transparency
    geocoded: NotRequired[bool]  # False only when set by flood_data.py; absent = True


class FloodDataProvider:
    """Interface every flood data provider must implement."""

    def get_risk_point(self, lat: float, lon: float) -> RiskPoint:
        raise NotImplementedError

    def get_provider_name(self) -> str:
        raise NotImplementedError
