"""
Abstract base class for flood data providers, plus the normalized
RiskPoint output schema every provider must return.

RiskPoint is the only format scorer.py ever sees:

    RiskPoint = {
        "lat": float,           # Facility latitude
        "lon": float,           # Facility longitude
        "risk_score": float,    # 0.0 (no risk) -> 1.0 (highest risk), provider normalized
        "risk_label": str,      # Human-readable: "Low" / "Moderate" / "High" / "Unknown"
        "source": str,          # Provider name e.g. "FEMA", "First Street"
        "raw": dict,            # Raw provider output, preserved for debugging/transparency
    }

FloodDataProvider subclasses implement get_risk_point() and
get_provider_name(); all provider-specific normalization logic (zone
names, depth values, raster lookups) lives in the subclass, not here.
"""
