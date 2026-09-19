"""Toolpath generation (FR-CAM-001).

Paths are emitted as controller-neutral moves. Nothing here knows about G-code;
the postprocessor is the only component that does (FR-PST-001).

Move records
------------
``rapid``   non-cutting positioning at machine rapid rate
``linear``  feed move to ``x, y, z`` at ``f`` mm/min
``arc``     feed arc to ``x, y`` around centre offsets ``i, j``
``plunge``  vertical feed entry at the plunge rate
``drill``   canned drilling cycle at ``x, y``
``dwell``   timed pause in seconds
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.engines import geom2d
from app.engines.cutting import CuttingParameters
from app.engines.geom2d import Point, Polygon
from app.engines.partmodel import Grid

GENERATOR_VERSION = "1.0.0"


@dataclass(slots=True)
class PathContext:
    """Everything a generator needs that is not the feature itself."""

    tool_diameter: float
    corner_radius: float = 0.0
    clearance_z: float = 25.0
    retract_z: float = 5.0
    tolerance_mm: float = 0.01
    finish_allowance_mm: float = 0.0
    ramp_angle_deg: float = 3.0
    #: Highest point of the stock in this setup. Entry moves never rapid below
    #: this height, which is what keeps a rapid out of uncut material.
    stock_top_z: float = 0.0

    @property
    def radius(self) -> float:
        return self.tool_diameter / 2.0

    @property
    def safe_entry_z(self) -> float:
        return self.stock_top_z + max(self.retract_z, 1.0)


@dataclass(slots=True)
class Toolpath:
    generator: str
    moves: list[dict[str, Any]] = field(default_factory=list)
    cutting_length_mm: float = 0.0
    rapid_length_mm: float = 0.0
    warnings: list[str] = field(default_factory=list)
    generator_version: str = GENERATOR_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "generator": self.generator,
            "generator_version": self.generator_version,
            "moves": self.moves,
            "move_count": len(self.moves),
            "cutting_length_mm": round(self.cutting_length_mm, 3),
            "rapid_length_mm": round(self.rapid_length_mm, 3),
            "warnings": self.warnings,
        }


class _Builder:
    """Accumulates moves and tracks the tool position and travelled length."""

    def __init__(self, generator: str, ctx: PathContext) -> None:
        self.generator = generator
        self.ctx = ctx
        self.moves: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.cutting = 0.0
        self.rapid = 0.0
        self._pos: tuple[float, float, float] | None = None

    def _advance(self, x: float, y: float, z: float, cutting: bool) -> None:
        if self._pos is not None:
            distance = math.dist(self._pos, (x, y, z))
            if cutting:
                self.cutting += distance
            else:
                self.rapid += distance
        self._pos = (x, y, z)

    def rapid_to(self, x: float, y: float, z: float) -> None:
        self.moves.append({"t": "rapid", "x": round(x, 4), "y": round(y, 4), "z": round(z, 4)})
        self._advance(x, y, z, cutting=False)

    def linear_to(self, x: float, y: float, z: float, feed: float) -> None:
        self.moves.append(
            {"t": "linear", "x": round(x, 4), "y": round(y, 4), "z": round(z, 4), "f": round(feed, 1)}
        )
        self._advance(x, y, z, cutting=True)

    def plunge_to(self, x: float, y: float, z: float, feed: float) -> None:
        self.moves.append(
            {"t": "plunge", "x": round(x, 4), "y": round(y, 4), "z": round(z, 4), "f": round(feed, 1)}
        )
        self._advance(x, y, z, cutting=True)

    def drill(self, x: float, y: float, cycle: dict[str, Any]) -> None:
        self.moves.append({"t": "drill", "x": round(x, 4), "y": round(y, 4), "cycle": cycle})
        z = cycle.get("r_plane", 0.0)
        self._advance(x, y, z, cutting=False)
        depth = abs(cycle.get("r_plane", 0.0) - cycle.get("z_depth", 0.0))
        self.cutting += depth

    def retract(self, z: float | None = None) -> None:
        target = self.ctx.clearance_z if z is None else z
        if self._pos is None:
            return
        self.rapid_to(self._pos[0], self._pos[1], target)

    def finish(self) -> Toolpath:
        return Toolpath(
            generator=self.generator,
            moves=self.moves,
            cutting_length_mm=self.cutting,
            rapid_length_mm=self.rapid,
            warnings=self.warnings,
        )


def _z_levels(z_from: float, z_to: float, step: float) -> list[float]:
    """Descending cut levels from ``z_from`` down to ``z_to`` inclusive."""
    depth = z_from - z_to
    if depth <= 1e-6:
        return [z_to]
    passes = max(1, int(math.ceil(depth / max(step, 1e-6))))
    actual = depth / passes
    return [z_from - actual * (i + 1) for i in range(passes)]


def _entry(builder: _Builder, x: float, y: float, z: float, params: CuttingParameters) -> None:
    """Position above the stock, then feed down to the cut.

    Positioning happens at the clearance plane and the descent stops at the
    stock ceiling before switching to the plunge feed, so no rapid is ever
    commanded inside material.
    """
    ctx = builder.ctx
    builder.rapid_to(x, y, ctx.clearance_z)
    safe = ctx.safe_entry_z
    if safe > z:
        builder.rapid_to(x, y, safe)
    builder.plunge_to(x, y, z, params.plunge_feed_mm_min)


def _link_at_depth(builder: _Builder, x: float, y: float, z: float, params: CuttingParameters) -> None:
    """Move to the next pass without leaving the cut.

    Adjacent passes are at most one stepover apart, so the link is a normal
    cutting move. Retracting and re-plunging between them would be both slower
    and, in uncut material, unsafe.
    """
    builder.linear_to(x, y, z, params.feed_mm_min)


def _cut_ring(builder: _Builder, ring: Polygon, z: float, params: CuttingParameters, linked: bool = False) -> None:
    """Cut one closed ring at ``z``. ``linked`` keeps the tool down at depth."""
    if len(ring) < 2:
        return
    if linked:
        _link_at_depth(builder, ring[0][0], ring[0][1], z, params)
    else:
        _entry(builder, ring[0][0], ring[0][1], z, params)
    for point in ring[1:]:
        builder.linear_to(point[0], point[1], z, params.feed_mm_min)
    builder.linear_to(ring[0][0], ring[0][1], z, params.feed_mm_min)


# --------------------------------------------------------------------------- facing
def facing(
    *,
    region: Polygon,
    z_start: float,
    z_target: float,
    ctx: PathContext,
    params: CuttingParameters,
    angle_deg: float = 0.0,
) -> Toolpath:
    """Zig-zag facing across the stock top down to ``z_target``."""
    builder = _Builder("facing", ctx)
    expanded = geom2d.offset_polygon(region, ctx.radius * 0.9) or region
    x0, y0, x1, y1 = geom2d.bounds(expanded)
    stepover = max(0.5, params.ae_mm)

    for z in _z_levels(z_start, z_target, params.ap_mm):
        first = True
        for line in geom2d.zigzag((x0, y0, x1, y1), stepover, angle_deg):
            if first:
                _entry(builder, line[0][0], line[0][1], z, params)
                first = False
            else:
                _link_at_depth(builder, line[0][0], line[0][1], z, params)
            builder.linear_to(line[-1][0], line[-1][1], z, params.feed_mm_min)
        builder.retract()
    return builder.finish()


# --------------------------------------------------------------------------- contour
def contour(
    *,
    outline: Polygon,
    z_start: float,
    z_target: float,
    ctx: PathContext,
    params: CuttingParameters,
    side: str = "outside",
    climb: bool = True,
) -> Toolpath:
    """Profile the part outline, stepping down at the programmed depth of cut."""
    builder = _Builder("contour", ctx)
    offset = ctx.radius + ctx.finish_allowance_mm
    path = geom2d.offset_polygon(outline, offset if side == "outside" else -offset)
    if len(path) < 3:
        builder.warnings.append("Outline collapsed at the programmed offset; contour not generated")
        return builder.finish()
    if not climb:
        path = list(reversed(path))

    for z in _z_levels(z_start, z_target, params.ap_mm):
        _cut_ring(builder, path, z, params)
        builder.retract()
    return builder.finish()


# --------------------------------------------------------------------------- pocket
def pocket(
    *,
    boundary: Polygon,
    z_start: float,
    floor_z: float,
    ctx: PathContext,
    params: CuttingParameters,
    islands: list[Polygon] | None = None,
) -> Toolpath:
    """Concentric-offset pocketing with a ramped entry on each level."""
    builder = _Builder("pocket", ctx)
    wall_offset = ctx.radius + ctx.finish_allowance_mm
    stepover = max(0.3, params.ae_mm)

    rings = geom2d.concentric_offsets(boundary, stepover, first_offset=wall_offset)
    if not rings:
        builder.warnings.append(
            f"Pocket is narrower than the {ctx.tool_diameter:.2f} mm cutter; select a smaller tool"
        )
        return builder.finish()

    if islands:
        for island in islands:
            grown = geom2d.offset_polygon(island, wall_offset)
            if grown:
                rings = [r for r in rings if not _ring_hits_island(r, grown)]

    for z in _z_levels(z_start, floor_z, params.ap_mm):
        for index, ring in enumerate(rings):
            if index == 0:
                _ramp_entry(builder, ring, z, params, ctx)
            else:
                _cut_ring(builder, ring, z, params, linked=True)
        builder.retract()
    return builder.finish()


def _ring_hits_island(ring: Polygon, island: Polygon) -> bool:
    return any(geom2d.point_in_polygon(p, island) for p in ring)


def _ramp_entry(builder: _Builder, ring: Polygon, z_target: float, params: CuttingParameters, ctx: PathContext) -> None:
    """Enter the cut along the first ring instead of plunging vertically.

    A ramp keeps the axial load on the periphery of the cutter, which is what
    lets a non-centre-cutting endmill enter material safely. If one lap of the
    ring is shorter than the required ramp length the entry continues over
    further laps rather than steepening the ramp.
    """
    if len(ring) < 2:
        return
    z_from = z_target + params.ap_mm
    tangent = math.tan(math.radians(max(0.2, ctx.ramp_angle_deg)))
    ramp_run = params.ap_mm / tangent

    closed = [*ring, ring[0]]
    perimeter = geom2d.polyline_length(closed)
    if perimeter < 1e-6:
        builder.plunge_to(ring[0][0], ring[0][1], z_target, params.plunge_feed_mm_min)
        return

    laps = max(1, int(math.ceil(ramp_run / perimeter)))
    ramp_track: list[Point] = []
    for _ in range(laps):
        ramp_track.extend(closed[1:])
    ramp_track = geom2d.resample([closed[0], *ramp_track], max(0.4, ramp_run / 32.0))

    builder.rapid_to(closed[0][0], closed[0][1], ctx.clearance_z)
    builder.rapid_to(closed[0][0], closed[0][1], max(ctx.safe_entry_z, z_from))
    builder.linear_to(closed[0][0], closed[0][1], z_from, params.plunge_feed_mm_min)

    travelled = 0.0
    previous = ramp_track[0]
    index = 1
    while index < len(ramp_track):
        point = ramp_track[index]
        travelled += math.dist(previous, point)
        previous = point
        index += 1
        fraction = min(1.0, travelled / ramp_run) if ramp_run > 1e-6 else 1.0
        builder.linear_to(point[0], point[1], z_from + (z_target - z_from) * fraction, params.feed_mm_min)
        if fraction >= 1.0:
            break

    # Finish the lap at full depth so the ring is fully cut.
    for point in ring[1:]:
        builder.linear_to(point[0], point[1], z_target, params.feed_mm_min)
    builder.linear_to(ring[0][0], ring[0][1], z_target, params.feed_mm_min)


# --------------------------------------------------------------------------- adaptive
def adaptive_rough(
    *,
    boundary: Polygon,
    z_start: float,
    floor_z: float,
    ctx: PathContext,
    params: CuttingParameters,
    max_engagement_ratio: float = 0.25,
) -> Toolpath:
    """Constant-engagement roughing: deep axially, light radially.

    Radial engagement is capped so the cutter never sees more than
    ``max_engagement_ratio`` of its diameter, which is what allows the full
    flute length to be used in one axial pass.
    """
    builder = _Builder("adaptive_rough", ctx)
    stepover = min(params.ae_mm, max_engagement_ratio * ctx.tool_diameter)
    stepover = max(0.2, stepover)
    wall_offset = ctx.radius + ctx.finish_allowance_mm
    rings = geom2d.concentric_offsets(boundary, stepover, first_offset=wall_offset)
    if not rings:
        builder.warnings.append("Region too small for adaptive roughing with the selected cutter")
        return builder.finish()

    axial = min(params.ap_mm * 3.0, z_start - floor_z)
    for z in _z_levels(z_start, floor_z, max(axial, 0.5)):
        for index, ring in enumerate(reversed(rings)):
            # Innermost ring first: open a slot, then expand outward at constant load.
            if index == 0:
                _ramp_entry(builder, ring, z, params, ctx)
            else:
                _cut_ring(builder, ring, z, params, linked=True)
        builder.retract()
    builder.warnings.append(
        f"Radial engagement capped at {stepover:.2f} mm ({max_engagement_ratio:.0%} of cutter diameter)"
    )
    return builder.finish()


# --------------------------------------------------------------------------- slot
def slotting(
    *,
    start: Point,
    end: Point,
    width: float,
    z_start: float,
    floor_z: float,
    ctx: PathContext,
    params: CuttingParameters,
) -> Toolpath:
    """Slot milling, adding side passes when the slot is wider than the cutter."""
    builder = _Builder("slotting", ctx)
    if ctx.tool_diameter > width + 1e-6:
        builder.warnings.append(
            f"Cutter {ctx.tool_diameter:.2f} mm is wider than the {width:.2f} mm slot; operation not generated"
        )
        return builder.finish()

    lateral = (width - ctx.tool_diameter) / 2.0 - ctx.finish_allowance_mm
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    if length < 1e-6:
        builder.warnings.append("Slot has zero length")
        return builder.finish()
    nx, ny = -dy / length, dx / length

    offsets = [0.0] if lateral <= 1e-6 else [-lateral, lateral]
    for z in _z_levels(z_start, floor_z, params.ap_mm):
        for offset in offsets:
            a = (start[0] + nx * offset, start[1] + ny * offset)
            b = (end[0] + nx * offset, end[1] + ny * offset)
            _entry(builder, a[0], a[1], z, params)
            builder.linear_to(b[0], b[1], z, params.feed_mm_min)
        builder.retract()
    return builder.finish()


# --------------------------------------------------------------------------- holes
def drilling(
    *,
    points: list[Point],
    z_top: float,
    depth: float,
    ctx: PathContext,
    params: CuttingParameters,
    peck: float | None = None,
    dwell_s: float = 0.0,
    cycle: str = "G81",
    pitch_mm: float | None = None,
    through_breakout_mm: float = 1.5,
    point_angle_deg: float = 140.0,
) -> Toolpath:
    """Canned-cycle hole making.

    For a through hole the depth is extended by the drill point length plus a
    break-out allowance, otherwise the cone left by the point would remain.
    """
    builder = _Builder(f"drilling:{cycle}", ctx)
    r_plane = max(z_top, ctx.stock_top_z) + max(1.0, ctx.retract_z * 0.4)
    z_depth = z_top - depth
    body = {
        "type": cycle,
        "r_plane": round(r_plane, 4),
        "z_depth": round(z_depth, 4),
        "feed": round(params.feed_mm_min, 1),
        "rpm": round(params.rpm, 1),
        "dwell_s": dwell_s,
        "point_angle_deg": point_angle_deg,
        "through_breakout_mm": through_breakout_mm,
    }
    if peck:
        body["peck"] = round(peck, 3)
    if pitch_mm:
        body["pitch_mm"] = pitch_mm

    builder.retract()
    for x, y in points:
        builder.drill(x, y, dict(body))
    builder.retract()
    return builder.finish()


def helical_bore(
    *,
    center: Point,
    diameter: float,
    z_start: float,
    floor_z: float,
    ctx: PathContext,
    params: CuttingParameters,
) -> Toolpath:
    """Helical interpolation for bores larger than the available drill."""
    builder = _Builder("helical_bore", ctx)
    path_radius = (diameter - ctx.tool_diameter) / 2.0 - ctx.finish_allowance_mm
    if path_radius <= 0.05:
        builder.warnings.append("Bore diameter is too close to the cutter diameter for helical interpolation")
        return builder.finish()

    pitch = max(0.05, min(params.ap_mm, diameter * 0.05))
    total_depth = z_start - floor_z
    turns = max(1, int(math.ceil(total_depth / pitch)))
    segments_per_turn = 48

    _entry(builder, center[0] + path_radius, center[1], z_start, params)
    for step in range(1, turns * segments_per_turn + 1):
        angle = 2 * math.pi * step / segments_per_turn
        z = max(floor_z, z_start - total_depth * step / (turns * segments_per_turn))
        builder.linear_to(
            center[0] + path_radius * math.cos(angle),
            center[1] + path_radius * math.sin(angle),
            z,
            params.feed_mm_min,
        )
    # One flat finishing revolution at depth so the bore is round at the floor.
    for step in range(segments_per_turn + 1):
        angle = 2 * math.pi * step / segments_per_turn
        builder.linear_to(
            center[0] + path_radius * math.cos(angle),
            center[1] + path_radius * math.sin(angle),
            floor_z,
            params.feed_mm_min,
        )
    builder.retract()
    return builder.finish()


# --------------------------------------------------------------------------- chamfer
def chamfering(
    *,
    edge: Polygon,
    z_top: float,
    width: float,
    ctx: PathContext,
    params: CuttingParameters,
    tool_angle_deg: float = 90.0,
    outside: bool = True,
) -> Toolpath:
    """Single-pass chamfer with the tool positioned by chamfer width."""
    builder = _Builder("chamfering", ctx)
    half_angle = math.radians(tool_angle_deg / 2.0)
    z = z_top - width
    lateral = ctx.radius + width * math.tan(half_angle) - width
    path = geom2d.offset_polygon(edge, lateral if outside else -lateral)
    if len(path) < 3:
        builder.warnings.append("Chamfer path collapsed at the programmed width")
        return builder.finish()
    _cut_ring(builder, path, z, params)
    builder.retract()
    return builder.finish()


# --------------------------------------------------------------------------- rest machining
def rest_machining(
    *,
    stock_z: np.ndarray,
    target_z: np.ndarray,
    grid: Grid,
    ctx: PathContext,
    params: CuttingParameters,
    tolerance_mm: float = 0.05,
    region: Polygon | None = None,
) -> Toolpath:
    """Machine only what the previous operations actually left behind.

    The rest mask comes from the simulated remaining stock, so a smaller tool
    never re-cuts volume a larger tool already removed, and never air-cuts the
    original stock envelope (PRD 13.2, "Stock-aware rest machining").
    """
    builder = _Builder("rest_machining", ctx)
    excess = stock_z - target_z
    rest_mask = excess > tolerance_mm
    if region is not None:
        from app.engines.partmodel import polygon_mask

        rest_mask &= polygon_mask(grid, region)

    if not rest_mask.any():
        builder.warnings.append("No remaining stock above tolerance; rest operation suppressed")
        return builder.finish()

    xs, ys = grid.centers()
    columns = np.where(rest_mask.any(axis=1))[0]
    x_lo, x_hi = xs[columns[0]], xs[columns[-1]]
    rows = np.where(rest_mask.any(axis=0))[0]
    y_lo, y_hi = ys[rows[0]], ys[rows[-1]]

    z_high = float(stock_z[rest_mask].max())
    z_low = float(target_z[rest_mask].min())
    stepover = max(0.3, params.ae_mm)

    for z in _z_levels(z_high, z_low, params.ap_mm):
        cut_any = False
        for line in geom2d.zigzag((x_lo, y_lo, x_hi, y_hi), stepover):
            for span in _mask_spans(line, rest_mask, grid, target_z, z):
                (sx, sy), (ex, ey) = span
                _entry(builder, sx, sy, z, params)
                builder.linear_to(ex, ey, z, params.feed_mm_min)
                builder.retract()
                cut_any = True
        builder.retract()
        if not cut_any:
            break
    return builder.finish()


def _mask_spans(
    line: list[Point], mask: np.ndarray, grid: Grid, target_z: np.ndarray, z_level: float
) -> list[tuple[Point, Point]]:
    """Split a scan line into spans where material remains above ``z_level``."""
    length = geom2d.polyline_length(line)
    if length < 1e-6:
        return []
    samples = max(2, int(length / max(grid.pitch * 0.5, 0.1)))
    spans: list[tuple[Point, Point]] = []
    run: list[Point] = []
    for i in range(samples + 1):
        t = i / samples
        x = line[0][0] + (line[-1][0] - line[0][0]) * t
        y = line[0][1] + (line[-1][1] - line[0][1]) * t
        ix = int((x - grid.x0) / grid.pitch)
        iy = int((y - grid.y0) / grid.pitch)
        inside = 0 <= ix < grid.nx and 0 <= iy < grid.ny
        # Cut here only if material remains and the floor is below this level.
        if inside and mask[ix, iy] and target_z[ix, iy] <= z_level + 1e-6:
            run.append((x, y))
        elif len(run) > 1:
            spans.append((run[0], run[-1]))
            run = []
        else:
            run = []
    if len(run) > 1:
        spans.append((run[0], run[-1]))
    return spans


# --------------------------------------------------------------------------- 3D finishing
def finish_3d(
    *,
    target_z: np.ndarray,
    grid: Grid,
    ctx: PathContext,
    params: CuttingParameters,
    region: Polygon | None = None,
    ball_nose: bool = True,
    angle_deg: float = 0.0,
) -> Toolpath:
    """Parallel raster finishing driven by the target height field.

    The tool centre height at each sample is the highest point the tool body
    would touch inside its footprint, so the cutter follows the surface without
    gouging it. A ball nose is offset by its sphere, a flat end by its face.
    """
    builder = _Builder("finish_3d", ctx)
    contact = _tool_contact_field(target_z, grid, ctx.radius, ctx.corner_radius if ball_nose else 0.0)
    if region is not None:
        from app.engines.partmodel import polygon_mask

        inside = polygon_mask(grid, region)
    else:
        inside = np.ones_like(contact, dtype=bool)

    if not inside.any():
        builder.warnings.append("Finishing region is empty")
        return builder.finish()

    xs, ys = grid.centers()
    stepover = max(0.05, params.ae_mm)
    columns = np.where(inside.any(axis=1))[0]
    rows = np.where(inside.any(axis=0))[0]
    bbox = (xs[columns[0]], ys[rows[0]], xs[columns[-1]], ys[rows[-1]])

    builder.retract()
    for line in geom2d.zigzag(bbox, stepover, angle_deg):
        samples = geom2d.resample(line, max(grid.pitch * 0.5, 0.2))
        cutting = False
        for x, y in samples:
            ix = int((x - grid.x0) / grid.pitch)
            iy = int((y - grid.y0) / grid.pitch)
            if not (0 <= ix < grid.nx and 0 <= iy < grid.ny) or not inside[ix, iy]:
                if cutting:
                    builder.retract()
                    cutting = False
                continue
            z = float(contact[ix, iy]) + ctx.finish_allowance_mm
            if not cutting:
                _entry(builder, x, y, z, params)
                cutting = True
            else:
                builder.linear_to(x, y, z, params.feed_mm_min)
        if cutting:
            builder.retract()
    return builder.finish()


def _tool_contact_field(target_z: np.ndarray, grid: Grid, radius: float, corner_radius: float) -> np.ndarray:
    """Height the tool centre must hold so the tool touches but never cuts below.

    For every offset inside the cutter footprint the required centre height is
    the surface height plus the tool's own profile at that radius; the contact
    height is the maximum over the footprint. This is an exact dilation of the
    surface by the tool profile.
    """
    cells = int(math.ceil(radius / grid.pitch))
    if cells <= 0:
        return target_z.copy()

    contact = np.full_like(target_z, -np.inf)
    nx, ny = target_z.shape
    for dx in range(-cells, cells + 1):
        for dy in range(-cells, cells + 1):
            distance = math.hypot(dx, dy) * grid.pitch
            if distance > radius:
                continue
            if corner_radius > 0:
                reach = min(distance, corner_radius)
                profile = corner_radius - math.sqrt(max(0.0, corner_radius**2 - reach**2))
            else:
                profile = 0.0
            shifted = np.full_like(target_z, -np.inf)
            xs_src = slice(max(0, -dx), nx - max(0, dx))
            xs_dst = slice(max(0, dx), nx - max(0, -dx))
            ys_src = slice(max(0, -dy), ny - max(0, dy))
            ys_dst = slice(max(0, dy), ny - max(0, -dy))
            shifted[xs_dst, ys_dst] = target_z[xs_src, ys_src]
            np.maximum(contact, shifted + profile, out=contact)

    contact[~np.isfinite(contact)] = target_z[~np.isfinite(contact)]
    return contact
