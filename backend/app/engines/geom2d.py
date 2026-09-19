"""Planar geometry used by the toolpath generators.

Polygons are lists of ``(x, y)`` tuples in counter-clockwise order and are
treated as closed. The offset routine is exact for convex outlines and
acceptable for mildly concave ones; anything else is classified as manual
planning rather than silently approximated (FR-FTR-003).
"""

from __future__ import annotations

import math

Point = tuple[float, float]
Polygon = list[Point]

EPS = 1e-9


def circle_polygon(center: Point, radius: float, segments: int = 72) -> Polygon:
    cx, cy = center
    return [
        (cx + radius * math.cos(2 * math.pi * i / segments), cy + radius * math.sin(2 * math.pi * i / segments))
        for i in range(segments)
    ]


def rect_polygon(center: Point, size: tuple[float, float], corner_radius: float = 0.0, arc_segments: int = 8) -> Polygon:
    """Rectangle, optionally with rounded corners, centred on ``center``."""
    cx, cy = center
    hx, hy = size[0] / 2.0, size[1] / 2.0
    r = max(0.0, min(corner_radius, hx - EPS, hy - EPS))
    if r <= EPS:
        return [(cx - hx, cy - hy), (cx + hx, cy - hy), (cx + hx, cy + hy), (cx - hx, cy + hy)]

    points: Polygon = []
    corners = [
        ((cx + hx - r, cy - hy + r), -math.pi / 2),
        ((cx + hx - r, cy + hy - r), 0.0),
        ((cx - hx + r, cy + hy - r), math.pi / 2),
        ((cx - hx + r, cy - hy + r), math.pi),
    ]
    for (ccx, ccy), start in corners:
        for i in range(arc_segments + 1):
            angle = start + (math.pi / 2) * (i / arc_segments)
            points.append((ccx + r * math.cos(angle), ccy + r * math.sin(angle)))
    return _dedupe(points)


def polygon_from_spec(spec: dict) -> Polygon:
    """Build a polygon from a part-model profile specification."""
    shape = spec.get("shape", spec.get("type", "rect"))
    center = tuple(spec.get("center", (0.0, 0.0)))
    if shape in ("circle", "round"):
        diameter = float(spec.get("diameter", 0.0))
        return circle_polygon(center, diameter / 2.0, int(spec.get("segments", 72)))
    if shape in ("polygon", "profile"):
        return [(float(p[0]), float(p[1])) for p in spec["points"]]
    size = spec.get("size", (10.0, 10.0))
    return rect_polygon(center, (float(size[0]), float(size[1])), float(spec.get("corner_radius", 0.0)))


def _dedupe(points: Polygon) -> Polygon:
    out: Polygon = []
    for p in points:
        if not out or math.dist(p, out[-1]) > 1e-7:
            out.append(p)
    if len(out) > 1 and math.dist(out[0], out[-1]) <= 1e-7:
        out.pop()
    return out


def signed_area(polygon: Polygon) -> float:
    total = 0.0
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def area(polygon: Polygon) -> float:
    return abs(signed_area(polygon))


def ensure_ccw(polygon: Polygon) -> Polygon:
    return polygon if signed_area(polygon) >= 0 else list(reversed(polygon))


def centroid(polygon: Polygon) -> Point:
    a = signed_area(polygon)
    if abs(a) < EPS:
        xs = [p[0] for p in polygon]
        ys = [p[1] for p in polygon]
        return (sum(xs) / len(xs), sum(ys) / len(ys))
    cx = cy = 0.0
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        cross = x1 * y2 - x2 * y1
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    return (cx / (6 * a), cy / (6 * a))


def bounds(polygon: Polygon) -> tuple[float, float, float, float]:
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return min(xs), min(ys), max(xs), max(ys)


def is_convex(polygon: Polygon, tolerance: float = 1e-7) -> bool:
    poly = ensure_ccw(polygon)
    n = len(poly)
    if n < 3:
        return False
    for i in range(n):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % n]
        cx, cy = poly[(i + 2) % n]
        cross = (bx - ax) * (cy - by) - (by - ay) * (cx - bx)
        if cross < -tolerance:
            return False
    return True


def point_in_polygon(point: Point, polygon: Polygon) -> bool:
    x, y = point
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            t = (y - y1) / (y2 - y1)
            if x < x1 + t * (x2 - x1):
                inside = not inside
    return inside


def distance_to_polygon(point: Point, polygon: Polygon) -> float:
    """Unsigned distance from a point to the polygon boundary."""
    best = float("inf")
    n = len(polygon)
    for i in range(n):
        best = min(best, _segment_distance(point, polygon[i], polygon[(i + 1) % n]))
    return best


