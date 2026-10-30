import math

import ifcopenshell
import pytest

from ifcgeoref import crs, model_info
from conftest import HOUSE, HOUSE_GEOREF


@pytest.mark.parametrize("lat, lon, expected", [
    (47.3769, 8.5417, 2056),     # Zürich
    (52.1561, 5.3878, 28992),    # Amersfoort
    (51.5007, -0.1246, 27700),   # London
])
def test_national_grid_is_suggested_first(lat, lon, expected):
    suggestions = crs.suggest(lat, lon)
    assert suggestions[0]["code"] == expected
    assert suggestions[0]["recommended"]
    assert all(s["projected"] for s in suggestions)


def test_utm_offered_everywhere():
    codes = [s["code"] for s in crs.suggest(41.8781, -87.6298)]  # Chicago
    assert 32616 in codes


def test_search_by_code_and_name():
    assert crs.search("2056")[0]["code"] == 2056
    assert 28992 in [r["code"] for r in crs.search("amersfoort rd new")]


def test_coverage_and_projection_checks():
    lv95 = crs.details(2056)
    assert crs.covers(lv95, 47.37, 8.54) is True
    assert crs.covers(lv95, 51.5, -0.12) is False
    with pytest.raises(crs.CRSError):
        crs.require_projected(4326)
    with pytest.raises(crs.CRSError):
        crs.details(999999)


def test_true_north_convergence_is_small_and_signed():
    # East of the British grid's central meridian (2° W) true north leans west of grid north.
    angle = math.degrees(crs.true_north_in_grid(27700, 51.5, -0.13))
    assert 90.5 < angle < 92.5
    assert math.degrees(crs.true_north_in_grid(2056, 46.95, 7.44)) == pytest.approx(90, abs=0.05)


def test_model_info_reads_units_site_and_georef():
    house_file = ifcopenshell.open(str(HOUSE))
    house = model_info.extract(house_file)
    assert house["schema"] == "IFC4" and house["supported"]
    assert house["unit"]["symbol"] == "mm" and house["unit"]["metres"] == pytest.approx(0.001)
    assert house["site"]["has_location"]
    assert house["site"]["latitude"] == pytest.approx(51.500153, abs=1e-6)  # (51, 30, 0, 549316)
    assert not house["georef"]["present"]
    assert house["max_id"] == max(e.id() for e in house_file)

    georef = model_info.extract(ifcopenshell.open(str(HOUSE_GEOREF)))["georef"]
    assert georef["present"] and georef["epsg"] == 32643
