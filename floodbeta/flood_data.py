"""
Flood zone lookup router.

Orchestrates the middle of the pipeline: takes facility addresses, geocodes
them via geocoder.py, routes the resulting coordinates to the configured
FloodDataProvider, and returns a list of normalized RiskPoints ready for
scorer.py. Contains no provider-specific logic and no scoring aggregation.

**Sole owner of the `geocoded` field contract** described in
providers/flood/base.py. Providers never set `geocoded` — a provider only returns
a RiskPoint for a coordinate it was handed, so the value is always True at
that layer and omitting it is correct. This module sits above the providers
and is the only place that knows a geocode failed, so it is the only place
in the codebase that sets geocoded=False. Nothing else should write that
field; scorer.py only reads it, treating an absent key as True.
"""

from __future__ import annotations

from . import geocoder
from .providers.flood.base import FloodDataProvider, RiskPoint

# Placeholder score for a facility that never got a coordinate. Deliberately
# matches the "no data" convention rather than implying safety — though it is
# never actually averaged, since scorer.py excludes geocoded=False entirely.
UNGEOCODED_RISK_SCORE = 0.1
UNGEOCODED_RISK_LABEL = "Unknown"


def _ungeocoded_point(address: str, error: str, provider_name: str) -> RiskPoint:
    """A facility that never got a coordinate. Excluded from the score."""
    return {
        "lat": None,
        "lon": None,
        "risk_score": UNGEOCODED_RISK_SCORE,
        "risk_label": UNGEOCODED_RISK_LABEL,
        "source": provider_name,
        # No flood provider was ever queried, so `raw` carries the geocoding
        # failure instead — the reason this point exists.
        "raw": {"address": address, "error": error},
        "geocoded": False,
    }


def get_risk_points(
    addresses: list[str], provider: FloodDataProvider
) -> list[RiskPoint]:
    """Geocode addresses and look up flood risk for each.

    Returns one RiskPoint per input address, in input order, so results line
    up index-for-index with the addresses passed in. Facilities that failed
    to geocode are included — never silently dropped — carrying
    geocoded=False so scorer.py can exclude them from the mean while still
    counting them in facility_count.

    RiskPoint has no address field, so the caller keeps that association by
    index. For failed geocodes the address and the geocoder's reason are
    preserved in `raw` for auditing.
    """
    located = geocoder.geocode_addresses(addresses)

    risk_points: list[RiskPoint] = []
    for entry in located:
        if entry["geocoded"]:
            point = provider.get_risk_point(entry["lat"], entry["lon"])
            point["geocoded"] = True
        else:
            point = _ungeocoded_point(
                entry["address"],
                entry.get("error", "Geocoding failed"),
                provider.get_provider_name(),
            )
        risk_points.append(point)

    return risk_points


def get_risk_points_for_facilities(
    facilities: list, provider: FloodDataProvider
) -> list[RiskPoint]:
    """Facility list -> RiskPoints, geocoding only what needs it.

    A facility that already carries lat/lon goes straight to the flood
    lookup. Provider-supplied coordinates are real facility positions;
    re-geocoding would replace them with a city centroid and lose the
    precision EPA FRS gives us for free.

    Facilities without coordinates are batched through the geocoder in one
    call, so its 1 req/sec pacing still applies across them collectively.
    Returns one RiskPoint per input facility, in input order.
    """
    needs_geocoding = [
        index
        for index, facility in enumerate(facilities)
        if facility.get("lat") is None or facility.get("lon") is None
    ]

    located = dict(
        zip(
            needs_geocoding,
            geocoder.geocode_addresses(
                [facilities[index]["address"] for index in needs_geocoding]
            ),
        )
    )

    risk_points: list[RiskPoint] = []
    for index, facility in enumerate(facilities):
        entry = located.get(index)

        if entry is None:
            lat, lon = facility["lat"], facility["lon"]
        elif entry["geocoded"]:
            lat, lon = entry["lat"], entry["lon"]
        else:
            risk_points.append(
                _ungeocoded_point(
                    facility["address"],
                    entry.get("error", "Geocoding failed"),
                    provider.get_provider_name(),
                )
            )
            continue

        point = provider.get_risk_point(lat, lon)
        point["geocoded"] = True
        risk_points.append(point)

    return risk_points
