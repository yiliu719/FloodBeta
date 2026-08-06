"""
Address -> lat/lon geocoding using geopy's Nominatim geocoder.

Converts facility addresses extracted from 10-K filings into coordinates for
flood zone lookup, and respects Nominatim's usage policy of at most one
request per second.

Precision note: 10-K property disclosures give city/state ("Sparks, Nevada"),
not street addresses, so this resolves to **city centroids only**. A centroid
can fall in a different FEMA flood zone than the facility itself — the gap is
widest for coastal sites. Street-level precision is deferred post-MVP; any UI
presenting these scores should state the limitation rather than implying
facility-level accuracy.
"""

from __future__ import annotations

import os
import time

from geopy.exc import GeocoderInsufficientPrivileges, GeopyError
from geopy.geocoders import Nominatim

# Nominatim's usage policy caps clients at 1 request/second. Exceeding it gets
# the client blocked, so this is a hard floor, not a tuning knob.
RATE_LIMIT_SECONDS = 1.0

# Nominatim rejects requests without a descriptive, contactable user agent.
DEFAULT_USER_AGENT = "FloodBeta/0.1"
REQUEST_TIMEOUT_SECONDS = 10

_geolocator: Nominatim | None = None


def get_user_agent() -> str:
    return os.environ.get("NOMINATIM_USER_AGENT", DEFAULT_USER_AGENT)


def _get_geolocator() -> Nominatim:
    """Return a lazily built, reused Nominatim client."""
    global _geolocator
    if _geolocator is None:
        _geolocator = Nominatim(
            user_agent=get_user_agent(), timeout=REQUEST_TIMEOUT_SECONDS
        )
    return _geolocator


def _result(address: str, lat=None, lon=None, error: str | None = None) -> dict:
    """Build a result. `error` is added only on failure, never on success."""
    result = {
        "address": address,
        "lat": lat,
        "lon": lon,
        "geocoded": lat is not None and lon is not None,
    }
    if error:
        result["error"] = error
    return result


def geocode_address(address: str) -> dict:
    """Geocode a single address to {"address", "lat", "lon", "geocoded"}.

    Never raises. A lookup that fails — no match, timeout, service error —
    returns geocoded=False with lat/lon of None, so one bad address cannot
    abort a whole company's screening run. Failures also carry an "error"
    key: "no facility at this address" and "the geocoding service is
    refusing us" both yield geocoded=False but need different UI responses,
    and without the reason they are indistinguishable.

    Does not sleep. Use geocode_addresses() for batches; calling this in a
    tight loop would breach Nominatim's rate limit.
    """
    if not address or not address.strip():
        return _result(address, error="Empty address")

    try:
        location = _get_geolocator().geocode(address.strip())
    except GeocoderInsufficientPrivileges as exc:
        # HTTP 403. Nominatim blocks cloud/shared IPs and unhelpful user
        # agents outright; every address in the batch will fail identically.
        return _result(address, error=f"Blocked by geocoding service: {exc}")
    except GeopyError as exc:
        # Timeouts, service errors, quota rejections.
        return _result(address, error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        # Deliberately broad: a single unexpected failure must not abort the
        # batch. The reason is preserved rather than swallowed.
        return _result(address, error=f"{type(exc).__name__}: {exc}")

    if location is None:
        return _result(address, error="No match found")
    return _result(address, location.latitude, location.longitude)


def geocode_addresses(addresses: list[str]) -> list[dict]:
    """Geocode addresses in order, pausing 1s between requests.

    Returns one dict per input, in input order, including failures — so the
    result always lines up index-for-index with the addresses passed in.
    """
    results: list[dict] = []
    for index, address in enumerate(addresses):
        if index:
            time.sleep(RATE_LIMIT_SECONDS)
        results.append(geocode_address(address))
    return results
