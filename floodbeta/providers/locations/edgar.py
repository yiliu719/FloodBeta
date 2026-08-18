"""
SEC EDGAR integration: ticker -> latest 10-K -> facility/property addresses.

Resolves a ticker to a CIK, finds the company's most recent 10-K, downloads
the primary filing document, isolates the Item 2 "Properties" section, and
extracts facility location strings for downstream geocoding.

Note on API choice: EDGAR's full-text search endpoint (efts.sec.gov) searches
document *contents* and cannot map a ticker to its latest filing. The
company_tickers + data.sec.gov/submissions JSON APIs do exactly that, are
equally free and key-less, and are the documented path for this lookup. Both
are used here instead of full-text search.

Extraction is heuristic. 10-K property tables rarely give street addresses;
they usually give "City, State", which is sufficient granularity for
geocoding. Callers should treat the output as candidate locations to be
verified, not ground truth.

Implements AssetLocationProvider via EdgarLocationProvider at the bottom of
this module. The parsing functions above it are the implementation detail and
remain importable for testing.
"""

from __future__ import annotations

import html
import os
import re
import time

import requests

from .base import AssetLocationProvider, Facility

# --- SEC endpoints -----------------------------------------------------------

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FILING_DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"

# SEC requires a descriptive User-Agent with contact info on every request and
# throttles at ~10 req/sec. Set SEC_USER_AGENT in your environment; the default
# is deliberately generic so no personal email is committed to a public repo.
DEFAULT_USER_AGENT = "FloodBeta/0.1 (configure SEC_USER_AGENT with your email)"
REQUEST_DELAY_SECONDS = 0.2
REQUEST_TIMEOUT_SECONDS = 30

MAX_ADDRESSES = 250


class EdgarError(Exception):
    """Raised when a filing cannot be located or retrieved.

    Callers (app.py) should catch this and surface the message to the user
    rather than letting it propagate as a stack trace.
    """


# --- HTTP --------------------------------------------------------------------


def _headers() -> dict:
    return {
        "User-Agent": os.environ.get("SEC_USER_AGENT", DEFAULT_USER_AGENT),
        "Accept-Encoding": "gzip, deflate",
    }


