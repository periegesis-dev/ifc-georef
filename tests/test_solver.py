import math

import pytest

from ifcgeoref import solver


def transform(points, rotation_deg, scale, e0, n0, h0):
    r = math.radians(rotation_deg)
    c, s = math.cos(r), math.sin(r)
    return [[scale * (c * x - s * y) + e0, scale * (s * x + c * y) + n0, scale * z + h0] for x, y, z in points]


LOCAL = [[0, 0, 0], [12000, 0, 0], [12000, 8000, 3000], [0, 8000, 3000]]  # millimetres


def control(local, target):
    return [{"local": l, "target": t, "label": f"P{i + 1}"} for i, (l, t) in enumerate(zip(local, target))]


def test_recovers_rotation_and_translation_with_fixed_scale():
    target = transform(LOCAL, 23.5, 0.001, 2683000.0, 1248000.0, 408.0)
    sol = solver.solve(control(LOCAL, target), unit_scale=0.001, rotation_fallback=0.0)
    assert sol["rotation_deg"] == pytest.approx(23.5, abs=1e-9)
    assert sol["eastings"] == pytest.approx(2683000.0, abs=1e-6)
    assert sol["northings"] == pytest.approx(1248000.0, abs=1e-6)
    assert sol["orthogonal_height"] == pytest.approx(408.0, abs=1e-9)
    assert sol["scale"] == 0.001
    assert sol["rotation_source"] == "points"
    assert sol["rms_horizontal_m"] < 1e-6
    assert sol["x_axis_abscissa"] == pytest.approx(math.cos(math.radians(23.5)))


def test_single_point_uses_fallback_rotation():
    sol = solver.solve(control([[1000, 2000, 0]], [[100.0, 200.0, 5.0]]), unit_scale=0.001,
                       rotation_fallback=math.radians(10))
    assert sol["rotation_source"] == "true_north"
    assert sol["rotation_deg"] == pytest.approx(10)
    # the control point must land exactly on its target
    e, n, h = solver.apply(sol, [(1000, 2000, 0)])[0]
    assert (e, n, h) == pytest.approx((100.0, 200.0, 5.0))


def test_free_scale_flags_a_wrong_scale():
    target = transform(LOCAL, -40, 0.00105, 500.0, 600.0, 0.0)
    sol = solver.solve(control(LOCAL, target), unit_scale=0.001, rotation_fallback=0.0, scale_mode="free")
    assert sol["scale"] == pytest.approx(0.00105, rel=1e-9)
    assert sol["rotation_deg"] == pytest.approx(-40)
    assert any(w["code"] == "scale_deviation" for w in sol["warnings"])


def test_fixed_scale_reports_residuals_for_a_bad_point():
    target = transform(LOCAL, 5, 0.001, 0.0, 0.0, 0.0)
    target[2][0] += 3.0  # one map point 3 m off
    sol = solver.solve(control(LOCAL, target), unit_scale=0.001, rotation_fallback=0.0)
    worst = max(sol["residuals"], key=lambda r: r["horizontal"])
    assert worst["label"] == "P3"
    assert sol["rms_horizontal_m"] > 1.0
    assert any(w["code"] in ("low_accuracy", "medium_accuracy") for w in sol["warnings"])


def test_close_points_fall_back_to_file_north():
    sol = solver.solve(control([[0, 0, 0], [100, 0, 0]], [[0, 0, 0], [0.1, 0, 0]]), unit_scale=0.001,
                       rotation_fallback=math.radians(3))
    assert sol["rotation_source"] == "true_north"
    assert any(w["code"] == "points_close" for w in sol["warnings"])


def test_manual_overrides_and_offset():
    target = transform(LOCAL, 0, 0.001, 10.0, 20.0, 30.0)
    sol = solver.solve(control(LOCAL, target), unit_scale=0.001, rotation_fallback=0.0,
                       rotation_override=90.0, height_override=99.0, offset=(1.0, -1.0, 0.5))
    assert sol["rotation_deg"] == pytest.approx(90)
    assert sol["orthogonal_height"] == pytest.approx(99.5)
    assert sol["rotation_source"] == "manual"


def test_missing_heights_default_to_zero_with_notice():
    sol = solver.solve([{"local": [0, 0, 0], "target": [1.0, 2.0, None]}], unit_scale=1.0, rotation_fallback=0.0)
    assert sol["orthogonal_height"] == 0.0
    assert any(w["code"] == "no_height" for w in sol["warnings"])


def test_rejects_invalid_numbers():
    with pytest.raises(ValueError):
        solver.solve([{"local": [float("nan"), 0, 0], "target": [0, 0, 0]}], unit_scale=1, rotation_fallback=0)
