"""Basic smoke tests for the FloodBeta pipeline.

These hit the live SEC EDGAR and Nominatim APIs. They deliberately assert
nothing about specific addresses or coordinates — extraction is heuristic and
geocoding resolves to city centroids — so the tests print what was produced
for manual inspection and only check that the pipeline returns a usable shape.

Run with output visible:

    python -m pytest tests/ -s
"""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from geopy.exc import GeocoderTimedOut

from floodbeta import edgar, flood_data, geocoder, scorer
from floodbeta.providers import base, fema

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


# --- Geocoding ---------------------------------------------------------------

# Hardcoded from the TSLA 10-K extraction so this exercises the geocoder alone
# and does not fail when EDGAR or the filing changes.
TSLA_ADDRESSES = [
    "Austin, Texas",
    "Fremont, California",
    "Sparks, Nevada",
    "Buffalo, New York",
    "Lathrop, California",
]


def test_geocode_tsla_addresses():
    """Print coordinates for the TSLA facilities; all five must resolve."""
    results = geocoder.geocode_addresses(TSLA_ADDRESSES)

    print(f"\n{'=' * 70}")
    print(f"Geocoding {len(TSLA_ADDRESSES)} TSLA addresses")
    print(f"{'-' * 70}")
    for index, result in enumerate(results, start=1):
        if result["geocoded"]:
            detail = f"{result['lat']:>10.5f}, {result['lon']:>11.5f}"
        else:
            detail = f"FAILED — {result.get('error', 'unknown')}"
        print(f"{index:3d}. {result['address']:<24} {detail}")
    print(f"{'-' * 70}")
    print(f"{sum(r['geocoded'] for r in results)}/{len(results)} geocoded")

    assert len(results) == len(TSLA_ADDRESSES)
    assert [r["address"] for r in results] == TSLA_ADDRESSES

    # Nominatim blocks cloud/shared IPs outright (HTTP 403). That is an
    # environment problem, not a code defect, and it fails every address
    # identically — so report it as a skip rather than a misleading failure.
    if all("Blocked" in r.get("error", "") for r in results):
        pytest.skip(
            "Nominatim returned 403 for every address — this IP is blocked by "
            "its usage policy. Re-run from a normal network to verify."
        )

    assert all(r["geocoded"] for r in results)


def test_empty_address_is_handled_without_a_network_call():
    assert geocoder.geocode_address("")["geocoded"] is False


# --- Geocoder logic, offline (no network) ------------------------------------


class _FakeLocation:
    def __init__(self, latitude, longitude):
        self.latitude = latitude
        self.longitude = longitude


def _install_fake_geocoder(monkeypatch, behaviour):
    """Point the module at a stub geocoder and neutralise its sleep."""
    monkeypatch.setattr(
        geocoder, "_get_geolocator", lambda: SimpleNamespace(geocode=behaviour)
    )
    slept = []
    monkeypatch.setattr(geocoder.time, "sleep", slept.append)
    return slept


def test_successful_lookup_maps_latitude_and_longitude(monkeypatch):
    _install_fake_geocoder(monkeypatch, lambda q, **kw: _FakeLocation(30.27, -97.74))

    result = geocoder.geocode_address("Austin, Texas")

    assert result == {
        "address": "Austin, Texas",
        "lat": 30.27,
        "lon": -97.74,
        "geocoded": True,
    }


def test_unmatched_address_reports_no_match(monkeypatch):
    _install_fake_geocoder(monkeypatch, lambda q, **kw: None)

    result = geocoder.geocode_address("Nowhere at all")

    assert result["geocoded"] is False
    assert result["lat"] is None and result["lon"] is None
    assert result["error"] == "No match found"


def test_service_error_does_not_abort_the_batch(monkeypatch):
    def flaky(query, **kwargs):
        if query == "Sparks, Nevada":
            raise GeocoderTimedOut("timed out")
        return _FakeLocation(1.0, 2.0)

    _install_fake_geocoder(monkeypatch, flaky)

    results = geocoder.geocode_addresses(
        ["Austin, Texas", "Sparks, Nevada", "Buffalo, New York"]
    )

    assert [r["geocoded"] for r in results] == [True, False, True]
    assert "GeocoderTimedOut" in results[1]["error"]


