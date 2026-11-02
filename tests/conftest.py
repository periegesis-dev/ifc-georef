import time
from pathlib import Path

import ifcopenshell
import ifcopenshell.guid
import pytest

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
HOUSE = SAMPLES / "Ifc4_SampleHouse_0_GroundFloor.ifc"                  # IFC4, mm, site in London
HOUSE_GEOREF = SAMPLES / "Ifc4_SampleHouse_georeferenced_EPSG32643.ifc"  # IFC4 with IfcMapConversion


def make_ifc2x3(path, feet=True, location=True):
    """Minimal valid IFC2X3 file: project, units, model context and a site in Zürich."""
    f = ifcopenshell.file(schema="IFC2X3")
    org = f.createIfcOrganization(Name="Test")
    person = f.createIfcPerson(FamilyName="Tester")
    owner = f.createIfcOwnerHistory(
        f.createIfcPersonAndOrganization(person, org),
        f.createIfcApplication(org, "1.0", "Test", "test"), None, "ADDED", None, None, None, 0)
    metre = f.createIfcSIUnit(None, "LENGTHUNIT", None, "METRE")
    if feet:
        dims = f.createIfcDimensionalExponents(1, 0, 0, 0, 0, 0, 0)
        factor = f.createIfcMeasureWithUnit(f.createIfcLengthMeasure(0.3048), metre)
        length = f.createIfcConversionBasedUnit(dims, "LENGTHUNIT", "FOOT", factor)
    else:
        length = metre
    units = f.createIfcUnitAssignment([length])
    axis = f.createIfcAxis2Placement3D(f.createIfcCartesianPoint((0.0, 0.0, 0.0)), None, None)
    context = f.createIfcGeometricRepresentationContext(None, "Model", 3, 1e-5, axis,
                                                         f.createIfcDirection((0.0, 1.0)))
    project = f.createIfcProject(ifcopenshell.guid.new(), owner, "Test project", None, None, None, None,
                                 [context], units)
    site = f.createIfcSite(ifcopenshell.guid.new(), owner, "Site", None, None,
                           f.createIfcLocalPlacement(None, axis), None, None, "ELEMENT",
                           (47, 22, 36, 0) if location else None, (8, 32, 24, 0) if location else None,
                           1300.0 if location else None, None, None)
    f.createIfcRelAggregates(ifcopenshell.guid.new(), owner, None, None, project, [site])
    f.write(str(path))
    return path


@pytest.fixture
def ifc2x3_feet(tmp_path):
    return make_ifc2x3(tmp_path / "zurich_feet_ifc2x3.ifc")


@pytest.fixture
def app(tmp_path):
    from ifcgeoref import create_app

    application = create_app({
        "DATA_DIR": tmp_path / "data",
        "MAX_PARALLEL_JOBS": 1,
        "GEOMETRY_THREADS": 2,
        "ELEVATION_LOOKUP": "off",
        "API_BASE_URL": "http://backend.invalid",
        "TESTING": True,
    })
    yield application


@pytest.fixture
def client(app):
    return app.test_client()


def wait_task(client, job_id, name, timeout=240):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/api/jobs/{job_id}").get_json()
        task = status["tasks"].get(name)
        if task and task["state"] in ("done", "error"):
            return status, task
        time.sleep(0.3)
    raise AssertionError(f"task {name} did not finish in {timeout} s")


def upload(client, path, name=None):
    with open(path, "rb") as fh:
        return client.post("/api/jobs", data={"file": (fh, name or Path(path).name)},
                           content_type="multipart/form-data")
