"""
FloodBeta score aggregation.

Aggregates a list of normalized RiskPoints (from any provider) into a single
FloodBeta score (0.0-1.0), weighted equally per facility.

Provider-agnostic by contract: this module does arithmetic on `risk_score`
floats and reads the `source` string for attribution. It contains no zone
names, no depth values, and no knowledge of how any provider produced a
score. Provider-specific normalization belongs in providers/.
"""

from __future__ import annotations

# Score band upper bounds. Bands are half-open — a score of exactly 0.2 is
# Moderate, and exactly 0.5 is High — so every value maps to one band.
LOW_MAX = 0.2
MODERATE_MAX = 0.5

LABEL_LOW = "Low"
LABEL_MODERATE = "Moderate"
LABEL_HIGH = "High"
LABEL_INSUFFICIENT = "Insufficient data"

# Enough precision to distinguish facilities, without exposing float noise
# like 0.15000000000000002 to the UI.
SCORE_PRECISION = 4


def label_for_score(score: float) -> str:
    """Map a FloodBeta score to its exposure band."""
    if score < LOW_MAX:
        return LABEL_LOW
    if score < MODERATE_MAX:
        return LABEL_MODERATE
    return LABEL_HIGH


def _is_scorable(point: dict) -> bool:
    """True when a point should contribute to the mean.

    Points carrying geocoded=False are excluded: a facility we could not
    place on the map has no measured flood risk, and letting it enter the
    mean at the provider's "Unknown" value (0.1) would quietly drag the
    score toward that number and misreport a data gap as a finding.

    `geocoded` is absent from the RiskPoint schema in providers/flood/base.py,
    so a point without it is assumed geocoded — a provider only returns a
    RiskPoint for a coordinate it was given.
    """
    if not point.get("geocoded", True):
        return False
    return isinstance(point.get("risk_score"), (int, float))


def _provider_name(risk_points: list) -> str | None:
    """Attribution from the points' `source` field.

    Reading a normalized field, not provider-specific logic. Multiple
    sources are joined so a blended run is never misattributed to one.
    """
    sources = sorted({point.get("source") for point in risk_points if point.get("source")})
    if not sources:
        return None
    return ", ".join(sources)


def calculate_floodbeta(risk_points: list) -> dict:
    """Aggregate RiskPoints into a FloodBeta score.

    Returns {"score", "label", "facility_count", "geocoded_count",
    "provider", "breakdown"}.

    `score` is the unweighted mean of risk_score across geocoded facilities,
    or None when none could be scored — in which case `label` is
    "Insufficient data" rather than a number implying a measurement that was
    never taken. `breakdown` is the input list, unmodified, so the UI can
    show exactly what went into the score, failures included.
    """
    points = list(risk_points or [])
    scorable = [point for point in points if _is_scorable(point)]

    result = {
        "score": None,
        "label": LABEL_INSUFFICIENT,
        "facility_count": len(points),
        "geocoded_count": len(scorable),
        "provider": _provider_name(points),
        "breakdown": points,
    }

    if not scorable:
        return result

    mean = sum(float(point["risk_score"]) for point in scorable) / len(scorable)
    score = round(mean, SCORE_PRECISION)

    result["score"] = score
    result["label"] = label_for_score(score)
    return result