def test_results_align_index_for_index_with_input(monkeypatch):
    known = {"Austin, Texas": (30.27, -97.74), "Sparks, Nevada": (39.53, -119.75)}
    _install_fake_geocoder(
        monkeypatch,
        lambda q, **kw: _FakeLocation(*known[q]) if q in known else None,
    )

    addresses = ["Austin, Texas", "Bogus Place", "Sparks, Nevada"]
    results = geocoder.geocode_addresses(addresses)

    assert [r["address"] for r in results] == addresses
    assert [r["geocoded"] for r in results] == [True, False, True]


def test_batch_sleeps_one_second_between_requests_only(monkeypatch):
    """Three addresses means two pauses — not three, and never zero."""
    slept = _install_fake_geocoder(monkeypatch, lambda q, **kw: _FakeLocation(1.0, 2.0))

    geocoder.geocode_addresses(["a", "b", "c"])

    assert slept == [1.0, 1.0]


# --- FEMA flood zone lookup --------------------------------------------------

# City centroids for TSLA_ADDRESSES. Hardcoded rather than geocoded so this
# test exercises FEMA alone and does not inherit Nominatim's availability.
TSLA_COORDS = [
    ("Austin, Texas", 30.2672, -97.7431),
    ("Fremont, California", 37.5485, -121.9886),
    ("Sparks, Nevada", 39.5349, -119.7527),
    ("Buffalo, New York", 42.8864, -78.8784),
    ("Lathrop, California", 37.8227, -121.2766),
]


def _assert_valid_risk_point(point, lat, lon):
    """A RiskPoint must satisfy the schema in providers/base.py."""
    assert set(point) == {"lat", "lon", "risk_score", "risk_label", "source", "raw"}
    assert point["lat"] == lat and point["lon"] == lon
    assert isinstance(point["risk_score"], float)
    assert 0.0 <= point["risk_score"] <= 1.0
    assert point["risk_label"] in base.RISK_LABELS
    assert point["source"] == "FEMA"
    assert isinstance(point["raw"], dict)


def test_fema_risk_points_for_tsla_coordinates():
    """Print the FEMA zone and risk score for each TSLA facility."""
    provider = fema.FEMAFloodProvider()

    print(f"\n{'=' * 78}")
    print(f"FEMA flood zones — {provider.get_provider_name()} NFHL")
    print(f"{'-' * 78}")
    print(f"{'facility':<22} {'zone':<8} {'subtype':<32} {'score':>5}  label")
    print(f"{'-' * 78}")

    for label, lat, lon in TSLA_COORDS:
        point = provider.get_risk_point(lat, lon)
        _assert_valid_risk_point(point, lat, lon)

        features = point["raw"].get("features") or [{}]
        attributes = features[0].get("attributes", {})
        zone = attributes.get("FLD_ZONE") or "—"
        subtype = (attributes.get("ZONE_SUBTY") or "—")[:32]
        print(
            f"{label:<22} {zone:<8} {subtype:<32} "
            f"{point['risk_score']:>5.2f}  {point['risk_label']}"
        )
    print(f"{'-' * 78}")


# --- Zone normalization, offline (no network) --------------------------------


