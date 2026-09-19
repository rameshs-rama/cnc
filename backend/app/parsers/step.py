"""STEP (ISO 10303) extraction.

A full B-Rep reader is a kernel-scale undertaking and is explicitly a build or
buy decision in PRD 15.3. What this reader does is deterministic and useful on
its own: it reads the header, resolves the length unit, computes the point cloud
bounding box from CARTESIAN_POINT entities, and collects cylindrical surfaces
and circles as hole candidates.

Every value it produces is a candidate requiring verification. It never asserts
a tolerance, a material or a thread.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_POINT = re.compile(
    r"CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*\)",
    re.IGNORECASE,
)
_CIRCLE = re.compile(r"CIRCLE\s*\(\s*'[^']*'\s*,\s*#(\d+)\s*,\s*([-\d.eE+]+)\s*\)", re.IGNORECASE)
_CYLINDER = re.compile(r"CYLINDRICAL_SURFACE\s*\(\s*'[^']*'\s*,\s*#(\d+)\s*,\s*([-\d.eE+]+)\s*\)", re.IGNORECASE)
_AXIS2 = re.compile(r"#(\d+)\s*=\s*AXIS2_PLACEMENT_3D\s*\(\s*'[^']*'\s*,\s*#(\d+)\s*(?:,\s*#(\d+))?", re.IGNORECASE)
_POINT_ID = re.compile(
    r"#(\d+)\s*=\s*CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*\)",
    re.IGNORECASE,
)
_DIRECTION_ID = re.compile(
    r"#(\d+)\s*=\s*DIRECTION\s*\(\s*'[^']*'\s*,\s*\(\s*([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*\)",
    re.IGNORECASE,
)
_PRODUCT = re.compile(r"PRODUCT\s*\(\s*'([^']*)'\s*,\s*'([^']*)'", re.IGNORECASE)
_SCHEMA = re.compile(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']*)'", re.IGNORECASE)
_NAME = re.compile(r"FILE_NAME\s*\(\s*'([^']*)'\s*,\s*'([^']*)'", re.IGNORECASE)

_UNIT_SCALE = {
    "MILLI": 1.0,
    "CENTI": 10.0,
    "DECI": 100.0,
    "METRE": 1000.0,
    "INCH": 25.4,
}

#: Guard against a decompression or regex blow-up on a hostile file.
MAX_BYTES = 80 * 1024 * 1024
MAX_ENTITIES = 400_000


@dataclass
class StepExtract:
    schema: str = ""
    product_name: str = ""
    originating_system: str = ""
    units: str = "mm"
    unit_scale_to_mm: float = 1.0
    point_count: int = 0
    bbox_min: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    bbox_max: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    cylinders: list[dict[str, Any]] = field(default_factory=list)
    circles: list[dict[str, Any]] = field(default_factory=list)
    entity_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "product_name": self.product_name,
            "originating_system": self.originating_system,
            "units": self.units,
            "unit_scale_to_mm": self.unit_scale_to_mm,
            "point_count": self.point_count,
            "bbox_min_mm": [round(v, 4) for v in self.bbox_min],
            "bbox_max_mm": [round(v, 4) for v in self.bbox_max],
            "size_mm": [round(self.bbox_max[i] - self.bbox_min[i], 4) for i in range(3)],
            "cylinders": self.cylinders,
            "circles": self.circles,
            "entity_counts": self.entity_counts,
            "warnings": self.warnings,
            "truncated": self.truncated,
        }


def parse(data: bytes) -> StepExtract:
    result = StepExtract()
    if len(data) > MAX_BYTES:
        data = data[:MAX_BYTES]
        result.truncated = True
        result.warnings.append(f"File truncated to {MAX_BYTES} bytes for parsing")

    text = data.decode("utf-8", errors="replace")

    if match := _SCHEMA.search(text):
        result.schema = match.group(1)
    if match := _NAME.search(text):
        result.originating_system = match.group(2)
    if match := _PRODUCT.search(text):
        result.product_name = match.group(2) or match.group(1)

    result.unit_scale_to_mm, result.units = _resolve_unit(text)
    scale = result.unit_scale_to_mm

    points: list[tuple[float, float, float]] = []
    for index, match in enumerate(_POINT.finditer(text)):
        if index >= MAX_ENTITIES:
            result.truncated = True
            result.warnings.append(f"Stopped after {MAX_ENTITIES} points")
            break
        try:
            points.append(
                (float(match.group(1)) * scale, float(match.group(2)) * scale, float(match.group(3)) * scale)
            )
        except ValueError:
            continue

    result.point_count = len(points)
    if points:
        result.bbox_min = [min(p[i] for p in points) for i in range(3)]
        result.bbox_max = [max(p[i] for p in points) for i in range(3)]
    else:
        result.warnings.append("No CARTESIAN_POINT entities found; the file may be compressed or non-standard")

    placements = _index_placements(text, scale)
    result.cylinders = _collect_radial(text, _CYLINDER, placements, scale, "cylindrical_surface")
    result.circles = _collect_radial(text, _CIRCLE, placements, scale, "circle")

    for entity in ("ADVANCED_FACE", "CLOSED_SHELL", "MANIFOLD_SOLID_BREP", "PLANE", "B_SPLINE_SURFACE", "CONICAL_SURFACE"):
        count = len(re.findall(rf"\b{entity}\b", text, re.IGNORECASE))
        if count:
            result.entity_counts[entity] = count

    if result.entity_counts.get("B_SPLINE_SURFACE"):
        result.warnings.append(
            "File contains B-spline surfaces; free-form regions require engineering review before planning"
        )
    return result


def _resolve_unit(text: str) -> tuple[float, str]:
    window = text[: text.find("DATA;") + 20000] if "DATA;" in text else text[:40000]
    if re.search(r"CONVERSION_BASED_UNIT[^;]*INCH", window, re.IGNORECASE):
        return 25.4, "inch"
    for prefix, scale in _UNIT_SCALE.items():
        if prefix == "METRE":
            continue
        if re.search(rf"SI_UNIT\s*\(\s*\.{prefix}\.\s*,\s*\.METRE\.", window, re.IGNORECASE):
            return scale, f"{prefix.lower()}metre"
    if re.search(r"SI_UNIT\s*\(\s*\$\s*,\s*\.METRE\.", window, re.IGNORECASE):
        return 1000.0, "metre"
    return 1.0, "mm"


def _index_placements(text: str, scale: float) -> dict[str, dict[str, Any]]:
    points = {
        m.group(1): (float(m.group(2)) * scale, float(m.group(3)) * scale, float(m.group(4)) * scale)
        for m in _POINT_ID.finditer(text)
    }
    directions = {
        m.group(1): (float(m.group(2)), float(m.group(3)), float(m.group(4))) for m in _DIRECTION_ID.finditer(text)
    }
    placements: dict[str, dict[str, Any]] = {}
    for match in _AXIS2.finditer(text):
        placement_id, origin_ref, axis_ref = match.group(1), match.group(2), match.group(3)
        placements[placement_id] = {
            "origin": points.get(origin_ref),
            "axis": directions.get(axis_ref) if axis_ref else None,
        }
    return placements


def _collect_radial(
    text: str, pattern: re.Pattern[str], placements: dict[str, dict[str, Any]], scale: float, source: str
) -> list[dict[str, Any]]:
    seen: dict[tuple, dict[str, Any]] = {}
    for match in pattern.finditer(text):
        placement = placements.get(match.group(1)) or {}
        try:
            radius = float(match.group(2)) * scale
        except ValueError:
            continue
        origin = placement.get("origin")
        axis = placement.get("axis")
        key = (
            round(radius, 3),
            tuple(round(v, 2) for v in origin) if origin else None,
        )
        if key in seen:
            seen[key]["occurrences"] += 1
            continue
        seen[key] = {
            "source": source,
            "diameter_mm": round(radius * 2.0, 4),
            "center": [round(v, 4) for v in origin] if origin else None,
            "axis": [round(v, 4) for v in axis] if axis else None,
            "axis_is_z": bool(axis and abs(abs(axis[2]) - 1.0) < 1e-6),
            "occurrences": 1,
        }
    ordered = sorted(seen.values(), key=lambda c: (-c["occurrences"], c["diameter_mm"]))
    return ordered[:256]


def hole_candidates(extract: StepExtract, top_z: float | None = None) -> list[dict[str, Any]]:
    """Cylinders whose axis is Z, deduplicated into likely hole features.

    These are candidates only. Depth, through-state and any thread remain
    unknown until an engineer confirms them.
    """
    candidates: list[dict[str, Any]] = []
    for entry in extract.cylinders:
        if not entry.get("axis_is_z") or not entry.get("center"):
            continue
        if entry["diameter_mm"] <= 0.2 or entry["diameter_mm"] > 200.0:
            continue
        candidates.append(
            {
                "center": [entry["center"][0], entry["center"][1]],
                "diameter": entry["diameter_mm"],
                "z_reference": entry["center"][2] if top_z is None else top_z,
                "confidence": 0.78,
                "method": "STEP cylindrical surface with Z axis",
                "note": "Depth and through-state are not established by this extraction",
            }
        )
    candidates.sort(key=lambda c: (c["center"][0], c["center"][1]))
    return candidates
