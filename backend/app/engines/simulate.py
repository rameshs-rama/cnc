"""Stock removal and full-machine verification (FR-SIM-001 to FR-SIM-003).

The simulator replays every move against the voxel stock, the fixture and the
machine envelope. Findings are emitted as severity-tagged events that name the
responsible path segment, the entities involved and the time at which the
condition occurs, so a collision is actionable rather than a bare failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.core.enums import Severity
from app.engines import collision
from app.engines.collision import Solid, ToolSegment
from app.engines.kinematics import (
    MachineModel,
    TimeBreakdown,
    drill_cycle_time,
    linear_move_time,
    rapid_move_time,
    spindle_change_time,
    within_travel,
)
from app.engines.partmodel import (
    Grid,
    PartModel,
    compare_occupancy,
    grid_for,
    target_heightfield,
    target_occupancy,
    worst_occupancy_location,
)
from app.engines.stock import HeightFieldStock, SetupFrame, VoxelStock, compare_to_target, locate_worst

ENGINE_VERSION = "1.0.0"

#: Maximum distance between collision samples. Half the smallest clamp feature
#: we expect to model, so a thin jaw cannot be stepped over.
SAMPLE_STEP_MM = 1.0


@dataclass(slots=True)
class ToolSpec:
    id: str
    code: str
    number: int
    diameter: float
    flute_length: float
    corner_radius: float = 0.0
    gauge_length: float = 90.0
    max_rpm: float = 12000.0
    segments: list[ToolSegment] = field(default_factory=list)
    has_collision_model: bool = True

    @property
    def radius(self) -> float:
        return self.diameter / 2.0


@dataclass(slots=True)
class OperationSim:
    operation_id: str
    label: str
    tool: ToolSpec
    moves: list[dict[str, Any]]
    rpm: float
    feed_mm_min: float
    plunge_feed_mm_min: float
    coolant: str = "flood"
    suppressed: bool = False


@dataclass(slots=True)
class SetupSim:
    setup_id: str
    sequence: int
    name: str
    orientation_deg: list[float]
    origin_mm: list[float]
    index_position: dict[str, Any]
    clearance_plane_mm: float
    fixture_solids: list[Solid]
    fixture_verified: bool
    operations: list[OperationSim]


@dataclass
class SimulationResult:
    passed: bool
    events: list[dict[str, Any]]
    event_counts: dict[str, int]
    max_severity: str | None
    time_breakdown: dict[str, float]
    cycle_time_seconds: float
    stock_comparison: dict[str, Any]
    remaining_stock: dict[str, Any]
    programmed_envelope: dict[str, Any]
    per_operation: list[dict[str, Any]]
    engine_version: str = ENGINE_VERSION


class _EventSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._seen: set[tuple] = set()

    def add(
        self,
        *,
        severity: Severity,
        code: str,
        message: str,
        consequence: str,
        recommendation: str,
        time_s: float = 0.0,
        setup: str | None = None,
        operation_id: str | None = None,
        move_index: int | None = None,
        entities: list[str] | None = None,
        position: tuple[float, float, float] | None = None,
        detail: dict[str, Any] | None = None,
        collapse_key: tuple | None = None,
    ) -> None:
        # A single crash produces thousands of samples; report the first and
        # count the rest rather than burying the operator in duplicates.
        key = collapse_key or (code, setup, operation_id, tuple(entities or ()))
        if key in self._seen:
            for event in self.events:
                if event.get("_key") == list(key):
                    event["occurrences"] += 1
                    return
            return
        self._seen.add(key)
        self.events.append(
            {
                "_key": list(key),
                "severity": severity.value,
                "code": code,
                "message": message,
                "consequence": consequence,
                "recommendation": recommendation,
                "time_s": round(time_s, 3),
                "setup": setup,
                "operation_id": operation_id,
                "move_index": move_index,
                "entities": entities or [],
                "position": [round(v, 3) for v in position] if position else None,
                "detail": detail or {},
                "occurrences": 1,
            }
        )


def simulate(
    *,
    part_model: PartModel,
    setups: list[SetupSim],
    machine: MachineModel,
    voxel_pitch: float = 1.0,
    tolerance_mm: float = 0.05,
    grid_pitch: float | None = None,
) -> SimulationResult:
    sink = _EventSink()
    grid = grid_for(part_model, grid_pitch or voxel_pitch)
    lo, hi = part_model.bbox()
    voxels = VoxelStock(lo, hi, voxel_pitch)
    target = target_heightfield(part_model, grid)

    totals = TimeBreakdown()
    per_operation: list[dict[str, Any]] = []
    envelope_points: list[list[float]] = []
    clock = 0.0

    for setup in setups:
        frame = SetupFrame.from_spec(setup.orientation_deg, setup.origin_mm)
        height = voxels.project(frame, grid)
        stock = HeightFieldStock(height, grid, voxels.frame_floor(frame))

        if setup.index_position:
            totals.indexing += machine.index_seconds
            clock += machine.index_seconds
        if not setup.fixture_verified:
            sink.add(
                severity=Severity.S2_ENGINEER,
                code="fixture_unverified",
                message=f"Fixture model for setup {setup.sequence} has not been verified",
                consequence="Collision results for this setup cannot be trusted",
                recommendation="Verify the fixture geometry against the physical setup, then re-simulate",
                setup=setup.name,
                time_s=clock,
            )

        last_tool: str | None = None
        last_rpm = 0.0

        for operation in setup.operations:
            if operation.suppressed:
                continue
            op_time = TimeBreakdown()
            if operation.tool.id != last_tool:
                op_time.tool_change += machine.tool_change_seconds
                last_tool = operation.tool.id
            if abs(operation.rpm - last_rpm) > 1.0:
                op_time.spindle_change += spindle_change_time(last_rpm, operation.rpm, machine)
                last_rpm = operation.rpm

            _check_operation_limits(sink, operation, machine, setup, clock)
            removed = _replay(
                sink=sink,
                operation=operation,
                setup=setup,
                machine=machine,
                stock=stock,
                grid=grid,
                op_time=op_time,
                clock_start=clock + op_time.total,
                envelope_points=envelope_points,
            )

            totals.add(op_time)
            clock += op_time.total
            per_operation.append(
                {
                    "operation_id": operation.operation_id,
                    "label": operation.label,
                    "setup": setup.name,
                    "tool": operation.tool.code,
                    "removed_volume_mm3": round(removed, 2),
                    "time": op_time.to_dict(),
                    "end_time_s": round(clock, 2),
                }
            )

        voxels.apply_height_field(frame, grid, stock.height)

    machined = voxels.part_height_field(grid)
    comparison = compare_to_target(machined, target, grid, tolerance_mm)

    # The height field catches sub-millimetre surface deviation from +Z; the
    # occupancy grid catches material left or removed from any direction, which
    # is what a flipped or indexed setup needs.
    target_voxels = target_occupancy(part_model, lo, hi, voxel_pitch)
    volume = compare_occupancy(voxels.occupied, target_voxels, voxel_pitch)
    comparison["volume"] = volume
    comparison["conformant"] = comparison["conformant"] and volume["conformant"]

    _report_conformance(sink, comparison, machined, target, grid, clock)
    _report_volume(sink, volume, voxels.occupied, target_voxels, lo, voxel_pitch, clock)

    counts: dict[str, int] = {}
    for event in sink.events:
        counts[event["severity"]] = counts.get(event["severity"], 0) + 1
        event.pop("_key", None)

    order = [Severity.S1_STOP, Severity.S2_ENGINEER, Severity.S3_WARNING, Severity.S4_ADVISORY]
    max_severity = next((s.value for s in order if counts.get(s.value)), None)
    passed = not counts.get(Severity.S1_STOP.value) and not counts.get(Severity.S2_ENGINEER.value)

    points = np.array(envelope_points, dtype=np.float64) if envelope_points else np.zeros((0, 3))
    return SimulationResult(
        passed=passed,
        events=sink.events,
        event_counts=counts,
        max_severity=max_severity,
        time_breakdown=totals.to_dict(),
        cycle_time_seconds=round(totals.total, 2),
        stock_comparison=comparison,
        remaining_stock={
            "grid": grid.to_dict(),
            "volume_mm3": round(voxels.volume(), 1),
            "height": _downsample(machined, 2),
        },
        programmed_envelope=collision.envelope_of(points),
        per_operation=per_operation,
    )


def _downsample(field_z: np.ndarray, step: int) -> list[list[float]]:
    return [[round(float(v), 3) for v in row] for row in field_z[::step, ::step]]


def _check_operation_limits(
    sink: _EventSink, operation: OperationSim, machine: MachineModel, setup: SetupSim, clock: float
) -> None:
    tool = operation.tool
    if operation.rpm > machine.max_rpm + 1e-6:
        sink.add(
            severity=Severity.S1_STOP,
            code="spindle_over_machine_limit",
            message=f"{operation.label} commands {operation.rpm:.0f} rpm, above the machine maximum {machine.max_rpm:.0f} rpm",
            consequence="The controller would alarm or the spindle would be overdriven",
            recommendation="Regenerate cutting parameters against this machine",
            setup=setup.name,
            operation_id=operation.operation_id,
            time_s=clock,
            entities=[tool.code, machine.code],
        )
    if operation.rpm > tool.max_rpm + 1e-6:
        sink.add(
            severity=Severity.S1_STOP,
            code="spindle_over_tool_limit",
            message=f"{operation.label} commands {operation.rpm:.0f} rpm, above the {tool.code} maximum {tool.max_rpm:.0f} rpm",
            consequence="Tool or holder could fail at speed",
            recommendation="Select a tool rated for this speed or reduce the surface speed",
            setup=setup.name,
            operation_id=operation.operation_id,
            time_s=clock,
            entities=[tool.code],
        )
    if operation.feed_mm_min > machine.max_feed_mm_min + 1e-6:
        sink.add(
            severity=Severity.S2_ENGINEER,
            code="feed_over_machine_limit",
            message=f"{operation.label} commands {operation.feed_mm_min:.0f} mm/min, above the machine maximum",
            consequence="The controller would clamp the feed, invalidating the cycle-time estimate",
            recommendation="Regenerate the operation against this machine's feed limit",
            setup=setup.name,
            operation_id=operation.operation_id,
            time_s=clock,
        )
    if not tool.has_collision_model:
        sink.add(
            severity=Severity.S2_ENGINEER,
            code="tool_missing_collision_model",
            message=f"Tool assembly {tool.code} has no collision geometry",
            consequence="Holder and shank collisions cannot be detected for this operation",
            recommendation="Add the assembly collision profile in the tool manager",
            setup=setup.name,
            operation_id=operation.operation_id,
            time_s=clock,
            entities=[tool.code],
        )


def _replay(
    *,
    sink: _EventSink,
    operation: OperationSim,
    setup: SetupSim,
    machine: MachineModel,
    stock: HeightFieldStock,
    grid: Grid,
    op_time: TimeBreakdown,
    clock_start: float,
    envelope_points: list[list[float]],
) -> float:
    tool = operation.tool
    position = (0.0, 0.0, setup.clearance_plane_mm)
    removed_total = 0.0
    clock = clock_start

    for index, move in enumerate(operation.moves):
        kind = move.get("t")
        if kind == "dwell":
            op_time.dwell += float(move.get("seconds", 0.0))
            clock += float(move.get("seconds", 0.0))
            continue

        if kind == "drill":
            target_point = (float(move["x"]), float(move["y"]), float(move["cycle"]["z_depth"]))
            approach = (target_point[0], target_point[1], float(move["cycle"]["r_plane"]))
            op_time.rapid += rapid_move_time(_delta(position, approach), machine)
            clock += op_time.rapid
            _verify_point(sink, approach, tool, setup, operation, machine, stock, index, clock, rapid=True)
            duration = drill_cycle_time(move["cycle"], machine)
            op_time.cutting += duration
            clock += duration
            removed_total += stock.cut(approach, target_point, tool.radius, 0.0)
            _verify_point(sink, target_point, tool, setup, operation, machine, stock, index, clock, rapid=False)
            envelope_points.append(list(target_point))
            position = approach
            continue

        target_point = (
            float(move.get("x", position[0])),
            float(move.get("y", position[1])),
            float(move.get("z", position[2])),
        )
        envelope_points.append(list(target_point))
        delta = _delta(position, target_point)

        if kind == "rapid":
            duration = rapid_move_time(delta, machine)
            op_time.rapid += duration
            clock += duration
            for sample in collision.sample_move(position, target_point, SAMPLE_STEP_MM):
                _verify_point(sink, sample, tool, setup, operation, machine, stock, index, clock, rapid=True)
        else:
            feed = float(move.get("f", operation.feed_mm_min))
            duration = linear_move_time(delta, feed, machine)
            op_time.cutting += duration
            clock += duration
            previous = position
            for sample in collision.sample_move(position, target_point, SAMPLE_STEP_MM):
                _verify_point(sink, sample, tool, setup, operation, machine, stock, index, clock, rapid=False)
                removed_total += stock.cut(previous, sample, tool.radius, tool.corner_radius)
                previous = sample

        position = target_point

    return removed_total


def _delta(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (b[0] - a[0], b[1] - a[1], b[2] - a[2])


def _verify_point(
    sink: _EventSink,
    point: tuple[float, float, float],
    tool: ToolSpec,
    setup: SetupSim,
    operation: OperationSim,
    machine: MachineModel,
    stock: HeightFieldStock,
    move_index: int,
    clock: float,
    *,
    rapid: bool,
) -> None:
    x, y, z = point

    ok, reason = within_travel(point, machine)
    if not ok:
        sink.add(
            severity=Severity.S1_STOP,
            code="axis_travel_violation",
            message=f"{operation.label}: {reason}",
            consequence="The controller would fault or the axis would overtravel",
            recommendation="Reposition the work offset or select a machine with sufficient travel",
            setup=setup.name,
            operation_id=operation.operation_id,
            move_index=move_index,
            time_s=clock,
            entities=[machine.code],
            position=point,
        )

    for segment in tool.segments:
        # -- against remaining stock -------------------------------------
        height = stock.max_height_in((x, y), (x, y), segment.radius)
        if segment.cutting:
            if rapid and height > z + 1e-6:
                sink.add(
                    severity=Severity.S1_STOP,
                    code="rapid_into_stock",
                    message=f"{operation.label}: rapid move enters material at Z{z:.3f} where stock stands at Z{height:.3f}",
                    consequence="The cutter would strike the workpiece at rapid rate",
                    recommendation="Raise the clearance plane or add a feed approach before this move",
                    setup=setup.name,
                    operation_id=operation.operation_id,
                    move_index=move_index,
                    time_s=clock,
                    entities=[tool.code, "stock"],
                    position=point,
                )
        else:
            penetration = collision.segment_hits_stock(z, segment, height)
            if penetration is not None and penetration > 1e-6:
                sink.add(
                    severity=Severity.S1_STOP,
                    code="assembly_into_stock",
                    message=(
                        f"{operation.label}: {segment.name} of {tool.code} is {penetration:.2f} mm inside "
                        f"stock standing at Z{height:.3f}"
                    ),
                    consequence="The non-cutting part of the assembly would rub or crash into the workpiece",
                    recommendation="Use a longer reach assembly, reduce the depth of cut, or reorder the setup",
                    setup=setup.name,
                    operation_id=operation.operation_id,
                    move_index=move_index,
                    time_s=clock,
                    entities=[tool.code, segment.name, "stock"],
                    position=point,
                    detail={"penetration_mm": round(penetration, 3)},
                )

        # -- against fixture, clamps and jaws ----------------------------
        for solid in setup.fixture_solids:
            clearance = float(solid.data.get("clearance", 0.0))
            penetration = collision.segment_hits_solid(z, segment, x, y, solid, clearance)
            if penetration is None:
                continue
            sink.add(
                severity=Severity.S1_STOP,
                code="fixture_collision",
                message=(
                    f"{operation.label}: {segment.name} of {tool.code} intersects {solid.name} "
                    f"by {penetration:.2f} mm"
                ),
                consequence="Tool, holder or spindle would strike the workholding",
                recommendation="Move the clamp, change the setup orientation, or restrict the path to a safe region",
                setup=setup.name,
                operation_id=operation.operation_id,
                move_index=move_index,
                time_s=clock,
                entities=[tool.code, segment.name, solid.name],
                position=point,
                detail={"penetration_mm": round(penetration, 3)},
            )


def _report_volume(
    sink: _EventSink,
    volume: dict[str, Any],
    machined: np.ndarray,
    target: np.ndarray,
    lo: list[float],
    pitch: float,
    clock: float,
) -> None:
    """Volume conformance, independent of the direction the part was cut from."""
    if volume["overcut_voxels"]:
        missing = target & ~machined
        position = worst_occupancy_location(missing, lo, pitch)
        sink.add(
            severity=Severity.S1_STOP,
            code="volume_overcut",
            message=(
                f"{volume['overcut_volume_mm3']:.1f} mm3 of the finished part has been machined away "
                f"({volume['overcut_voxels']} voxels at {pitch} mm)"
            ),
            consequence="The part would be undersize or breached; the condition cannot be corrected downstream",
            recommendation="Correct the offending operation or the stock-to-leave, then re-simulate",
            time_s=clock,
            entities=["stock", "part"],
            position=tuple(position) if position else None,
            detail=volume,
        )


def _report_conformance(
    sink: _EventSink,
    comparison: dict[str, Any],
    machined: np.ndarray,
    target: np.ndarray,
    grid: Grid,
    clock: float,
) -> None:
    if comparison["overcut_cells"]:
        overcut = np.clip(target - machined, 0.0, None)
        x, y, depth = locate_worst(overcut, grid)
        sink.add(
            severity=Severity.S1_STOP,
            code="overcut",
            message=f"Finished surface is cut {depth:.3f} mm below the part at X{x:.2f} Y{y:.2f}",
            consequence="The part would be scrap; the condition cannot be corrected downstream",
            recommendation="Correct the offending toolpath or the stock-to-leave, then re-simulate",
            time_s=clock,
            entities=["stock", "part"],
            position=(x, y, float(target[int((x - grid.x0) / grid.pitch), int((y - grid.y0) / grid.pitch)])),
            detail={
                "max_overcut_mm": round(comparison["overcut_max_mm"], 4),
                "volume_mm3": round(comparison["overcut_volume_mm3"], 2),
            },
        )
    if comparison["remaining_cells"]:
        excess = np.clip(machined - target, 0.0, None)
        x, y, height = locate_worst(excess, grid)
        sink.add(
            severity=Severity.S3_WARNING,
            code="stock_remaining",
            message=f"{height:.3f} mm of stock remains above the finished surface at X{x:.2f} Y{y:.2f}",
            consequence="The part would not meet the modelled geometry without a further operation",
            recommendation="Add a rest or finishing operation covering the remaining region",
            time_s=clock,
            entities=["stock"],
            position=(x, y, height),
            detail={
                "max_remaining_mm": round(comparison["remaining_max_mm"], 4),
                "volume_mm3": round(comparison["remaining_volume_mm3"], 2),
                "cells": comparison["remaining_cells"],
            },
        )