@pytest.mark.parametrize(
    "zone,subtype,expected_score,expected_label",
    [
        ("AE", "", 1.0, "High"),
        ("A", "", 1.0, "High"),
        ("AO", "", 1.0, "High"),
        ("AH", "", 1.0, "High"),
        ("A12", "", 1.0, "High"),
        ("A30", "", 1.0, "High"),
        ("AE", "FLOODWAY", 1.0, "High"),
        ("X", "0.2 PCT ANNUAL CHANCE FLOOD HAZARD", 0.3, "Moderate"),
        ("B", "", 0.3, "Moderate"),
        ("X", "AREA OF MINIMAL FLOOD HAZARD", 0.05, "Low"),
        ("X", "", 0.05, "Low"),
        ("C", "", 0.05, "Low"),
        ("D", "", 0.1, "Unknown"),
        ("AREA NOT INCLUDED", "", 0.1, "Unknown"),
        (None, "", 0.1, "Unknown"),
        ("", "", 0.1, "Unknown"),
    ],
)
def test_zone_normalization(zone, subtype, expected_score, expected_label):
    assert fema.normalize_zone(zone, subtype) == (expected_score, expected_label)


def test_coastal_v_zones_score_as_high_risk():
    """V zones are SFHA with wave action — not in CLAUDE.md's table."""
    for zone in ["V", "VE", "V12"]:
        assert fema.normalize_zone(zone, "") == (1.0, "High")


def test_levee_protected_x_zone_is_moderate_not_minimal():
    score, label = fema.normalize_zone(
        "X", "AREA WITH REDUCED FLOOD RISK DUE TO LEVEE"
    )

    assert (score, label) == (0.3, "Moderate")


def test_zone_matching_is_case_and_whitespace_insensitive():
    assert fema.normalize_zone(" ae ", "") == (1.0, "High")


# --- FEMA error handling, offline --------------------------------------------


class _FakeResponse:
    def __init__(self, payload=None, exc=None):
        self._payload = payload
        self._exc = exc

    def raise_for_status(self):
        return None

    def json(self):
        if self._exc:
            raise self._exc
        return self._payload


def _provider_returning(payload=None, exc=None, raise_on_get=None):
    def fake_get(url, params=None, timeout=None):
        if raise_on_get:
            raise raise_on_get
        return _FakeResponse(payload, exc)

    return fema.FEMAFloodProvider(session=SimpleNamespace(get=fake_get))


def test_arcgis_error_body_with_http_200_is_treated_as_failure():
    """ArcGIS returns HTTP 200 with an error body — status is not enough."""
    provider = _provider_returning({"error": {"code": 404, "message": "not found"}})

    point = provider.get_risk_point(30.0, -97.0)

    assert point["risk_score"] == 0.1
    assert point["risk_label"] == "Unknown"
    assert "error" in point["raw"]


def test_no_intersecting_polygon_is_unknown_not_low():
    """Outside NFHL coverage means no data, which is not the same as safe."""
    provider = _provider_returning({"features": []})

    point = provider.get_risk_point(0.0, 0.0)

    assert (point["risk_score"], point["risk_label"]) == (0.1, "Unknown")


def test_network_failure_returns_unknown_without_raising():
    provider = _provider_returning(
        raise_on_get=requests.ConnectionError("connection reset")
    )

    point = provider.get_risk_point(30.0, -97.0)

    assert (point["risk_score"], point["risk_label"]) == (0.1, "Unknown")
    assert "Request failed" in point["raw"]["error"]


def test_malformed_json_returns_unknown_without_raising():
    provider = _provider_returning(exc=ValueError("Expecting value"))

    point = provider.get_risk_point(30.0, -97.0)

    assert (point["risk_score"], point["risk_label"]) == (0.1, "Unknown")
    assert "Invalid JSON" in point["raw"]["error"]


def test_overlapping_polygons_take_the_highest_risk():
    """Boundary points can match several zones; screening must be conservative."""
    provider = _provider_returning(
        {
            "features": [
                {"attributes": {"FLD_ZONE": "X", "ZONE_SUBTY": "MINIMAL"}},
                {"attributes": {"FLD_ZONE": "AE", "ZONE_SUBTY": ""}},
            ]
        }
    )

    point = provider.get_risk_point(30.0, -97.0)

    assert (point["risk_score"], point["risk_label"]) == (1.0, "High")


def test_risk_point_conforms_to_schema_offline():
    provider = _provider_returning(
        {"features": [{"attributes": {"FLD_ZONE": "AE", "ZONE_SUBTY": ""}}]}
    )

    _assert_valid_risk_point(provider.get_risk_point(30.5, -97.5), 30.5, -97.5)


