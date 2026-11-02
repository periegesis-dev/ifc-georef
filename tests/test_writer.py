import ifcopenshell
import pytest

from ifcgeoref import crs, model_info, step, writer
from conftest import HOUSE, HOUSE_GEOREF

SOLUTION = {"eastings": 2683012.345, "northings": 1248123.456, "orthogonal_height": 408.25,
            "x_axis_abscissa": 0.8, "x_axis_ordinate": 0.6, "scale": 0.001}


def read_meta(path):
    return model_info.extract(ifcopenshell.open(str(path)))


def test_step_encoding():
    assert step.real(1.0) == "1.0"
    assert step.real(1e-05) == "1.E-05"
    assert step.real(-2.5e20) == "-2.5E+20"
    assert step.string("O'Brien \\ Zürich") == "'O''Brien \\\\ Z\\X2\\00FC\\X0\\rich'"
    with pytest.raises(ValueError):
        step.real(float("inf"))


def test_record_span_ignores_semicolons_in_strings():
    data = b"DATA;\n#1=IFCLABEL('a;b');\n#12=IFCWALL('x',$);\n#123=IFCTEXT('it''s;');\nENDSEC;\nEND-ISO-10303-21;\n"
    s, e = step.record_span(data, 1)
    assert data[s:e] == b"#1=IFCLABEL('a;b');"
    s, e = step.record_span(data, 123)
    assert data[s:e] == b"#123=IFCTEXT('it''s;');"
    assert step.record_span(data, 99) is None
    assert data[step.data_section_end(data):].startswith(b"ENDSEC;")


def test_ifc4_adds_map_conversion_and_keeps_original_bytes(tmp_path):
    meta = read_meta(HOUSE)
    assert not meta["georef"]["present"]
    out = tmp_path / "out.ifc"
    summary = writer.write(HOUSE, out, meta, SOLUTION, crs.details(2056))
    assert summary["added"] == 2 and summary["replaced"] == 0

    original, written = HOUSE.read_bytes(), out.read_bytes()
    insert_at = step.data_section_end(original)
    assert written[:insert_at] == original[:insert_at]
    assert written.endswith(original[insert_at:])

    info = model_info.georef_info(ifcopenshell.open(str(out)))
    assert info["present"] and info["epsg"] == 2056
    for key, value in SOLUTION.items():
        assert info[key] == pytest.approx(value)


def test_ifc4_replaces_existing_georeferencing(tmp_path):
    meta = read_meta(HOUSE_GEOREF)
    assert meta["georef"]["present"]
    out = tmp_path / "out.ifc"
    summary = writer.write(HOUSE_GEOREF, out, meta, SOLUTION, crs.details(2056))
    assert summary["added"] == 0 and summary["replaced"] == 2
    f = ifcopenshell.open(str(out))
    assert len(f.by_type("IfcMapConversion")) == 1
    info = model_info.georef_info(f)
    assert info["epsg"] == 2056 and info["eastings"] == pytest.approx(SOLUTION["eastings"])
    assert sum(1 for _ in f) == sum(1 for _ in ifcopenshell.open(str(HOUSE_GEOREF)))  # nothing added


def test_ifc2x3_writes_and_then_updates_property_sets(tmp_path, ifc2x3_feet):
    meta = read_meta(ifc2x3_feet)
    assert meta["unit"]["name"] == "FOOT" and meta["unit"]["metres"] == pytest.approx(0.3048)
    assert meta["site"]["elevation_m"] == pytest.approx(1300 * 0.3048)
    first = tmp_path / "first.ifc"
    writer.write(ifc2x3_feet, first, meta, {**SOLUTION, "scale": 0.3048}, crs.details(2056))
    f = ifcopenshell.open(str(first))
    info = model_info.georef_info(f)
    assert info["present"] and info["method"] == "ePSet_MapConversion"
    assert info["epsg"] == 2056 and info["scale"] == pytest.approx(0.3048)

    second = tmp_path / "second.ifc"
    meta2 = model_info.extract(f)
    writer.write(first, second, meta2, {**SOLUTION, "eastings": 2600000.0, "scale": 0.3048}, crs.details(2056))
    f2 = ifcopenshell.open(str(second))
    assert model_info.georef_info(f2)["eastings"] == pytest.approx(2600000.0)
    psets = [p for p in f2.by_type("IfcPropertySet") if p.Name.lower() == "epset_mapconversion"]
    assert len(psets) == 1
