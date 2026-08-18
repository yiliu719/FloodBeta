"""
EPA Facility Registry Service (FRS) asset location provider.

Resolves a ticker to a company name via SEC EDGAR, searches the FRS public
REST service for facilities registered under that name, and returns
normalized Facility dicts. FRS supplies latitude/longitude for most
facilities, so flood_data.py skips geocoding entirely for them — these are
real facility coordinates, not city centroids.

Two API constraints shape this module, both discovered by probing the live
service rather than from its documentation:

1. `state_abbr` is REQUIRED, not optional. Omitting it returns
   "The state_abbr, registry_id, pgm_sys_id, zip_code, or spatial search
   parameters were not provided." Nationwide coverage therefore costs one
   request per state.

2. The service enforces **12 requests per minute** and answers HTTP 429 with
   a plain-text body once exceeded. Combined with (1), scanning all states
   takes roughly four minutes, so callers should scope the search with
   `states=` whenever they can.

Result volume is large: "TESLA" in California alone returns ~155 registered
sites. MAX_FACILITIES caps the list so a screening run does not turn into
thousands of downstream flood lookups.
"""

from __future__ import annotations

import re
import time

import requests

from . import edgar
from .base import AssetLocationProvider, Facility

FRS_ENDPOINT = (
    "https://ofmpub.epa.gov/frs_public2/frs_rest_services.get_facilities"
)

PROVIDER_NAME = "EPA FRS"
REGISTRY_URL = "https://www.epa.gov/frs"

# 12 requests/minute is the service's stated ceiling, so requests are spaced
# just over five seconds apart. CLAUDE.md's suggested 0.5s draws an immediate
# 429; this is the real courtesy interval.
RATE_LIMIT_SECONDS = 5.2
REQUEST_TIMEOUT_SECONDS = 60
RATE_LIMIT_RETRIES = 1

# A nationwide scan costs one request per entry, so ~4.5 minutes.
US_STATES = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "PR", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA",
    "WA", "WV", "WI", "WY",
)

MAX_FACILITIES = 250

# Stripped when the registered name finds nothing — EPA records use legal
# entity names that rarely carry these suffixes.
COMPANY_SUFFIX_RE = re.compile(
    r"[,\s]+(inc|incorporated|corp|corporation|co|company|llc|l\.l\.c|ltd|"
    r"limited|plc|holdings|group|lp|l\.p)\.?$",
    re.I,
)


class EpaError(Exception):
    """Raised when facilities cannot be retrieved at all."""


def strip_company_suffix(name: str) -> str:
    """Drop one trailing legal suffix. "Tesla, Inc." -> "Tesla"."""
    return COMPANY_SUFFIX_RE.sub("", (name or "").strip()).strip(" ,")


