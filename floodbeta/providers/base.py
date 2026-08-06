"""
Abstract base class for flood data providers, plus the normalized
RiskPoint output schema every provider must return.

RiskPoint is the only format scorer.py ever sees. All provider-specific
normalization logic (zone names, depth values, raster lookups) lives in the
subclass; scoring aggregation lives in scorer.py and never here.
"""

from __future__ import annotations

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


class FloodDataProvider:
    """Interface every flood data provider must implement."""

    def get_risk_point(self, lat: float, lon: float) -> RiskPoint:
        raise NotImplementedError

    def get_provider_name(self) -> str:
        raise NotImplementedError
