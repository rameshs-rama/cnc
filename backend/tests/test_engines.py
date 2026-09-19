"""Unit tests for the deterministic engines.

These are the calculations a manufacturing engineer would check by hand, so the
assertions are against analytic values rather than recorded output.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from app.engines import cutting, geom2d
from app.engines.kinematics import MachineModel, trapezoid_time
from app.engines.partmodel import (
    PartModel,
    compare_occupancy,
    grid_for,
    target_heightfield,
    target_occupancy,
)
from app.engines.stock import HeightFieldStock, SetupFrame, VoxelStock, compare_to_target


# --------------------------------------------------------------------- geometry
def test_rectangle_offset_is_exact():
    rectangle = geom2d.rect_polygon((0, 0), (50, 30))
    assert geom2d.area(rectangle) == pytest.approx(1500.0)
    inner = geom2d.offset_polygon(rectangle, -5)
    assert geom2d.area(inner) == pytest.approx(40 * 20, rel=1e-9)


def test_circle_area_converges():
    circle = geom2d.circle_polygon((0, 0), 10, 360)
    assert geom2d.area(circle) == pytest.approx(math.pi * 100, rel=1e-4)


def test_offset_collapses_when_tool_does_not_fit():
    narrow = geom2d.rect_polygon((0, 0), (8, 40))
    assert geom2d.offset_polygon(narrow, -5) == []


def test_concentric_offsets_shrink_monotonically():
    rings = geom2d.concentric_offsets(geom2d.rect_polygon((0, 0), (60, 40)), 4.0, first_offset=5.0)
    areas = [geom2d.area(r) for r in rings]
    assert areas == sorted(areas, reverse=True)


# ------------------------------------------------------------- cutting physics
@pytest.fixture()
def aluminium_rules():
    return {
        "carbide/rough": {"vc_m_min": 420.0, "fz_mm": 0.085, "ap_factor": 1.0, "ae_factor": 0.4, "kc11": 700.0, "mc": 0.23}
    }


def test_spindle_speed_follows_surface_speed_formula(aluminium_rules):
    tool = cutting.ToolGeometry(diameter=10.0, flutes=3, flute_length=30.0, max_rpm=30000.0)
    machine = cutting.MachineLimits(max_rpm=30000.0, min_rpm=50.0, max_feed_mm_min=30000.0, spindle_curve=[[0, 20], [30000, 20]])
    result = cutting.compute(tool=tool, machine=machine, material_rules=aluminium_rules, operation_class="rough")
    assert result.rpm == pytest.approx(1000 * 420 / (math.pi * 10), rel=1e-6)
    assert result.vc_m_min == pytest.approx(420.0, rel=1e-6)


def test_speed_is_clamped_by_the_machine_and_the_clamp_is_reported(aluminium_rules):
    tool = cutting.ToolGeometry(diameter=6.0, flutes=4, flute_length=20.0, max_rpm=30000.0)
    machine = cutting.MachineLimits(max_rpm=12000.0, min_rpm=50.0, max_feed_mm_min=30000.0, spindle_curve=[[0, 15], [12000, 15]])
    result = cutting.compute(tool=tool, machine=machine, material_rules=aluminium_rules, operation_class="rough")
    assert result.rpm == 12000.0
    assert any("clamped" in c for c in result.rationale["clamps"])


def test_depth_backs_off_when_the_spindle_lacks_power(aluminium_rules):
    tool = cutting.ToolGeometry(diameter=20.0, flutes=4, flute_length=45.0, max_rpm=20000.0)
    strong = cutting.MachineLimits(max_rpm=12000.0, min_rpm=50.0, max_feed_mm_min=30000.0, spindle_curve=[[0, 30], [12000, 30]])
    weak = cutting.MachineLimits(max_rpm=12000.0, min_rpm=50.0, max_feed_mm_min=30000.0, spindle_curve=[[0, 1.2], [12000, 1.2]])
    a = cutting.compute(tool=tool, machine=strong, material_rules=aluminium_rules, operation_class="rough")
    b = cutting.compute(tool=tool, machine=weak, material_rules=aluminium_rules, operation_class="rough")
    assert b.ap_mm < a.ap_mm
    assert b.power_kw <= 1.2 + 1e-6
    assert any("spindle power" in c for c in b.rationale["clamps"])


def test_chip_thinning_raises_feed_at_light_engagement():
    assert cutting.chip_thinning_factor(10.0, 5.0) == pytest.approx(1.0)
    assert cutting.chip_thinning_factor(10.0, 1.0) > 1.0
    assert cutting.chip_thinning_factor(10.0, 0.5) > cutting.chip_thinning_factor(10.0, 1.0)


def test_missing_material_rule_is_refused_not_guessed():
    tool = cutting.ToolGeometry(diameter=10.0, flutes=3, flute_length=30.0)
    machine = cutting.MachineLimits(max_rpm=12000.0, min_rpm=50.0, max_feed_mm_min=10000.0)
    with pytest.raises(KeyError, match="No cutting rule"):
        cutting.compute(tool=tool, machine=machine, material_rules={}, operation_class="rough")


def test_tapping_feed_is_locked_to_pitch():
    tool = cutting.ToolGeometry(diameter=8.0, flutes=3, flute_length=25.0, max_rpm=6000.0)
    machine = cutting.MachineLimits(max_rpm=12000.0, min_rpm=50.0, max_feed_mm_min=30000.0)
    result = cutting.tapping_parameters(tool, machine, pitch_mm=1.25)
    assert result.feed_mm_min == pytest.approx(result.rpm * 1.25)


# ------------------------------------------------------------------- kinematics
def test_trapezoid_reaches_cruise_on_a_long_move():
    # 100 mm at 100 mm/s with 1000 mm/s^2: 0.1 s up, 0.1 s down, 90 mm at cruise.
    assert trapezoid_time(100.0, 100.0, 1000.0) == pytest.approx(0.2 + 0.9, rel=1e-6)


def test_trapezoid_is_triangular_on_a_short_move():
    # Too short to reach cruise: t = 2 * sqrt(d/a).
    assert trapezoid_time(1.0, 100.0, 1000.0) == pytest.approx(2 * math.sqrt(1.0 / 1000.0), rel=1e-6)


def test_travel_limits_reject_a_position_outside_the_envelope():
    from app.engines.kinematics import within_travel

    machine = MachineModel(code="M", travels_mm={"X": [-100, 100], "Y": [-50, 50], "Z": [-200, 10]})
    assert within_travel((0, 0, 0), machine)[0] is True
    ok, reason = within_travel((0, 0, -250), machine)
    assert ok is False and "Z axis" in reason


# ------------------------------------------------------------------ stock model
def test_voxel_volume_matches_the_block():
    stock = VoxelStock([-50, -30, -20], [50, 30, 0], 1.0)
    assert stock.volume() == pytest.approx(100 * 60 * 20)


def test_flipping_the_part_presents_the_other_face():
    stock = VoxelStock([-50, -30, -20], [50, 30, 0], 1.0)
    grid = grid_for(PartModel(stock={"min": [-50, -30, -20], "max": [50, 30, 0]}), 1.0)
    top = stock.project(SetupFrame.from_spec([0, 0, 0], [0, 0, 0]), grid)
    flipped = stock.project(SetupFrame.from_spec([180, 0, 0], [0, 0, 0]), grid)
    assert top.max() == pytest.approx(0.0)
    assert flipped.max() == pytest.approx(20.0)


def test_a_machined_through_column_reads_as_empty_not_as_a_sentinel():
    stock = VoxelStock([-10, -10, -10], [10, 10, 0], 1.0)
    grid = grid_for(PartModel(stock={"min": [-10, -10, -10], "max": [10, 10, 0]}), 1.0)
    frame = SetupFrame.from_spec([0, 0, 0], [0, 0, 0])
    height = HeightFieldStock(stock.project(frame, grid), grid, stock.frame_floor(frame))
    height.cut((0, 0, -20), (0, 0, -20), 3.0)
    stock.apply_height_field(frame, grid, height.height)
    machined = stock.part_height_field(grid)
    assert machined.min() == pytest.approx(stock.frame_floor(frame))
    assert machined.min() > -100  # never a -1e9 sentinel


def test_one_pass_removes_the_analytic_volume():
    stock = VoxelStock([-50, -30, -20], [50, 30, 0], 1.0)
    grid = grid_for(PartModel(stock={"min": [-50, -30, -20], "max": [50, 30, 0]}), 1.0)
    frame = SetupFrame.from_spec([0, 0, 0], [0, 0, 0])
    height = HeightFieldStock(stock.project(frame, grid), grid, stock.frame_floor(frame))
    removed = height.cut((-40, 0, -5), (40, 0, -5), 5.0)
    expected = 80 * 10 * 5 + math.pi * 25 * 5  # swept rectangle plus the two end caps
    assert removed == pytest.approx(expected, rel=0.02)


# ------------------------------------------------------------ target modelling
def test_target_heightfield_places_every_surface(bracket_model):
    grid = grid_for(bracket_model, 1.0)
    field = target_heightfield(bracket_model, grid)
    xs, ys = grid.centers()

    def at(x, y):
        return float(field[int(np.argmin(abs(xs - x))), int(np.argmin(abs(ys - y)))])

    assert at(0, 0) == pytest.approx(-8.0)  # pocket floor
    assert at(40, 20) == pytest.approx(-20.0)  # through hole
    assert at(48, 28) == pytest.approx(0.0)  # untouched top face
    assert at(51, 0) < -19.0  # outside the outline


def test_target_occupancy_volume_matches_the_geometry(bracket_model):
    lo, hi = bracket_model.bbox()
    occupancy = target_occupancy(bracket_model, lo, hi, 0.5)
    volume = float(occupancy.sum()) * 0.5**3
    analytic = 100 * 60 * 20 - 60 * 40 * 8 - 4 * math.pi * (8.2 / 2) ** 2 * 20
    assert volume == pytest.approx(analytic, rel=0.02)


def test_conformance_reports_clean_when_machined_equals_target(bracket_model):
    grid = grid_for(bracket_model, 1.0)
    field = target_heightfield(bracket_model, grid)
    report = compare_to_target(field.copy(), field, grid, 0.05)
    assert report["conformant"] is True
    assert report["overcut_cells"] == 0


def test_conformance_flags_material_cut_below_the_part(bracket_model):
    grid = grid_for(bracket_model, 1.0)
    field = target_heightfield(bracket_model, grid)
    machined = field.copy()
    machined[10:14, 10:14] -= 0.4
    report = compare_to_target(machined, field, grid, 0.05)
    assert report["overcut_cells"] == 16
    assert report["overcut_max_mm"] == pytest.approx(0.35, abs=1e-6)


def test_occupancy_comparison_counts_both_directions():
    target = np.zeros((4, 4, 4), dtype=bool)
    target[1:3, 1:3, 1:3] = True
    machined = target.copy()
    machined[1, 1, 1] = False  # cut away part of the finished part
    machined[0, 0, 0] = True  # stock left where none should be
    report = compare_occupancy(machined, target, 1.0)
    assert report["overcut_voxels"] == 1
    assert report["remaining_voxels"] == 1
    assert report["conformant"] is False