def _get(url: str) -> requests.Response:
    """GET with SEC-compliant headers and a courtesy delay."""
    time.sleep(REQUEST_DELAY_SECONDS)
    try:
        response = requests.get(
            url, headers=_headers(), timeout=REQUEST_TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        raise EdgarError(f"Network error contacting SEC EDGAR: {exc}") from exc

    if response.status_code == 403:
        raise EdgarError(
            "SEC EDGAR returned 403. Set SEC_USER_AGENT to a descriptive "
            "string including a contact email."
        )
    if not response.ok:
        raise EdgarError(f"SEC EDGAR returned HTTP {response.status_code} for {url}")
    return response


# --- Ticker -> CIK -> filing -------------------------------------------------


def normalize_ticker(ticker: str) -> str:
    """Strip '$' prefixes and whitespace, uppercase. '$tsla ' -> 'TSLA'."""
    return ticker.strip().lstrip("$").strip().upper()


def get_cik_for_ticker(ticker: str) -> str:
    """Return the 10-digit zero-padded CIK for a ticker.

    Raises EdgarError if the ticker is not in SEC's registry.
    """
    symbol = normalize_ticker(ticker)
    if not symbol:
        raise EdgarError("No ticker provided.")

    entries = _get(TICKER_MAP_URL).json()
    for entry in entries.values():
        if entry.get("ticker", "").upper() == symbol:
            return str(entry["cik_str"]).zfill(10)

    raise EdgarError(f"Ticker '{symbol}' not found in SEC EDGAR's registry.")


def get_company_name(ticker: str) -> str:
    """Return SEC's registered company name for a ticker.

    Used by the EPA provider as its facility-name search term. Reads the
    submissions endpoint directly rather than get_latest_10k, so it works
    for filers with no 10-K on record.
    """
    cik = get_cik_for_ticker(ticker)
    data = _get(SUBMISSIONS_URL.format(cik=cik)).json()
    return data.get("name", "")


def get_latest_10k(cik: str) -> dict:
    """Return metadata for the company's most recent annual report.

    Amendments ("10-K/A") are skipped in favour of the original filing: an
    amendment usually restates only Part III (executive compensation) and
    contains no Item 2 Properties section, so it would yield zero locations.
    Amendments are used only when no original 10-K exists.

    Looks only at the `recent` filings block, which covers roughly the last
    1,000 filings — always sufficient to find the newest annual report.
    Returns {"url", "form", "filing_date", "accession", "company"}.
    """
    data = _get(SUBMISSIONS_URL.format(cik=cik)).json()
    recent = data.get("filings", {}).get("recent", {})

    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    documents = recent.get("primaryDocument", [])
    dates = recent.get("filingDate", [])

    def _build(index: int) -> dict:
        return {
            "company": data.get("name", ""),
            "form": forms[index],
            "filing_date": dates[index],
            "accession": accessions[index],
            "url": FILING_DOC_URL.format(
                cik=str(int(cik)),
                accession=accessions[index].replace("-", ""),
                document=documents[index],
            ),
        }

    # Filings are newest-first. Match "10-K" and variants like "10-K405",
    # but never the quarterly "10-Q".
    amendment_index = None
    for index, form in enumerate(forms):
        if not form.startswith("10-K") or not documents[index]:
            continue
        if form.endswith("/A"):
            if amendment_index is None:
                amendment_index = index
            continue
        return _build(index)

    if amendment_index is not None:
        return _build(amendment_index)

    raise EdgarError(f"No 10-K filing found for CIK {cik}.")


# --- HTML -> text ------------------------------------------------------------

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_BLOCK_TAG_RE = re.compile(
    r"</?(?:p|div|br|tr|table|li|ul|ol|h[1-6]|section|hr)\b[^>]*>", re.I
)
_CELL_TAG_RE = re.compile(r"</?(?:td|th)\b[^>]*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_WS_RE = re.compile(r"[ \t ]+")
_BLANK_LINES_RE = re.compile(r"\n\s*\n+")

CELL_SEPARATOR = "|"


def html_to_text(markup: str) -> str:
    """Flatten filing HTML to plain text, preserving table structure.

    Rows and paragraphs become newlines; table cells are joined with '|'.
    Keeping cell boundaries visible is what stops a facility name in one
    column ("Gigafactory Texas") from being absorbed into the city in the
    next column ("Austin, Texas") — the two stay separately addressable.
    """
    text = _SCRIPT_STYLE_RE.sub(" ", markup)
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = _CELL_TAG_RE.sub(f" {CELL_SEPARATOR} ", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = text.replace("’", "'").replace("\xa0", " ")
    text = _INLINE_WS_RE.sub(" ", text)
    text = re.sub(r" +,", ",", text)
    text = _normalize_cells(text)
    text = _BLANK_LINES_RE.sub("\n", text)
    return text.strip()


def _normalize_cells(text: str) -> str:
    """Collapse repeated cell separators, one line at a time.

    Deliberately line-wise: a single regex over the whole document with \\s
    around the separator would match newlines too and splice every table row
    onto one line, erasing the row boundaries this all depends on.
    """
    lines = []
    for line in text.split("\n"):
        line = re.sub(rf"(?:[ \t]*\{CELL_SEPARATOR}[ \t]*)+", f" {CELL_SEPARATOR} ", line)
        lines.append(line.strip().strip(CELL_SEPARATOR).strip())
    return "\n".join(lines)


# --- Item 2 Properties section ----------------------------------------------

_ITEM_2_RE = re.compile(r"item\s*2\s*[.:–—-]?\s*propert(?:y|ies)", re.I)
_ITEM_3_RE = re.compile(r"item\s*3\s*[.:–—-]?\s*legal\s+proceedings", re.I)

# If Item 3 is missing, read this far past the Item 2 heading instead.
_FALLBACK_SECTION_CHARS = 40_000


def extract_properties_section(text: str) -> str:
    """Return the Item 2 Properties section, or '' if it cannot be located.

    A 10-K mentions "Item 2. Properties" at least twice: once in the table of
    contents and once as the real heading. The TOC occurrence is followed
    almost immediately by "Item 3. Legal Proceedings", so the correct section
    is the *longest* Item 2 -> Item 3 span, not the first.
    """
    starts = [match.end() for match in _ITEM_2_RE.finditer(text)]
    if not starts:
        return ""

    ends = [match.start() for match in _ITEM_3_RE.finditer(text)]
    best = ""
    for start in starts:
        following = [end for end in ends if end > start]
        stop = following[0] if following else start + _FALLBACK_SECTION_CHARS
        candidate = text[start:stop]
        if len(candidate) > len(best):
            best = candidate
    return best.strip()


# --- Address extraction ------------------------------------------------------

_STATE_NAMES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "District of Columbia", "Florida", "Georgia",
    "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky",
    "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York", "North Carolina",
    "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania",
    "Puerto Rico", "Rhode Island", "South Carolina", "South Dakota",
    "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming",
]

_STATE_ABBREVS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DC", "DE", "FL", "GA", "HI",
    "IA", "ID", "IL", "IN", "KS", "KY", "LA", "MA", "MD", "ME", "MI", "MN",
    "MO", "MS", "MT", "NC", "ND", "NE", "NH", "NJ", "NM", "NV", "NY", "OH",
    "OK", "OR", "PA", "PR", "RI", "SC", "SD", "TN", "TX", "UT", "VA", "VT",
    "WA", "WI", "WV", "WY",
}

# These double as ordinary English words in filing prose ("...facilities in
# OR around..."), so they only count as a state when a ZIP code follows.
_AMBIGUOUS_ABBREVS = {"IN", "OR", "ME", "OK", "HI", "DE", "AL", "LA", "MS", "PA"}

# Capitalized token sequences that are headings or boilerplate, not cities.
_CITY_STOPWORDS = {
    "item", "items", "note", "notes", "company", "corporation", "inc",
    "state", "states", "united", "location", "locations", "property",
    "properties", "facility", "facilities", "segment", "segments", "total",
    "table", "contents", "page", "see", "our", "the", "and", "for", "part",
    "annual", "report", "form", "common", "stock", "incorporated",
    "headquarters", "principal", "executive", "offices", "office", "owned",
    "leased", "approximately", "square", "feet", "including", "such",
}

_CITY = (
    r"[A-Z][A-Za-z.'-]+"
    r"(?:[ -](?:[A-Z][A-Za-z.'-]+|de|del|la|las|los|of|upon))*"
)

_STATE_NAME_SET = set(_STATE_NAMES)

# Joins cities that share one trailing state: "Ocala and Orlando, Florida",
# "Moorestown/Mt. Laurel, New Jersey", "Ocala, Orlando, and Tampa, Florida".
# Anchored to the end so it only matches immediately before a known city.
# A semicolon or period is deliberately absent: those separate independent
# city/state pairs and must terminate the walk.
_CITY_CHAIN_SEPARATOR_RE = re.compile(r"(?:\s+(?:and|&)\s+|\s*/\s*|,\s+(?:and\s+)?)$")
_CITY_SUFFIX_RE = re.compile(rf"({_CITY})$")

# A comma is required. Making it optional matched facility names against the
# state that follows them ("Gigafactory Texas" -> city "Gigafactory"), which
# is why cell boundaries are preserved in html_to_text instead.
_CITY_STATE_NAME_RE = re.compile(
    rf"\b({_CITY}),\s+({'|'.join(sorted(_STATE_NAMES, key=len, reverse=True))})\b"
)

# Two-letter abbreviations: comma required, ZIP optional.
_CITY_STATE_ABBREV_RE = re.compile(
    rf"\b({_CITY}),\s+([A-Z]{{2}})\b(\s+\d{{5}}(?:-\d{{4}})?)?"
)

_STREET_SUFFIXES = (
    r"Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|"
    r"Parkway|Pkwy|Highway|Hwy|Court|Ct|Circle|Cir|Place|Pl|Terrace|Trail|"
    r"Plaza|Turnpike|Route"
)
_STREET_RE = re.compile(
    rf"\b\d{{1,6}}\s+(?:[A-Z][A-Za-z.'-]+\s+){{0,4}}(?:{_STREET_SUFFIXES})\b\.?,?\s*$",
    re.I,
)


def _looks_like_city(city: str) -> bool:
    """Reject headings and boilerplate that match the capitalized-word shape."""
    words = re.split(r"[ -]", city)
    if len(words) > 4 or len(city) < 3:
        return False
    return not any(word.strip(".'").lower() in _CITY_STOPWORDS for word in words)


def _cell_bounds(text: str, position: int) -> tuple[int, int]:
    """Return the (start, end) offsets of the table cell containing position."""
    start = max(
        text.rfind("\n", 0, position) + 1,
        text.rfind(CELL_SEPARATOR, 0, position) + 1,
    )
    candidates = [
        offset
        for offset in (text.find("\n", position), text.find(CELL_SEPARATOR, position))
        if offset != -1
    ]
    return start, min(candidates) if candidates else len(text)


def _street_prefix(text: str, city_start: int, cell_start: int) -> str:
    """Return a street address immediately preceding a city in the same cell."""
    match = _STREET_RE.search(text[cell_start:city_start])
    return match.group(0).strip().rstrip(",") if match else ""


def _shared_state_cities(
    text: str, cell_start: int, city_start: int
) -> list[tuple[int, str]]:
    """Find cities that share the state of the city at `city_start`.

    10-K prose groups sites by state — "Ocala and Orlando, Florida" is two
    facilities, but only the last carries the state, so a plain city/state
    regex silently drops every city but the final one. Walks backwards from
    the matched city across `and` / `/` / `,` separators, collecting cities
    that share the trailing state.

    Stops at a semicolon, period, or cell edge, which separate independent
    city/state pairs. Critically, it also stops on a state name: in
    "Fremont, California and Sparks, Nevada" the token before `and` is
    California, and treating it as a city would invent "California, Nevada".

    Returns [(position, city)] in document order.
    """
    found: list[tuple[int, str]] = []
    cursor = city_start

    while True:
        prefix = text[cell_start:cursor]
        separator = _CITY_CHAIN_SEPARATOR_RE.search(prefix)
        if not separator:
            break

        candidate_match = _CITY_SUFFIX_RE.search(prefix[: separator.start()])
        if not candidate_match:
            break

        candidate = candidate_match.group(1)
        if candidate in _STATE_NAME_SET or not _looks_like_city(candidate):
            break

        found.append((cell_start + candidate_match.start(1), candidate))
        cursor = cell_start + candidate_match.start(1)

    return sorted(found)


def _facility_name(text: str, cell_start: int) -> str:
    """Return the label column for the row containing this cell, if any.

    Property tables commonly lead with a name column ("Gigafactory Nevada")
    before the location column. Anything preceding the location cell on the
    same row is taken as the name, provided it is not itself a location and
    does not look like a heading.
    """
    row_start = text.rfind("\n", 0, cell_start) + 1
    if row_start >= cell_start:
        return ""

    cells = [
        cell.strip()
        for cell in text[row_start:cell_start].split(CELL_SEPARATOR)
        if cell.strip()
    ]
    if not cells:
        return ""

    name = cells[0]
    if len(name) > 60 or _CITY_STATE_NAME_RE.search(name):
        return ""
    if not re.search(r"[A-Za-z]", name) or name.lower() in _CITY_STOPWORDS:
        return ""
    return name


def extract_facilities(section_text: str) -> list[dict]:
    """Extract candidate facilities from a Properties section.

    Returns [{"name", "location", "address"}] where `location` is
    "City, State", `name` is the facility label from the table's first
    column (or "" when the filing gives none), and `address` is the
    geocodable string — street prefix plus location where available.

    Deduplicated on `address` so a site mentioned both in prose and in the
    property table counts once; scorer.py takes an unweighted mean, so a
    duplicate would silently double-weight that location. The first
    non-empty name wins. Returns [] when nothing matches.
    """
    found: list[tuple[int, str]] = []

    def _add_with_shared_state(position: int, city: str, state: str) -> None:
        """Record a city/state pair plus any cities sharing its state."""
        cell_start, _ = _cell_bounds(section_text, position)
        for shared_position, shared_city in _shared_state_cities(
            section_text, cell_start, position
        ):
            found.append((shared_position, f"{shared_city}, {state}"))
        found.append((position, f"{city}, {state}"))

    for match in _CITY_STATE_NAME_RE.finditer(section_text):
        city, state = match.group(1), match.group(2)
        if _looks_like_city(city):
            _add_with_shared_state(match.start(), city, state)

    for match in _CITY_STATE_ABBREV_RE.finditer(section_text):
        city, abbrev, zip_code = match.group(1), match.group(2), match.group(3)
        if abbrev not in _STATE_ABBREVS or not _looks_like_city(city):
            continue
        if abbrev in _AMBIGUOUS_ABBREVS and not zip_code:
            continue
        if zip_code:
            # A ZIP belongs to this specific city, so it cannot be shared
            # with others in the list.
            found.append((match.start(), f"{city}, {abbrev} {zip_code.strip()}"))
        else:
            _add_with_shared_state(match.start(), city, abbrev)

    found.sort(key=lambda pair: pair[0])

    facilities: list[dict] = []
    by_address: dict[str, dict] = {}
    for position, location in found:
        cell_start, _ = _cell_bounds(section_text, position)
        street = _street_prefix(section_text, position, cell_start)
        address = f"{street}, {location}" if street else location
        name = _facility_name(section_text, cell_start)

        key = address.lower()
        if key in by_address:
            # Same site seen twice; keep the first name we managed to find.
            if name and not by_address[key]["name"]:
                by_address[key]["name"] = name
            continue

        facility = {"name": name, "location": location, "address": address}
        by_address[key] = facility
        facilities.append(facility)
        if len(facilities) >= MAX_ADDRESSES:
            break
    return facilities


def extract_addresses(section_text: str) -> list[str]:
    """Geocodable address strings only — see extract_facilities for detail."""
    return [facility["address"] for facility in extract_facilities(section_text)]


# --- Public entry point ------------------------------------------------------


def get_facility_addresses(ticker: str) -> list[str]:
    """Ticker -> list of facility location strings from the latest 10-K.

    Returns [] when the filing is retrieved but no locations can be parsed,
    which is a normal outcome for companies with thin property disclosures.
    Raises EdgarError when the ticker or filing cannot be found at all.
    """
    return get_facility_report(ticker)["addresses"]


def get_facility_report(ticker: str) -> dict:
    """Same as get_facility_addresses, plus names and filing provenance.

    Returns {"ticker", "company", "form", "filing_date", "url",
    "facilities", "addresses"} so app.py can label each mapped point with
    its facility name and cite the filing the locations came from — required
    by the project's transparency principle.
    """
    cik = get_cik_for_ticker(ticker)
    filing = get_latest_10k(cik)

    text = html_to_text(_get(filing["url"]).text)
    section = extract_properties_section(text)
    facilities = extract_facilities(section) if section else []

    return {
        "ticker": normalize_ticker(ticker),
        "company": filing["company"],
        "form": filing["form"],
        "filing_date": filing["filing_date"],
        "url": filing["url"],
        "facilities": facilities,
        "addresses": [facility["address"] for facility in facilities],
    }


# --- AssetLocationProvider implementation ------------------------------------

PROVIDER_NAME = "SEC EDGAR"


class EdgarLocationProvider(AssetLocationProvider):
    """Facility locations from a company's latest 10-K Item 2 Properties."""

    def __init__(self):
        self._filing_info: dict | None = None

    def get_provider_name(self) -> str:
        return PROVIDER_NAME

    def get_facilities(self, ticker: str) -> list[Facility]:
        """Ticker -> normalized Facility list from the latest 10-K.

        lat/lon are always None: 10-K property disclosures give city and
        state, never coordinates, so flood_data.py must geocode these.

        Filing provenance is captured as a side effect and read back via
        get_filing_info(), because the interface gives that method no ticker
        to look up. Raises EdgarError if the ticker or filing is not found.
        """
        report = get_facility_report(ticker)

        self._filing_info = {
            "ticker": report["ticker"],
            "company": report["company"],
            "form": report["form"],
            "filing_date": report["filing_date"],
            "url": report["url"],
        }

        return [
            {
                # extract_facilities uses "" for an unnamed facility; the
                # Facility schema wants None.
                "name": facility["name"] or None,
                "address": facility["address"],
                "lat": None,
                "lon": None,
                "source": PROVIDER_NAME,
                "raw": dict(facility),
            }
            for facility in report["facilities"]
        ]

    def get_filing_info(self) -> dict | None:
        """Provenance for the most recent get_facilities() call, or None."""
        return self._filing_info
