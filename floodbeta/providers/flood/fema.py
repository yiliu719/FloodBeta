"""
FEMA Flood Map Service Center provider implementation.

Queries the National Flood Hazard Layer (NFHL) "Flood Hazard Zones" layer for
a given lat/lon and normalizes FEMA's categorical zone labels into the
RiskPoint schema defined in base.py.

Endpoint note: the NFHL map service is hosted on hazards.fema.gov, not
msc.fema.gov — the latter answers /arcgis/rest/services/public/NFHL with
"Service not found". msc.fema.gov remains the human-facing Map Service
Center website; the REST service lives here.

Scoring is provider-local by design: nothing in this module aggregates, and
scorer.py never sees a zone name.
"""

from __future__ import annotations

import re
import time

import requests

from .base import FloodDataProvider, RiskPoint

NFHL_QUERY_URL = (
    "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
)

# Courtesy throttle between requests against a free public service.
REQUEST_DELAY_SECONDS = 0.5
REQUEST_TIMEOUT_SECONDS = 30

SCORE_HIGH = 1.0
SCORE_MODERATE = 0.3
SCORE_LOW = 0.05
SCORE_UNKNOWN = 0.1

# --- Zone normalization (see CLAUDE.md) --------------------------------------

# Special Flood Hazard Areas — the 1% annual chance (100-year) floodplain.
_SFHA_ZONES = {"A", "AE", "AO", "AH", "AR", "A99", "V", "VE"}

# Numbered legacy equivalents from older FIRMs: A1-A30 and V1-V30.
_NUMBERED_SFHA_RE = re.compile(r"^[AV](?:[1-9]|[12]\d|30)$")

# ZONE_SUBTY markers that upgrade an X zone from minimal to moderate: the
# 500-year floodplain, and areas only dry because a levee is holding.
_SHADED_X_MARKERS = ("0.2 PCT", "LEVEE")


def normalize_zone(fld_zone: str | None, zone_subty: str | None = "") -> tuple:
    """Map a FEMA zone to (risk_score, risk_label).

    Zone codes not covered by CLAUDE.md's table are handled here:

    * V / VE / V1-30 — coastal high hazard, SFHA *with* wave action. Absent
      from the table, but scoring them "Unknown" (0.1) instead of 1.0 would
      understate exactly the coastal sites this tool exists to flag.
    * AR / A99 — SFHA pending or behind planned protection. Still SFHA.
    * D — flood hazard undetermined, which is genuinely unknown, not low.
    """
    zone = (fld_zone or "").strip().upper()
    subtype = (zone_subty or "").strip().upper()

    if not zone:
        return SCORE_UNKNOWN, "Unknown"

    if zone in _SFHA_ZONES or _NUMBERED_SFHA_RE.match(zone):
        # Includes the floodway, which arrives as AE + ZONE_SUBTY "FLOODWAY"
        # and is already at the ceiling.
        return SCORE_HIGH, "High"

    if zone == "X":
        if any(marker in subtype for marker in _SHADED_X_MARKERS):
            return SCORE_MODERATE, "Moderate"  # shaded X — 500-year
        return SCORE_LOW, "Low"  # unshaded X — minimal

    if zone == "B":
        return SCORE_MODERATE, "Moderate"
    if zone == "C":
        return SCORE_LOW, "Low"

    # Zone D, "AREA NOT INCLUDED", and anything unrecognized.
    return SCORE_UNKNOWN, "Unknown"


class FEMAFloodProvider(FloodDataProvider):
    """FloodDataProvider backed by FEMA's National Flood Hazard Layer."""

    def __init__(self, session: requests.Session | None = None):
        self._session = session or requests.Session()
        self._last_request_at: float | None = None

    def get_provider_name(self) -> str:
        return "FEMA"

    def _throttle(self) -> None:
        """Sleep only for the remainder of the courtesy delay.

        Measuring elapsed time rather than sleeping unconditionally keeps a
        single lookup fast while still spacing batch requests 0.5s apart,
        wherever the batching happens to live.
        """
        if self._last_request_at is not None:
            remaining = REQUEST_DELAY_SECONDS - (
                time.monotonic() - self._last_request_at
            )
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _risk_point(self, lat, lon, score, label, raw) -> RiskPoint:
        return {
            "lat": lat,
            "lon": lon,
            "risk_score": score,
            "risk_label": label,
            "source": self.get_provider_name(),
            "raw": raw,
        }

    def get_risk_point(self, lat: float, lon: float) -> RiskPoint:
        """Look up the flood zone at a coordinate. Never raises.

        Any failure — network error, malformed response, or a point outside
        NFHL coverage — yields risk_score=0.1 / "Unknown" rather than an
        exception, so one bad coordinate cannot abort a screening run. The
        reason is preserved in `raw` for auditing.
        """
        self._throttle()

        params = {
            "geometry": f"{lon},{lat}",  # ArcGIS point order is x,y
            "geometryType": "esriGeometryPoint",
            "inSR": 4326,  # our coordinates are WGS84 lat/lon
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "FLD_ZONE,ZONE_SUBTY",
            "returnGeometry": "false",
            "f": "json",
        }

        try:
            response = self._session.get(
                NFHL_QUERY_URL, params=params, timeout=REQUEST_TIMEOUT_SECONDS
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            return self._risk_point(
                lat, lon, SCORE_UNKNOWN, "Unknown", {"error": f"Request failed: {exc}"}
            )
        except ValueError as exc:
            return self._risk_point(
                lat, lon, SCORE_UNKNOWN, "Unknown", {"error": f"Invalid JSON: {exc}"}
            )
        except Exception as exc:
            # Deliberately broad: a single lookup must never abort the run.
            return self._risk_point(
                lat, lon, SCORE_UNKNOWN, "Unknown", {"error": f"{type(exc).__name__}: {exc}"}
            )

        # ArcGIS reports service-side failures with HTTP 200 and an error
        # body, so status code alone is not a success signal.
        if not isinstance(payload, dict) or "error" in payload:
            return self._risk_point(lat, lon, SCORE_UNKNOWN, "Unknown", payload)

        features = payload.get("features") or []
        if not features:
            # No intersecting polygon: outside NFHL coverage, or unmapped.
            return self._risk_point(lat, lon, SCORE_UNKNOWN, "Unknown", payload)

        # Zone polygons can overlap at boundaries. Take the highest risk
        # present rather than whichever the service happened to list first.
        score, label = max(
            (
                normalize_zone(
                    feature.get("attributes", {}).get("FLD_ZONE"),
                    feature.get("attributes", {}).get("ZONE_SUBTY"),
                )
                for feature in features
            ),
            key=lambda pair: pair[0],
        )
        return self._risk_point(lat, lon, score, label, payload)
