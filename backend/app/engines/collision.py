"""Collision geometry for full-machine verification (FR-SIM-001).

The cutter is only the first segment of the tool. Shank, extension and holder
are modelled as further segments, which is what makes a holder crash - the most
common real collision - detectable rather than invisible.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass(slots=True)
class ToolSegment:
    """One cylindrical section of the assembly, measured from the tool tip."""

    name: str
    radius: float
    z_from: float  # distance above the tip where this segment starts
    z_to: float
    cutting: bool = False


def build_segments(
    *,
    cutter_diameter: float,
    flute_length: float,
    shank_diameter: float | None,
    collision_profile: list[list[float]] | None,
    gauge_length: float,
) -> list[ToolSegment]:
    """Assemble the tool silhouette from the tip upward."""
    segments = [
        ToolSegment("flute", cutter_diameter / 2.0, 0.0, max(flute_length, 0.1), cutting=True),
    ]
    height = max(flute_length, 0.1)

    if collision_profile:
        for index, entry in enumerate(collision_profile):
            diameter, length = float(entry[0]), float(entry[1])
            if length <= 0:
                continue
            segments.append(ToolSegment(f"assembly_{index}", diameter / 2.0, height, height + length))
            height += length
    else:
        shank = shank_diameter or cutter_diameter
        segments.append(ToolSegment("shank", shank / 2.0, height, max(height + 20.0, gauge_length * 0.4)))
        height = max(height + 20.0, gauge_length * 0.4)
        segments.append(ToolSegment("holder", max(shank, cutter_diameter) * 1.6, height, gauge_length))
    return segments


@dataclass(slots=True)
class Solid:
    """A fixture, clamp or machine element in setup coordinates."""

    name: str
    kind: str  # "box" or "cylinder"
    data: dict[str, Any]

    def distance_to_axis(self, x: float, y: float) -> float:
        """Planar distance from the tool axis to this solid, negative inside."""
        if self.kind == "cylinder":
            cx, cy = self.data["center"][0], self.data["center"][1]
            return math.hypot(x - cx, y - cy) - float(self.data["radius"])
        lo = self.data["min"]
        hi = self.data["max"]
        dx = max(lo[0] - x, 0.0, x - hi[0])
        dy = max(lo[1] - y, 0.0, y - hi[1])
        if dx == 0.0 and dy == 0.0:
            return -min(x - lo[0], hi[0] - x, y - lo[1], hi[1] - y)
        return math.hypot(dx, dy)

    def z_span(self) -> tuple[float, float]:
        if self.kind == "cylinder":
            return float(self.data["z_min"]), float(self.data["z_max"])
        return float(self.data["min"][2]), float(self.data["max"][2])


def load_solids(specs: list[dict[str, Any]]) -> list[Solid]:
    return [Solid(name=s.get("name", f"solid_{i}"), kind=s.get("type", "box"), data=s) for i, s in enumerate(specs)]


def segment_hits_solid(
    tip_z: float, segment: ToolSegment, x: float, y: float, solid: Solid, clearance: float
) -> float | None:
    """Penetration depth of a tool segment into a solid, or None if clear."""
    seg_lo, seg_hi = tip_z + segment.z_from, tip_z + segment.z_to
    solid_lo, solid_hi = solid.z_span()
    if seg_hi <= solid_lo or seg_lo >= solid_hi:
        return None
    planar = solid.distance_to_axis(x, y) - segment.radius - clearance
    if planar >= 0:
        return None
    return -planar


def segment_hits_stock(
    tip_z: float,
    segment: ToolSegment,
    height_in_footprint: float,
    clearance: float = 0.0,
) -> float | None:
    """Penetration of a non-cutting segment into remaining stock."""
    seg_lo = tip_z + segment.z_from
    if height_in_footprint <= seg_lo + clearance:
        return None
    return float(height_in_footprint - seg_lo)


def reach_shortfall(required_depth: float, flute_length: float, clearance_needed: float, gauge_length: float) -> float | None:
    """How much tool length is missing for a cut, or None when it reaches."""
    if required_depth <= flute_length and required_depth + clearance_needed <= gauge_length:
        return None
    return max(required_depth - flute_length, required_depth + clearance_needed - gauge_length)


def sample_move(
    a: tuple[float, float, float], b: tuple[float, float, float], step_mm: float
) -> list[tuple[float, float, float]]:
    """Discretise a move finely enough that a thin obstacle cannot be skipped."""
    distance = math.dist(a, b)
    if distance <= step_mm:
        return [b]
    count = int(math.ceil(distance / step_mm))
    return [
        (
            a[0] + (b[0] - a[0]) * i / count,
            a[1] + (b[1] - a[1]) * i / count,
            a[2] + (b[2] - a[2]) * i / count,
        )
        for i in range(1, count + 1)
    ]


def envelope_of(points: np.ndarray) -> dict[str, list[float]]:
    if points.size == 0:
        return {"min": [0.0, 0.0, 0.0], "max": [0.0, 0.0, 0.0]}
    return {
        "min": [float(points[:, i].min()) for i in range(3)],
        "max": [float(points[:, i].max()) for i in range(3)],
    }
