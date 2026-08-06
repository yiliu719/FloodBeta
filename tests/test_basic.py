"""Basic smoke tests for the FloodBeta pipeline.

These hit the live SEC EDGAR API. They deliberately assert nothing about
specific addresses — extraction is heuristic and still being tuned, so the
tests print what was extracted for manual inspection and only check that the
pipeline returns a usable shape.

Run with output visible:

    python -m pytest tests/ -s
"""

import pytest

from floodbeta import edgar

# Hero demo ticker and validation ticker. Tesla's Gigafactories should surface
# CA / TX / NV / NY; Lockheed Martin discloses facilities across many states.
TICKERS = ["$TSLA", "$LMT"]


@pytest.mark.parametrize("ticker", TICKERS)
def test_extract_facility_addresses(ticker):
    """Print extracted facility locations for visual verification."""
    report = edgar.get_facility_report(ticker)

    print(f"\n{'=' * 70}")
    print(f"{report['ticker']} — {report['company']}")
    print(f"{report['form']} filed {report['filing_date']}")
    print(f"{report['url']}")
    print(f"{'-' * 70}")

    facilities = report["facilities"]
    if facilities:
        for index, facility in enumerate(facilities, start=1):
            name = facility["name"] or "—"
            print(f"{index:3d}. {facility['address']:<40} {name}")
    else:
        print("(no facilities extracted)")
    print(f"{'-' * 70}")
    print(f"{len(facilities)} location(s) extracted")

    addresses = report["addresses"]
    assert isinstance(addresses, list)
    assert all(isinstance(address, str) for address in addresses)
    # Each address must appear once: scorer.py takes an unweighted mean, so a
    # duplicate would double-weight that location's flood risk.
    assert len(addresses) == len(set(addresses))


def test_normalize_ticker_strips_dollar_sign():
    assert edgar.normalize_ticker("$tsla ") == "TSLA"


def test_unknown_ticker_raises_edgar_error():
    with pytest.raises(edgar.EdgarError):
        edgar.get_cik_for_ticker("$NOTAREALTICKER")


def test_extract_addresses_returns_empty_list_when_nothing_matches():
    assert edgar.extract_addresses("No locations are disclosed here.") == []


# --- Shared-state city lists (offline, no network) ---------------------------


def test_cities_sharing_one_trailing_state_are_all_extracted():
    """"Ocala and Orlando, Florida" is two facilities, not one."""
    text = (
        "Missiles and Fire Control - Troy, Alabama; Camden, Arkansas; "
        "Ocala and Orlando, Florida; Lexington, Kentucky; and "
        "Moorestown/Mt. Laurel, New Jersey."
    )
    addresses = edgar.extract_addresses(text)

    for expected in [
        "Troy, Alabama",
        "Camden, Arkansas",
        "Ocala, Florida",
        "Orlando, Florida",
        "Lexington, Kentucky",
        "Moorestown, New Jersey",
        "Mt. Laurel, New Jersey",
    ]:
        assert expected in addresses, f"missing {expected!r} from {addresses}"


def test_state_name_is_never_absorbed_as_a_city():
    """The walk must stop at a state, or "California, Nevada" is invented."""
    addresses = edgar.extract_addresses("Fremont, California and Sparks, Nevada.")

    assert addresses == ["Fremont, California", "Sparks, Nevada"]


def test_semicolon_terminates_a_shared_state_list():
    """"...South Carolina; and Fort Worth, Texas" must not chain backwards."""
    addresses = edgar.extract_addresses(
        "Greenville, South Carolina; and Fort Worth, Texas."
    )

    assert addresses == ["Greenville, South Carolina", "Fort Worth, Texas"]


def test_document_order_is_preserved_across_shared_state_lists():
    addresses = edgar.extract_addresses("Ocala and Orlando, Florida.")

    assert addresses == ["Ocala, Florida", "Orlando, Florida"]