def test_provider_implements_the_base_interface():
    provider = fema.FEMAFloodProvider()

    assert isinstance(provider, base.FloodDataProvider)
    assert provider.get_provider_name() == "FEMA"


# --- Scoring, offline (no network) -------------------------------------------


def _point(risk_score, geocoded=True, source="FEMA"):
    return {
        "lat": 30.0,
        "lon": -97.0,
        "risk_score": risk_score,
        "risk_label": "High",
        "source": source,
        "raw": {},
        "geocoded": geocoded,
    }


def test_score_is_the_unweighted_mean():
    result = scorer.calculate_floodbeta([_point(1.0), _point(0.05), _point(0.3)])

    assert result["score"] == pytest.approx(0.45)
    assert result["facility_count"] == 3
    assert result["geocoded_count"] == 3
    assert result["provider"] == "FEMA"


def test_failed_geocodes_are_excluded_from_the_mean():
    """A data gap must not be averaged in as if it were a measurement."""
    scored_only = scorer.calculate_floodbeta([_point(1.0), _point(1.0)])
    with_failure = scorer.calculate_floodbeta(
        [_point(1.0), _point(1.0), _point(0.1, geocoded=False)]
    )

    assert with_failure["score"] == scored_only["score"] == 1.0
    assert with_failure["facility_count"] == 3
    assert with_failure["geocoded_count"] == 2


def test_zero_geocoded_facilities_yields_no_score():
    result = scorer.calculate_floodbeta(
        [_point(0.1, geocoded=False), _point(0.1, geocoded=False)]
    )

    assert result["score"] is None
    assert result["label"] == "Insufficient data"
    assert result["facility_count"] == 2
    assert result["geocoded_count"] == 0


def test_empty_input_yields_no_score():
    result = scorer.calculate_floodbeta([])

    assert result["score"] is None
    assert result["label"] == "Insufficient data"
    assert result["facility_count"] == 0


@pytest.mark.parametrize(
    "score,expected",
    [
        (0.0, "Low"),
        (0.05, "Low"),
        (0.199, "Low"),
        (0.2, "Moderate"),  # bands are half-open
        (0.35, "Moderate"),
        (0.499, "Moderate"),
        (0.5, "High"),
        (1.0, "High"),
    ],
)
def test_score_bands(score, expected):
    assert scorer.label_for_score(score) == expected


def test_breakdown_passes_input_through_unmodified():
    points = [_point(1.0), _point(0.05, geocoded=False)]

    result = scorer.calculate_floodbeta(points)

    assert result["breakdown"] == points


def test_points_without_a_geocoded_key_are_scored():
    """RiskPoint per base.py has no `geocoded` field; absent means present."""
    bare = {
        "lat": 30.0,
        "lon": -97.0,
        "risk_score": 1.0,
        "risk_label": "High",
        "source": "FEMA",
        "raw": {},
    }

    result = scorer.calculate_floodbeta([bare])

    assert result["score"] == 1.0
    assert result["geocoded_count"] == 1


def test_multiple_providers_are_both_attributed():
    result = scorer.calculate_floodbeta(
        [_point(1.0, source="FEMA"), _point(0.0, source="First Street")]
    )

    assert result["provider"] == "FEMA, First Street"


def _code_without_docstrings(module):
    """Module source minus docstrings and comments."""
    return _code_without_docstrings_from_path(Path(module.__file__))


