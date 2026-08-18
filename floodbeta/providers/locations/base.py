"""
Abstract base class for asset location providers, plus the normalized
Facility output schema every location provider must return.

A location provider answers one question: given a ticker, where are this
company's facilities? Provider-specific logic — filing parsing, registry
lookups, name matching — lives in the subclass. flood_data.py consumes only
the normalized Facility shape and never sees a 10-K or a registry record.

Key design note: when a provider supplies lat/lon, flood_data.py skips the
geocoder for that facility and goes straight to the flood zone lookup. This
preserves provider-supplied coordinates rather than degrading them to city
centroids.
"""

from __future__ import annotations

from typing import TypedDict


class Facility(TypedDict):
    """Normalized per-facility location record."""

    name: str | None    # Facility name if known (e.g. "Gigafactory Texas")
    address: str        # Human-readable address (city, state minimum)
    lat: float | None   # Pre-geocoded latitude if provider supplies it
    lon: float | None   # Pre-geocoded longitude if provider supplies it
    source: str         # Provider name e.g. "SEC EDGAR", "EPA FRS"
    raw: dict           # Raw provider output for transparency/debugging


class AssetLocationProvider:
    """Interface every asset location provider must implement."""

    def get_facilities(self, ticker: str) -> list[Facility]:
        raise NotImplementedError

    def get_provider_name(self) -> str:
        raise NotImplementedError

    def get_filing_info(self) -> dict | None:
        """Optional: filing provenance (company name, date, URL).

        Returns None when not applicable — EPA FRS has no filings.
        """
        raise NotImplementedError
