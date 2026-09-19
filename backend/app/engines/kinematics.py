"""Machine kinematics, travel limits and cycle-time decomposition.

Cycle time is built from a trapezoidal velocity profile per move rather than
dividing length by feed, because acceleration dominates on the short segments
that make up a finishing path (FR-SIM-003).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class MachineModel:
    code: str
    kinematics: str = "3axis"
    travels_mm: dict[str, list[float]] = field(default_factory=dict)
    rapid_mm_min: dict[str, float] = field(default_factory=dict)
    acceleration_mm_s2: dict[str, float] = field(default_factory=dict)
    max_feed_mm_min: float = 10000.0
    max_rpm: float = 12000.0
    min_rpm: float = 60.0
    tool_change_seconds: float = 6.0
    index_seconds: float = 8.0
    spindle_accel_rpm_s: float = 4000.0
    rotary_axes: dict[str, Any] = field(default_factory=dict)

    def travel(self, axis: str) -> tuple[float, float]:
        span = self.travels_mm.get(axis)
        if not span:
            return (-1e9, 1e9)
        return float(span[0]), float(span[1])

    def rapid(self, axis: str) -> float:
        return float(self.rapid_mm_min.get(axis, 20000.0))

    def accel(self, axis: str) -> float:
        return float(self.acceleration_mm_s2.get(axis, 3000.0))

    @staticmethod
    def from_record(machine: Any) -> MachineModel:
        return MachineModel(
            code=machine.code,
            kinematics=machine.kinematics,
            travels_mm=dict(machine.travels_mm or {}),
            rapid_mm_min=dict(machine.rapid_mm_min or {}),
            acceleration_mm_s2=dict(machine.acceleration_mm_s2 or {}),
            max_feed_mm_min=machine.max_feed_mm_min,
            max_rpm=machine.max_rpm,
            min_rpm=machine.min_rpm,
            tool_change_seconds=machine.tool_change_seconds,
            index_seconds=machine.index_seconds,
            rotary_axes=dict(machine.rotary_axes or {}),
        )


def trapezoid_time(distance: float, velocity_mm_s: float, accel_mm_s2: float) -> float:
    """Time to traverse ``distance`` with a trapezoidal velocity profile."""
    if distance <= 1e-9:
        return 0.0
    v = max(velocity_mm_s, 1e-6)
    a = max(accel_mm_s2, 1e-6)
    ramp_distance = v * v / a  # accelerate up and back down
    if ramp_distance >= distance:
        peak = math.sqrt(a * distance)
        return 2.0 * peak / a
    return 2.0 * (v / a) + (distance - ramp_distance) / v


def linear_move_time(delta: tuple[float, float, float], feed_mm_min: float, machine: MachineModel) -> float:
    """Feed move: the commanded vector feed, limited by the machine maximum."""
    distance = math.dist((0.0, 0.0, 0.0), delta)
    if distance <= 1e-9:
        return 0.0
    feed = min(feed_mm_min, machine.max_feed_mm_min) / 60.0
    # Effective acceleration along the move direction.
    unit = [abs(c) / distance for c in delta]
    accel = min(
        machine.accel(axis) / max(component, 1e-6)
        for axis, component in zip(("X", "Y", "Z"), unit, strict=False)
        if component > 1e-6
    )
    return trapezoid_time(distance, feed, accel)


def rapid_move_time(delta: tuple[float, float, float], machine: MachineModel) -> float:
    """Rapid move: axes move independently, so the slowest axis sets the time."""
    worst = 0.0
    for axis, component in zip(("X", "Y", "Z"), delta, strict=False):
        distance = abs(component)
        if distance <= 1e-9:
            continue
        worst = max(worst, trapezoid_time(distance, machine.rapid(axis) / 60.0, machine.accel(axis)))
    return worst


def spindle_change_time(from_rpm: float, to_rpm: float, machine: MachineModel) -> float:
    return abs(to_rpm - from_rpm) / max(machine.spindle_accel_rpm_s, 1.0)


def drill_cycle_time(cycle: dict[str, Any], machine: MachineModel) -> float:
    """Time for one canned drilling cycle including pecks and retracts."""
    r_plane = float(cycle.get("r_plane", 0.0))
    z_depth = float(cycle.get("z_depth", 0.0))
    depth = abs(r_plane - z_depth)
    feed = min(float(cycle.get("feed", 100.0)), machine.max_feed_mm_min) / 60.0
    rapid_z = machine.rapid("Z") / 60.0
    accel_z = machine.accel("Z")

    cut = trapezoid_time(depth, feed, accel_z)
    dwell = float(cycle.get("dwell_s", 0.0))
    retract = trapezoid_time(depth, rapid_z, accel_z)

    peck = cycle.get("peck")
    if peck:
        steps = max(1, int(math.ceil(depth / float(peck))))
        if cycle.get("type") == "G83":
            # Full retract to R on every peck.
            retract = sum(trapezoid_time(depth * (i + 1) / steps, rapid_z, accel_z) * 2 for i in range(steps))
        else:
            retract += steps * 0.15
    return cut + dwell + retract


def within_travel(point: tuple[float, float, float], machine: MachineModel) -> tuple[bool, str | None]:
    """Check one programmed position against the machine travel envelope."""
    for axis, value in zip(("X", "Y", "Z"), point, strict=False):
        low, high = machine.travel(axis)
        if value < low - 1e-6:
            return False, f"{axis} axis below travel limit: {value:.3f} mm < {low:.3f} mm"
        if value > high + 1e-6:
            return False, f"{axis} axis beyond travel limit: {value:.3f} mm > {high:.3f} mm"
    return True, None


@dataclass
class TimeBreakdown:
    """Cycle-time components required by FR-SIM-003."""

    cutting: float = 0.0
    rapid: float = 0.0
    dwell: float = 0.0
    tool_change: float = 0.0
    spindle_change: float = 0.0
    indexing: float = 0.0

    @property
    def total(self) -> float:
        return self.cutting + self.rapid + self.dwell + self.tool_change + self.spindle_change + self.indexing

    def to_dict(self) -> dict[str, float]:
        return {
            "cutting_s": round(self.cutting, 2),
            "rapid_s": round(self.rapid, 2),
            "dwell_s": round(self.dwell, 2),
            "tool_change_s": round(self.tool_change, 2),
            "spindle_change_s": round(self.spindle_change, 2),
            "indexing_s": round(self.indexing, 2),
            "total_s": round(self.total, 2),
        }

    def add(self, other: TimeBreakdown) -> None:
        self.cutting += other.cutting
        self.rapid += other.rapid
        self.dwell += other.dwell
        self.tool_change += other.tool_change
        self.spindle_change += other.spindle_change
        self.indexing += other.indexing