def _coordinate(value) -> float | None:
    """FRS returns coordinates as strings, and sometimes null or blank."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_address(record: dict) -> str:
    """Assemble a human-readable address from an FRS record.

    Falls back to city/state when no street address is registered, which
    still geocodes — matching the minimum the Facility schema requires.
    """
    street = (record.get("LocationAddress") or "").strip()
    city = (record.get("CityName") or "").strip().title()
    state = (record.get("StateAbbr") or "").strip().upper()
    zip_code = (record.get("ZipCode") or "").strip()[:5]

    tail = ", ".join(part for part in (city, state) if part)
    if zip_code:
        tail = f"{tail} {zip_code}".strip()

    if street:
        return f"{street.title()}, {tail}" if tail else street.title()
    return tail


class EpaLocationProvider(AssetLocationProvider):
    """Facility locations from the EPA Facility Registry Service."""

    def __init__(
        self,
        states: list | tuple | None = None,
        session: requests.Session | None = None,
        max_facilities: int = MAX_FACILITIES,
    ):
        """`states` scopes the search. None means every US state — slow.

        One request per state at ~5s apart is the price of the service's
        rate limit, so passing a short list is strongly preferred.
        """
        self.states = tuple(states) if states else US_STATES
        self.max_facilities = max_facilities
        self._session = session or requests.Session()
        self._last_request_at: float | None = None

        # Populated by get_facilities for UI transparency.
        self.matched_name: str | None = None
        self.searched_states: tuple = ()
        self.skipped_states: list = []
        self.truncated: bool = False
        # Raw match count per state, before dedupe and capping. A large
        # single-state count means the name matched broadly — EPA registers
        # third-party businesses under similar names — so the UI can warn.
        self.state_counts: dict = {}

    def get_provider_name(self) -> str:
        return PROVIDER_NAME

    def get_filing_info(self) -> dict | None:
        """None — EPA FRS is a registry, not a filing system."""
        return None

    def _throttle(self) -> None:
        if self._last_request_at is not None:
            remaining = RATE_LIMIT_SECONDS - (time.monotonic() - self._last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _query_state(self, name: str, state: str) -> list:
        """One name+state query. Returns [] on any failure, never raises."""
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            self._throttle()
            try:
                response = self._session.get(
                    FRS_ENDPOINT,
                    params={
                        "facility_name": name,
                        "state_abbr": state,
                        "output": "JSON",
                    },
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException:
                self.skipped_states.append(state)
                return []

            if response.status_code == 429:
                if attempt < RATE_LIMIT_RETRIES:
                    time.sleep(RATE_LIMIT_SECONDS)
                    continue
                self.skipped_states.append(state)
                return []

            if not response.ok:
                self.skipped_states.append(state)
                return []

            try:
                payload = response.json()
            except ValueError:
                # The service emits trailing commas in some error bodies,
                # which is not valid JSON.
                self.skipped_states.append(state)
                return []

            results = (payload or {}).get("Results") or {}
            if results.get("Error"):
                return []

            facilities = results.get("FRSFacility") or []
            # A single match may arrive as a bare object rather than a list.
            if isinstance(facilities, dict):
                facilities = [facilities]
            return facilities

        return []

    def _search(self, name: str) -> list:
        """Query every configured state for one candidate name.

        Resets skipped_states so the list describes this pass only, rather
        than accumulating across candidate names.
        """
        self.skipped_states = []
        self.state_counts = {}
        records = []
        for state in self.states:
            found = self._query_state(name, state)
            self.state_counts[state] = len(found)
            records.extend(found)
        return records

    def get_facilities(self, ticker: str) -> list[Facility]:
        """Ticker -> normalized Facility list from EPA FRS.

        Resolves the search term from SEC EDGAR's registered company name,
        then retries with legal suffixes stripped if that finds nothing.
        Returns [] when the company has no registered facilities — a normal
        outcome, not an error.
        """
        self.skipped_states = []
        self.truncated = False
        self.searched_states = self.states

        try:
            company = edgar.get_company_name(ticker)
        except edgar.EdgarError as exc:
            raise EpaError(f"Could not resolve ticker to a company name: {exc}")

        if not company:
            raise EpaError(f"SEC EDGAR returned no company name for {ticker!r}.")

        candidates = [company]
        stripped = strip_company_suffix(company)
        if stripped and stripped.lower() != company.lower():
            candidates.append(stripped)

        self.matched_name = None
        records: list = []
        for candidate in candidates:
            found = self._search(candidate)
            if found:
                self.matched_name = candidate
                records = found
                break
            if self.skipped_states:
                # An empty result is only evidence the name is wrong when
                # every state was actually queried. If states were skipped,
                # stop rather than spend a second full pass — at 12 requests
                # per minute the retry would deepen the rate limiting that
                # caused the empty result in the first place.
                break

        # One registry id can appear in several program systems.
        seen: set = set()
        facilities: list[Facility] = []
        for record in records:
            registry_id = record.get("RegistryId")
            key = registry_id or (
                record.get("FacilityName"),
                record.get("LocationAddress"),
                record.get("CityName"),
            )
            if key in seen:
                continue
            seen.add(key)

            address = build_address(record)
            if not address:
                continue

            facilities.append(
                {
                    "name": (record.get("FacilityName") or "").title() or None,
                    "address": address,
                    "lat": _coordinate(record.get("Latitude83")),
                    "lon": _coordinate(record.get("Longitude83")),
                    "source": PROVIDER_NAME,
                    "raw": {**record, "MatchedName": self.matched_name},
                }
            )

            if len(facilities) >= self.max_facilities:
                self.truncated = True
                break

        return facilities
