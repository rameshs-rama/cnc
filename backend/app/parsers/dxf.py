"""DXF reading for 2D profiles and hole positions.

DXF is a group-code stream: an integer code line followed by a value line. The
reader walks entities in the ENTITIES section and extracts circles, arcs, lines
and polylines, which is enough to propose a part outline and a hole pattern for
a prismatic component.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_LINES = 4_000_000


@dataclass
class DxfExtract:
    units: str = "mm"
    circles: list[dict[str, Any]] = field(default_factory=list)
    arcs: list[dict[str, Any]] = field(default_factory=list)
    lines: list[dict[str, Any]] = field(default_factory=list)
    polylines: list[dict[str, Any]] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    layers: list[str] = field(default_factory=list)
    bbox_min: list[float] = field(default_factory=lambda: [0.0, 0.0])
    bbox_max: list[float] = field(default_factory=lambda: [0.0, 0.0])
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "units": self.units,
            "counts": {
                "circles": len(self.circles),
                "arcs": len(self.arcs),
                "lines": len(self.lines),
                "polylines": len(self.polylines),
                "texts": len(self.texts),
            },
            "circles": self.circles[:256],
            "arcs": self.arcs[:256],
            "polylines": self.polylines[:64],
            "texts": self.texts[:128],
            "layers": sorted(set(self.layers))[:64],
            "bbox_min_mm": [round(v, 4) for v in self.bbox_min],
            "bbox_max_mm": [round(v, 4) for v in self.bbox_max],
            "size_mm": [round(self.bbox_max[i] - self.bbox_min[i], 4) for i in range(2)],
            "warnings": self.warnings,
        }


#: $INSUNITS values that matter for a machining drawing.
_INSUNITS = {1: ("inch", 25.4), 4: ("mm", 1.0), 5: ("cm", 10.0), 6: ("m", 1000.0)}


def parse(data: bytes) -> DxfExtract:
    result = DxfExtract()
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    raw_lines = text.split("\n")
    if len(raw_lines) > MAX_LINES:
        raw_lines = raw_lines[:MAX_LINES]
        result.warnings.append(f"File truncated to {MAX_LINES} lines for parsing")

    pairs: list[tuple[int, str]] = []
    index = 0
    while index + 1 < len(raw_lines):
        code_text = raw_lines[index].strip()
        value = raw_lines[index + 1].strip()
        index += 2
        try:
            pairs.append((int(code_text), value))
        except ValueError:
            continue

    scale = _resolve_units(pairs, result)
    _walk_entities(pairs, scale, result)
    _compute_bounds(result)
    return result


def _resolve_units(pairs: list[tuple[int, str]], result: DxfExtract) -> float:
    for position, (code, value) in enumerate(pairs):
        if code == 9 and value.upper() == "$INSUNITS" and position + 1 < len(pairs):
            try:
                units = int(pairs[position + 1][1])
            except ValueError:
                break
            name, scale = _INSUNITS.get(units, ("mm", 1.0))
            result.units = name
            if units not in _INSUNITS:
                result.warnings.append(f"Unrecognised $INSUNITS value {units}; assuming millimetres")
            return scale
    result.warnings.append("Drawing declares no $INSUNITS; millimetres assumed and must be confirmed")
    return 1.0


def _walk_entities(pairs: list[tuple[int, str]], scale: float, result: DxfExtract) -> None:
    in_entities = False
    entity: str | None = None
    values: dict[int, list[str]] = {}

    def flush() -> None:
        nonlocal entity, values
        if entity:
            _emit(entity, values, scale, result)
        entity, values = None, {}

    for position, (code, value) in enumerate(pairs):
        if code == 2 and value.upper() == "ENTITIES" and position and pairs[position - 1][1].upper() == "SECTION":
            in_entities = True
            continue
        if code == 0 and value.upper() == "ENDSEC" and in_entities:
            flush()
            in_entities = False
            continue
        if not in_entities:
            continue
        if code == 0:
            flush()
            entity = value.upper()
            continue
        values.setdefault(code, []).append(value)
    flush()


def _number(values: dict[int, list[str]], code: int, index: int = 0, default: float = 0.0) -> float:
    try:
        return float(values[code][index])
    except (KeyError, IndexError, ValueError):
        return default


def _emit(entity: str, values: dict[int, list[str]], scale: float, result: DxfExtract) -> None:
    layer = values.get(8, [""])[0]
    if layer:
        result.layers.append(layer)

    if entity == "CIRCLE":
        radius = _number(values, 40) * scale
        if radius <= 0:
            return
        result.circles.append(
            {
                "center": [round(_number(values, 10) * scale, 4), round(_number(values, 20) * scale, 4)],
                "diameter": round(radius * 2.0, 4),
                "layer": layer,
            }
        )
    elif entity == "ARC":
        result.arcs.append(
            {
                "center": [round(_number(values, 10) * scale, 4), round(_number(values, 20) * scale, 4)],
                "radius": round(_number(values, 40) * scale, 4),
                "start_angle": _number(values, 50),
                "end_angle": _number(values, 51),
                "layer": layer,
            }
        )
    elif entity == "LINE":
        result.lines.append(
            {
                "start": [round(_number(values, 10) * scale, 4), round(_number(values, 20) * scale, 4)],
                "end": [round(_number(values, 11) * scale, 4), round(_number(values, 21) * scale, 4)],
                "layer": layer,
            }
        )
    elif entity in ("LWPOLYLINE", "POLYLINE"):
        xs = values.get(10, [])
        ys = values.get(20, [])
        points = [
            [round(float(x) * scale, 4), round(float(y) * scale, 4)]
            for x, y in zip(xs, ys, strict=False)
            if _is_number(x) and _is_number(y)
        ]
        if len(points) >= 2:
            closed = bool(int(_number(values, 70, default=0)) & 1)
            result.polylines.append({"points": points, "closed": closed, "layer": layer})
    elif entity in ("TEXT", "MTEXT"):
        for value in values.get(1, []):
            cleaned = value.strip()
            if cleaned:
                result.texts.append(cleaned)


def _is_number(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


def _compute_bounds(result: DxfExtract) -> None:
    xs: list[float] = []
    ys: list[float] = []
    for circle in result.circles:
        xs += [circle["center"][0] - circle["diameter"] / 2, circle["center"][0] + circle["diameter"] / 2]
        ys += [circle["center"][1] - circle["diameter"] / 2, circle["center"][1] + circle["diameter"] / 2]
    for line in result.lines:
        xs += [line["start"][0], line["end"][0]]
        ys += [line["start"][1], line["end"][1]]
    for polyline in result.polylines:
        xs += [p[0] for p in polyline["points"]]
        ys += [p[1] for p in polyline["points"]]
    for arc in result.arcs:
        xs += [arc["center"][0] - arc["radius"], arc["center"][0] + arc["radius"]]
        ys += [arc["center"][1] - arc["radius"], arc["center"][1] + arc["radius"]]
    if xs and ys:
        result.bbox_min = [min(xs), min(ys)]
        result.bbox_max = [max(xs), max(ys)]


def outline_candidate(extract: DxfExtract) -> dict[str, Any] | None:
    """Largest closed polyline, or the overall extent, as the part outline."""
    closed = [p for p in extract.polylines if p["closed"] and len(p["points"]) >= 3]
    if closed:
        best = max(closed, key=lambda p: _polygon_area(p["points"]))
        return {"shape": "polygon", "points": best["points"], "source": f"closed polyline on layer {best['layer']}"}
    size = [extract.bbox_max[i] - extract.bbox_min[i] for i in range(2)]
    if size[0] <= 0 or size[1] <= 0:
        return None
    center = [(extract.bbox_max[i] + extract.bbox_min[i]) / 2.0 for i in range(2)]
    return {"shape": "rect", "center": center, "size": size, "source": "drawing extent; no closed profile found"}


def _polygon_area(points: list[list[float]]) -> float:
    total = 0.0
    for i in range(len(points)):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def hole_candidates(extract: DxfExtract, top_z: float = 0.0) -> list[dict[str, Any]]:
    grouped: dict[float, list[dict[str, Any]]] = {}
    for circle in extract.circles:
        grouped.setdefault(round(circle["diameter"], 3), []).append(circle)
    out: list[dict[str, Any]] = []
    for diameter, circles in sorted(grouped.items()):
        for circle in circles:
            out.append(
                {
                    "center": circle["center"],
                    "diameter": diameter,
                    "z_reference": top_z,
                    "confidence": 0.85,
                    "method": f"DXF circle on layer {circle['layer'] or 'unnamed'}",
                    "note": "Depth and through-state are not established by a 2D view",
                }
            )
    return out