def _code_without_docstrings_from_path(path):
    """Source at `path` minus docstrings and comments.

    Prose legitimately names the things the code must not contain — the
    docstring says "no depth values" — so only executable code is checked.
    String literals are kept: a leaked `if zone == "AE"` must still be seen.
    """
    tree = ast.parse(Path(path).read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                node.body.pop(0)
    return ast.unparse(tree)  # also drops comments


def test_scorer_module_contains_no_provider_specific_logic():
    """Layer separation is a hard architectural rule in CLAUDE.md."""
    code = _code_without_docstrings(scorer)

    for forbidden in [
        "FLD_ZONE",
        "ZONE_SUBTY",
        "floodway",
        "NFHL",
        "fema",
        "inundation",
        "depth",
    ]:
        assert forbidden.lower() not in code.lower(), (
            f"{forbidden!r} leaked into scorer.py"
        )


# --- flood_data orchestration ------------------------------------------------


def test_flood_data_risk_points_for_tsla_addresses():
    """Print the full RiskPoint produced for each TSLA facility."""
    provider = fema.FEMAFloodProvider()
    risk_points = flood_data.get_risk_points(TSLA_ADDRESSES, provider)

    print(f"\n{'=' * 78}")
    print(f"flood_data.get_risk_points — {len(TSLA_ADDRESSES)} TSLA addresses")
    print(f"{'=' * 78}")
    for address, point in zip(TSLA_ADDRESSES, risk_points):
        print(f"\n{address}")
        print(f"  lat        : {point['lat']}")
        print(f"  lon        : {point['lon']}")
        print(f"  risk_score : {point['risk_score']}")
        print(f"  risk_label : {point['risk_label']}")
        print(f"  source     : {point['source']}")
        print(f"  geocoded   : {point['geocoded']}")
        features = point["raw"].get("features")
        if features:
            print(f"  raw        : {features[0].get('attributes')}")
        else:
            print(f"  raw        : {point['raw']}")
    print(f"\n{'=' * 78}")

    assert len(risk_points) == len(TSLA_ADDRESSES)
    for point in risk_points:
        assert set(point) == {
            "lat",
            "lon",
            "risk_score",
            "risk_label",
            "source",
            "raw",
            "geocoded",
        }
        assert point["source"] == "FEMA"
        assert point["risk_label"] in base.RISK_LABELS
        assert 0.0 <= point["risk_score"] <= 1.0


class _StubProvider(base.FloodDataProvider):
    def get_provider_name(self):
        return "STUB"

    def get_risk_point(self, lat, lon):
        return {
            "lat": lat,
            "lon": lon,
            "risk_score": 1.0,
            "risk_label": "High",
            "source": "STUB",
            "raw": {},
        }


def _patch_geocoder(monkeypatch, results):
    monkeypatch.setattr(geocoder, "geocode_addresses", lambda addresses: results)


def test_successful_geocode_gets_geocoded_true(monkeypatch):
    _patch_geocoder(
        monkeypatch,
        [{"address": "Austin, Texas", "lat": 30.2, "lon": -97.7, "geocoded": True}],
    )

    points = flood_data.get_risk_points(["Austin, Texas"], _StubProvider())

    assert points[0]["geocoded"] is True
    assert points[0]["risk_score"] == 1.0


def test_failed_geocode_becomes_an_unknown_risk_point(monkeypatch):
    _patch_geocoder(
        monkeypatch,
        [
            {
                "address": "Nowhere",
                "lat": None,
                "lon": None,
                "geocoded": False,
                "error": "No match found",
            }
        ],
    )

    point = flood_data.get_risk_points(["Nowhere"], _StubProvider())[0]

    assert point["geocoded"] is False
    assert point["risk_score"] == 0.1
    assert point["risk_label"] == "Unknown"
    assert point["source"] == "STUB"
    assert point["lat"] is None and point["lon"] is None
    assert point["raw"] == {"address": "Nowhere", "error": "No match found"}


def test_failed_geocodes_are_kept_in_order_not_dropped(monkeypatch):
    _patch_geocoder(
        monkeypatch,
        [
            {"address": "A", "lat": 1.0, "lon": 2.0, "geocoded": True},
            {"address": "B", "lat": None, "lon": None, "geocoded": False},
            {"address": "C", "lat": 3.0, "lon": 4.0, "geocoded": True},
        ],
    )

    points = flood_data.get_risk_points(["A", "B", "C"], _StubProvider())

    assert [p["geocoded"] for p in points] == [True, False, True]
    assert len(points) == 3


def test_ungeocoded_points_are_excluded_by_the_scorer(monkeypatch):
    """The contract's purpose: a data gap must not move the score."""
    _patch_geocoder(
        monkeypatch,
        [
            {"address": "A", "lat": 1.0, "lon": 2.0, "geocoded": True},
            {"address": "B", "lat": None, "lon": None, "geocoded": False},
        ],
    )

    points = flood_data.get_risk_points(["A", "B"], _StubProvider())
    result = scorer.calculate_floodbeta(points)

    assert result["score"] == 1.0  # not (1.0 + 0.1) / 2
    assert result["facility_count"] == 2
    assert result["geocoded_count"] == 1


def test_flood_data_is_the_only_module_setting_geocoded_false():
    """Sole ownership of the contract, enforced rather than documented."""
    package = Path(flood_data.__file__).parent
    offenders = []

    for path in sorted(package.rglob("*.py")):
        code = _code_without_docstrings_from_path(path)
        normalized = code.replace(" ", "").replace("'", '"')
        if '"geocoded":False' in normalized or "geocoded=False" in normalized:
            offenders.append(path.name)

    assert offenders == ["flood_data.py"], f"geocoded=False also set in {offenders}"


# --- End-to-end pipeline -----------------------------------------------------


def test_end_to_end_tsla_pipeline():
    """Ticker -> edgar -> geocoder -> FEMA -> scorer, printed for review."""
    report = edgar.get_facility_report("$TSLA")
    facilities = report["facilities"]

    # flood_data owns geocoding, provider routing, and the geocoded contract;
    # the pipeline is edgar -> flood_data -> scorer.
    provider = fema.FEMAFloodProvider()
    risk_points = flood_data.get_risk_points(report["addresses"], provider)

    if all("Blocked" in str(p["raw"].get("error", "")) for p in risk_points):
        pytest.skip(
            "Nominatim returned 403 for every address — this IP is blocked by "
            "its usage policy. Re-run from a normal network for the full pipeline."
        )

    result = scorer.calculate_floodbeta(risk_points)

    print(f"\n{'=' * 78}")
    print(f"{report['ticker']} — {report['company']}")
    print(f"{report['form']} filed {report['filing_date']}  |  {result['provider']}")
    print(f"{'=' * 78}")
    print(f"{'facility':<22} {'location':<24} {'zone':<7} {'score':>6}  label")
    print(f"{'-' * 78}")
    for facility, point in zip(facilities, risk_points):
        attributes = (point["raw"].get("features") or [{}])[0].get("attributes", {})
        zone = attributes.get("FLD_ZONE") or "—"
        score = "—" if point["risk_score"] is None else f"{point['risk_score']:.2f}"
        print(
            f"{(facility['name'] or '—'):<22} {facility['location']:<24} "
            f"{zone:<7} {score:>6}  {point['risk_label']}"
        )
    print(f"{'-' * 78}")
    print(
        f"FloodBeta: {result['score']}  ({result['label']})   "
        f"{result['geocoded_count']}/{result['facility_count']} facilities scored"
    )
    print(f"{'=' * 78}")

    assert result["facility_count"] == len(facilities)
    assert set(result) == {
        "score",
        "label",
        "facility_count",
        "geocoded_count",
        "provider",
        "breakdown",
    }
    assert result["breakdown"] == risk_points
    if result["geocoded_count"]:
        assert 0.0 <= result["score"] <= 1.0
        assert result["label"] in ("Low", "Moderate", "High")


def test_user_agent_defaults_and_reads_env(monkeypatch):
    monkeypatch.delenv("NOMINATIM_USER_AGENT", raising=False)
    assert geocoder.get_user_agent() == "FloodBeta/0.1"

    monkeypatch.setenv("NOMINATIM_USER_AGENT", "FloodBeta/0.1 (me@example.com)")
    assert geocoder.get_user_agent() == "FloodBeta/0.1 (me@example.com)"
