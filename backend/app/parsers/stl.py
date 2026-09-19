"""STL mesh reading.

Gives an exact triangle count, bounding box, surface area and closed-volume
estimate. The volume is the signed tetrahedron sum, which is correct for a
closed manifold and meaningless for an open one - so the reader reports whether
the mesh is closed rather than quoting a volume it cannot stand behind.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import Any

MAX_TRIANGLES = 4_000_000


@dataclass
class StlExtract:
    format: str = "binary"
    triangle_count: int = 0
    bbox_min: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    bbox_max: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    surface_area_mm2: float = 0.0
    signed_volume_mm3: float = 0.0
    closed: bool = False
    degenerate_triangles: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "triangle_count": self.triangle_count,
            "bbox_min_mm": [round(v, 4) for v in self.bbox_min],
            "bbox_max_mm": [round(v, 4) for v in self.bbox_max],
            "size_mm": [round(self.bbox_max[i] - self.bbox_min[i], 4) for i in range(3)],
            "surface_area_mm2": round(self.surface_area_mm2, 3),
            "volume_mm3": round(abs(self.signed_volume_mm3), 3) if self.closed else None,
            "closed": self.closed,
            "degenerate_triangles": self.degenerate_triangles,
            "warnings": self.warnings,
        }


def parse(data: bytes) -> StlExtract:
    if data[:5].lstrip().lower().startswith(b"solid") and b"facet normal" in data[:4096].lower():
        return _parse_ascii(data)
    return _parse_binary(data)


def _accumulate(result: StlExtract, triangles: list[tuple[tuple[float, float, float], ...]]) -> None:
    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    area = 0.0
    volume = 0.0
    edges: dict[tuple, int] = {}

    for a, b, c in triangles:
        for vertex in (a, b, c):
            for i in range(3):
                lo[i] = min(lo[i], vertex[i])
                hi[i] = max(hi[i], vertex[i])
        ab = tuple(b[i] - a[i] for i in range(3))
        ac = tuple(c[i] - a[i] for i in range(3))
        cross = (
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        )
        magnitude = math.sqrt(sum(v * v for v in cross))
        if magnitude < 1e-12:
            result.degenerate_triangles += 1
            continue
        area += magnitude / 2.0
        volume += (
            a[0] * (b[1] * c[2] - b[2] * c[1])
            - a[1] * (b[0] * c[2] - b[2] * c[0])
            + a[2] * (b[0] * c[1] - b[1] * c[0])
        ) / 6.0
        for start, end in ((a, b), (b, c), (c, a)):
            key = tuple(sorted((tuple(round(v, 5) for v in start), tuple(round(v, 5) for v in end))))
            edges[key] = edges.get(key, 0) + 1

    result.triangle_count = len(triangles)
    result.surface_area_mm2 = area
    result.signed_volume_mm3 = volume
    if triangles:
        result.bbox_min = lo
        result.bbox_max = hi
    open_edges = sum(1 for count in edges.values() if count != 2)
    result.closed = open_edges == 0 and bool(triangles)
    if not result.closed and triangles:
        result.warnings.append(
            f"Mesh is not closed ({open_edges} boundary edges); no volume is reported and stock cannot be derived from it"
        )
    if result.degenerate_triangles:
        result.warnings.append(f"{result.degenerate_triangles} degenerate triangles were skipped")


def _parse_binary(data: bytes) -> StlExtract:
    result = StlExtract(format="binary")
    if len(data) < 84:
        result.warnings.append("File is too short to be a binary STL")
        return result
    count = struct.unpack("<I", data[80:84])[0]
    expected = 84 + count * 50
    if count > MAX_TRIANGLES:
        result.warnings.append(f"Triangle count {count} exceeds the {MAX_TRIANGLES} parser limit")
        return result
    if len(data) < expected:
        result.warnings.append(
            f"Declared {count} triangles but the file holds {(len(data) - 84) // 50}; reading what is present"
        )
        count = max(0, (len(data) - 84) // 50)

    triangles = []
    offset = 84
    for _ in range(count):
        values = struct.unpack_from("<12fH", data, offset)
        offset += 50
        triangles.append(((values[3], values[4], values[5]), (values[6], values[7], values[8]), (values[9], values[10], values[11])))
    _accumulate(result, triangles)
    return result


def _parse_ascii(data: bytes) -> StlExtract:
    result = StlExtract(format="ascii")
    triangles = []
    current: list[tuple[float, float, float]] = []
    for raw in data.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line.startswith("vertex"):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            current.append((float(parts[1]), float(parts[2]), float(parts[3])))
        except ValueError:
            continue
        if len(current) == 3:
            triangles.append(tuple(current))
            current = []
            if len(triangles) > MAX_TRIANGLES:
                result.warnings.append(f"Stopped after {MAX_TRIANGLES} triangles")
                break
    _accumulate(result, triangles)
    return result