def _segment_distance(p: Point, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq < EPS:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return math.dist(p, (ax + t * dx, ay + t * dy))


def offset_polygon(polygon: Polygon, delta: float) -> Polygon:
    """Offset a closed polygon by ``delta`` (positive outward, negative inward).

    Each edge is displaced along its outward normal and consecutive edges are
    re-intersected. Vertices that end up on the wrong side of the original
    boundary are dropped, which removes the small self-intersections an inward
    offset creates at tight corners.
    """
    poly = ensure_ccw(_dedupe(polygon))
    n = len(poly)
    if n < 3 or abs(delta) < EPS:
        return list(poly)

    lines: list[tuple[Point, Point]] = []
    for i in range(n):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % n]
        ex, ey = bx - ax, by - ay
        length = math.hypot(ex, ey)
        if length < EPS:
            continue
        # Outward normal of a CCW polygon is (dy, -dx) normalised.
        nx, ny = ey / length, -ex / length
        lines.append(((ax + nx * delta, ay + ny * delta), (bx + nx * delta, by + ny * delta)))

    result: Polygon = []
    count = len(lines)
    for i in range(count):
        p = _line_intersection(lines[i], lines[(i + 1) % count])
        if p is None:
            p = lines[i][1]
        result.append(p)

    result = _dedupe(result)
    if len(result) < 3:
        return []

    if delta < 0:
        keep = [p for p in result if point_in_polygon(p, poly)]
        if len(keep) >= 3:
            result = keep
        if area(result) < EPS or signed_area(result) < 0:
            return []
    return result


def _line_intersection(l1: tuple[Point, Point], l2: tuple[Point, Point]) -> Point | None:
    (x1, y1), (x2, y2) = l1
    (x3, y3), (x4, y4) = l2
    denominator = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denominator) < 1e-12:
        return None
    a = x1 * y2 - y1 * x2
    b = x3 * y4 - y3 * x4
    return ((a * (x3 - x4) - (x1 - x2) * b) / denominator, (a * (y3 - y4) - (y1 - y2) * b) / denominator)


def concentric_offsets(polygon: Polygon, step: float, first_offset: float = 0.0) -> list[Polygon]:
    """Inward offset rings used for pocket roughing, outermost first."""
    rings: list[Polygon] = []
    distance = first_offset
    guard = 0
    while guard < 4096:
        guard += 1
        ring = offset_polygon(polygon, -distance) if distance > EPS else ensure_ccw(list(polygon))
        if len(ring) < 3 or area(ring) < step * step * 0.05:
            break
        rings.append(ring)
        distance += step
    return rings


def polyline_length(points: list[Point]) -> float:
    return sum(math.dist(points[i], points[i + 1]) for i in range(len(points) - 1))


def resample(points: list[Point], max_step: float) -> list[Point]:
    """Insert intermediate points so no segment exceeds ``max_step``."""
    if max_step <= 0 or len(points) < 2:
        return list(points)
    out: list[Point] = [points[0]]
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        distance = math.dist(a, b)
        steps = max(1, int(math.ceil(distance / max_step)))
        for s in range(1, steps + 1):
            t = s / steps
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return out


def zigzag(
    bbox: tuple[float, float, float, float], stepover: float, angle_deg: float = 0.0
) -> list[list[Point]]:
    """Parallel scan lines covering an axis-aligned box."""
    x0, y0, x1, y1 = bbox
    lines: list[list[Point]] = []
    if angle_deg == 90.0:
        x = x0
        flip = False
        while x <= x1 + EPS:
            lines.append([(x, y1), (x, y0)] if flip else [(x, y0), (x, y1)])
            flip = not flip
            x += stepover
    else:
        y = y0
        flip = False
        while y <= y1 + EPS:
            lines.append([(x1, y), (x0, y)] if flip else [(x0, y), (x1, y)])
            flip = not flip
            y += stepover
    return lines


def clip_line_to_polygon(line: list[Point], polygon: Polygon, samples_per_mm: float = 2.0) -> list[list[Point]]:
    """Split a scan line into the spans that lie inside a polygon."""
    length = polyline_length(line)
    if length < EPS:
        return []
    count = max(2, int(length * samples_per_mm))
    spans: list[list[Point]] = []
    current: list[Point] = []
    for i in range(count + 1):
        t = i / count
        p = (line[0][0] + (line[-1][0] - line[0][0]) * t, line[0][1] + (line[-1][1] - line[0][1]) * t)
        if point_in_polygon(p, polygon):
            current.append(p)
        elif current:
            if len(current) > 1:
                spans.append([current[0], current[-1]])
            current = []
    if len(current) > 1:
        spans.append([current[0], current[-1]])
    return spans
