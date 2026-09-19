"""The canonical part model.

A part is a stock envelope plus parametric features expressed in the part
coordinate system, with Z up and Z=0 at the finished top face unless stated
otherwise. This is the engineering representation that planning, CAM and
verification all read; the triangle mesh is only a visual derivative (PRD 10.1).

The model is deliberately narrow. It covers the prismatic and 2.5D geometry in
the MVP support matrix (Appendix A) and exposes anything outside it as a
manual-planning feature rather than approximating it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.core.enums import FeatureSupport, FeatureType
from app.engines import geom2d
from app.engines.geom2d import Polygon

#: Feature types the generators can plan without engineering intervention.
SUPPORTED_TYPES = {
    FeatureType.FACE,
    FeatureType.POCKET,
    FeatureType.SLOT,
    FeatureType.HOLE,
    FeatureType.COUNTERBORE,
    FeatureType.COUNTERSINK,
    FeatureType.STEP,
    FeatureType.CHAMFER,
}
#: Recognised but requiring a confirmed attribute before an operation is chosen.
PARTIAL_TYPES = {FeatureType.THREAD_CANDIDATE, FeatureType.BOSS, FeatureType.ISLAND, FeatureType.FILLET}


@dataclass(slots=True)
class Feature:
    key: str
    feature_type: str
    params: dict[str, Any] = field(default_factory=dict)
    label: str = ""
    access: tuple[float, float, float] = (0.0, 0.0, 1.0)
    criticality: str = "Noncritical"
    tolerance: dict[str, Any] = field(default_factory=dict)
    thread_spec: str | None = None
    surface_finish_ra: float | None = None
    confidence: float = 0.0
    status: str = "Inferred"

    @property
    def support(self) -> str:
        try:
            ftype = FeatureType(self.feature_type)
        except ValueError:
            return FeatureSupport.MANUAL
        if ftype in SUPPORTED_TYPES:
            return FeatureSupport.SUPPORTED
        if ftype in PARTIAL_TYPES:
            return FeatureSupport.PARTIAL
        return FeatureSupport.MANUAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "type": self.feature_type,
            "label": self.label,
            "params": self.params,
            "access": list(self.access),
            "criticality": self.criticality,
            "tolerance": self.tolerance,
            "thread_spec": self.thread_spec,
            "surface_finish_ra": self.surface_finish_ra,
            "confidence": self.confidence,
            "status": self.status,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> Feature:
        access = data.get("access") or [0.0, 0.0, 1.0]
        return Feature(
            key=data["key"],
            feature_type=data.get("type", FeatureType.POCKET),
            params=dict(data.get("params", {})),
            label=data.get("label", ""),
            access=(float(access[0]), float(access[1]), float(access[2])),
            criticality=data.get("criticality", "Noncritical"),
            tolerance=dict(data.get("tolerance", {})),
            thread_spec=data.get("thread_spec"),
            surface_finish_ra=data.get("surface_finish_ra"),
            confidence=float(data.get("confidence", 0.0)),
            status=data.get("status", "Inferred"),
        )


@dataclass(slots=True)
class PartModel:
    """Finished part geometry plus the stock it is cut from."""

    units: str = "mm"
    #: Finished part outline in XY. Everything outside it is stock to remove.
    outline: dict[str, Any] = field(default_factory=lambda: {"shape": "rect", "center": [0, 0], "size": [100, 60]})
    #: Finished part height measured from ``z_bottom`` to ``z_top``.
    z_top: float = 0.0
    z_bottom: float = -25.0
    #: Raw stock block: size and where the part sits inside it.
    stock: dict[str, Any] = field(default_factory=dict)
    features: list[Feature] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ views
    def outline_polygon(self) -> Polygon:
        return geom2d.ensure_ccw(geom2d.polygon_from_spec(self.outline))

    @property
    def part_height(self) -> float:
        return self.z_top - self.z_bottom

    def stock_block(self) -> dict[str, Any]:
        """Stock envelope, derived from the outline when not stated explicitly."""
        if self.stock:
            return self.stock
        x0, y0, x1, y1 = geom2d.bounds(self.outline_polygon())
        margin = 2.0
        return {
            "type": "block",
            "min": [x0 - margin, y0 - margin, self.z_bottom],
            "max": [x1 + margin, y1 + margin, self.z_top + margin],
        }

    def bbox(self) -> tuple[list[float], list[float]]:
        block = self.stock_block()
        return list(block["min"]), list(block["max"])

    def feature(self, key: str) -> Feature | None:
        return next((f for f in self.features if f.key == key), None)

    def features_of(self, *types: str) -> list[Feature]:
        wanted = set(types)
        return [f for f in self.features if f.feature_type in wanted]

    # ------------------------------------------------------- serialisation
    def to_dict(self) -> dict[str, Any]:
        return {
            "units": self.units,
            "outline": self.outline,
            "z_top": self.z_top,
            "z_bottom": self.z_bottom,
            "stock": self.stock_block(),
            "features": [f.to_dict() for f in self.features],
            "notes": self.notes,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> PartModel:
        return PartModel(
            units=data.get("units", "mm"),
            outline=dict(data.get("outline", {"shape": "rect", "center": [0, 0], "size": [100, 60]})),
            z_top=float(data.get("z_top", 0.0)),
            z_bottom=float(data.get("z_bottom", -25.0)),
            stock=dict(data.get("stock", {})),
            features=[Feature.from_dict(f) for f in data.get("features", [])],
            notes=list(data.get("notes", [])),
        )


# --------------------------------------------------------------------------- grids
@dataclass(slots=True)
class Grid:
    """Regular XY sampling grid shared by the stock and target height fields."""

    x0: float
    y0: float
    nx: int
    ny: int
    pitch: float

    @property
    def x1(self) -> float:
        return self.x0 + self.nx * self.pitch

    @property
    def y1(self) -> float:
        return self.y0 + self.ny * self.pitch

    def centers(self) -> tuple[np.ndarray, np.ndarray]:
        xs = self.x0 + (np.arange(self.nx) + 0.5) * self.pitch
        ys = self.y0 + (np.arange(self.ny) + 0.5) * self.pitch
        return xs, ys

    def mesh(self) -> tuple[np.ndarray, np.ndarray]:
        xs, ys = self.centers()
        return np.meshgrid(xs, ys, indexing="ij")

    @property
    def cell_area(self) -> float:
        return self.pitch * self.pitch

    def to_dict(self) -> dict[str, Any]:
        return {"x0": self.x0, "y0": self.y0, "nx": self.nx, "ny": self.ny, "pitch": self.pitch}


def grid_for(model: PartModel, pitch: float = 1.0) -> Grid:
    lo, hi = model.bbox()
    nx = max(1, int(math.ceil((hi[0] - lo[0]) / pitch)))
    ny = max(1, int(math.ceil((hi[1] - lo[1]) / pitch)))
    return Grid(x0=lo[0], y0=lo[1], nx=nx, ny=ny, pitch=pitch)


def polygon_mask(grid: Grid, polygon: Polygon) -> np.ndarray:
    """Boolean mask of grid cells whose centre lies inside ``polygon``.

    Uses a vectorised crossing-number test so simulation grids stay fast at the
    1 mm pitch the verification engine defaults to.
    """
    gx, gy = grid.mesh()
    inside = np.zeros(gx.shape, dtype=bool)
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if y1 == y2:
            continue
        straddles = (y1 > gy) != (y2 > gy)
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (gy - y1) / (y2 - y1)
            x_cross = x1 + t * (x2 - x1)
        inside ^= straddles & (gx < x_cross)
    return inside


def circle_mask(grid: Grid, center: tuple[float, float], radius: float) -> np.ndarray:
    gx, gy = grid.mesh()
    return (gx - center[0]) ** 2 + (gy - center[1]) ** 2 <= radius * radius


def capsule_mask(grid: Grid, a: tuple[float, float], b: tuple[float, float], radius: float) -> np.ndarray:
    """Mask of a swept circle between two points - the footprint of a cut move."""
    gx, gy = grid.mesh()
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-12:
        return circle_mask(grid, a, radius)
    t = np.clip(((gx - ax) * dx + (gy - ay) * dy) / length_sq, 0.0, 1.0)
    px, py = ax + t * dx, ay + t * dy
    return (gx - px) ** 2 + (gy - py) ** 2 <= radius * radius


# --------------------------------------------------------------- height fields
def target_heightfield(model: PartModel, grid: Grid) -> np.ndarray:
    """Finished-part height z(x, y) over the grid.

    Cells outside the part outline drop to the bottom of the stock because that
    material is removed by profiling. Pockets, slots, holes and steps lower the
    surface; bosses raise it. The result is the surface the simulation compares
    the machined stock against to find overcut and undercut (FR-SIM-002).
    """
    lo, _ = model.bbox()
    field_z = np.full((grid.nx, grid.ny), model.z_top, dtype=np.float64)

    outline = model.outline_polygon()
    inside_outline = polygon_mask(grid, outline)
    field_z[~inside_outline] = lo[2]

    for feature in model.features:
        ftype = feature.feature_type
        params = feature.params
        if ftype == FeatureType.FACE:
            z = float(params.get("z", model.z_top))
            field_z[inside_outline] = np.minimum(field_z[inside_outline], z)
        elif ftype in (FeatureType.POCKET, FeatureType.STEP):
            mask = polygon_mask(grid, _feature_polygon(feature))
            field_z[mask] = np.minimum(field_z[mask], _floor_z(model, feature))
        elif ftype == FeatureType.SLOT:
            a = tuple(params["start"])
            b = tuple(params["end"])
            mask = capsule_mask(grid, a, b, float(params["width"]) / 2.0)
            field_z[mask] = np.minimum(field_z[mask], _floor_z(model, feature))
        elif ftype in (FeatureType.HOLE, FeatureType.THREAD_CANDIDATE):
            center = tuple(params["center"])
            mask = circle_mask(grid, center, float(params["diameter"]) / 2.0)
            field_z[mask] = np.minimum(field_z[mask], _floor_z(model, feature))
        elif ftype in (FeatureType.COUNTERBORE, FeatureType.COUNTERSINK):
            center = tuple(params["center"])
            mask = circle_mask(grid, center, float(params["diameter"]) / 2.0)
            field_z[mask] = np.minimum(field_z[mask], _floor_z(model, feature))
            pilot = params.get("pilot_diameter")
            if pilot:
                pilot_mask = circle_mask(grid, center, float(pilot) / 2.0)
                field_z[pilot_mask] = np.minimum(field_z[pilot_mask], _floor_z(model, feature, pilot=True))
        elif ftype in (FeatureType.BOSS, FeatureType.ISLAND):
            mask = polygon_mask(grid, _feature_polygon(feature))
            top = float(params.get("top_z", model.z_top))
            field_z[mask] = np.maximum(field_z[mask], top)

    return field_z


def _floor_z(model: PartModel, feature: Feature, pilot: bool = False) -> float:
    params = feature.params
    lo, _ = model.bbox()
    if params.get("through"):
        return lo[2]
    top = float(params.get("top_z", model.z_top))
    if pilot:
        depth = float(params.get("pilot_depth", params.get("depth", 0.0)))
        return top - depth
    if "floor_z" in params:
        return float(params["floor_z"])
    return top - float(params.get("depth", 0.0))


def _feature_polygon(feature: Feature) -> Polygon:
    params = feature.params
    spec = {
        "shape": params.get("shape", "rect"),
        "center": params.get("center", [0.0, 0.0]),
        "size": params.get("size", [10.0, 10.0]),
        "corner_radius": params.get("corner_radius", 0.0),
        "diameter": params.get("diameter", 10.0),
        "points": params.get("points"),
    }
    if spec["points"]:
        spec["shape"] = "polygon"
    return geom2d.ensure_ccw(geom2d.polygon_from_spec(spec))


def feature_polygon(feature: Feature) -> Polygon:
    """Public accessor used by the toolpath generators."""
    return _feature_polygon(feature)


def feature_floor_z(model: PartModel, feature: Feature) -> float:
    return _floor_z(model, feature)


def stock_heightfield(model: PartModel, grid: Grid) -> np.ndarray:
    block = model.stock_block()
    return np.full((grid.nx, grid.ny), float(block["max"][2]), dtype=np.float64)


def removal_volume(stock: np.ndarray, target: np.ndarray, grid: Grid) -> float:
    """Material to remove, in mm^3."""
    return float(np.clip(stock - target, 0.0, None).sum() * grid.cell_area)


# ------------------------------------------------------------ voxel occupancy
def target_occupancy(model: PartModel, lo: list[float], hi: list[float], pitch: float) -> np.ndarray:
    """Voxels the finished part must still occupy.

    The height field above is single valued, so it describes the part only as
    seen from +Z. This occupancy grid does not have that limitation, which is
    what makes conformance meaningful once a part is flipped or indexed and
    material is removed from more than one direction.
    """
    lo_a = np.array(lo, dtype=np.float64)
    hi_a = np.array(hi, dtype=np.float64)
    dims = np.maximum(np.ceil((hi_a - lo_a) / pitch).astype(int), 1)
    nx, ny, nz = (int(dims[0]), int(dims[1]), int(dims[2]))

    grid = Grid(x0=lo_a[0], y0=lo_a[1], nx=nx, ny=ny, pitch=pitch)
    zs = lo_a[2] + (np.arange(nz) + 0.5) * pitch

    inside_outline = polygon_mask(grid, model.outline_polygon())
    within_height = (zs > model.z_bottom - 1e-9) & (zs < model.z_top + 1e-9)
    occupancy = inside_outline[:, :, None] & within_height[None, None, :]

    for feature in model.features:
        ftype = feature.feature_type
        params = feature.params

        if ftype == FeatureType.FACE:
            cut_top = float(params.get("z", model.z_top))
            occupancy[:, :, zs > cut_top] = False
            continue

        if ftype in (FeatureType.POCKET, FeatureType.STEP):
            mask = polygon_mask(grid, _feature_polygon(feature))
        elif ftype == FeatureType.SLOT:
            mask = capsule_mask(grid, tuple(params["start"]), tuple(params["end"]), float(params["width"]) / 2.0)
        elif ftype in (
            FeatureType.HOLE,
            FeatureType.THREAD_CANDIDATE,
            FeatureType.COUNTERBORE,
            FeatureType.COUNTERSINK,
        ):
            mask = circle_mask(grid, tuple(params["center"]), float(params["diameter"]) / 2.0)
        elif ftype in (FeatureType.BOSS, FeatureType.ISLAND):
            mask = polygon_mask(grid, _feature_polygon(feature))
            top = float(params.get("top_z", model.z_top))
            span = (zs > model.z_bottom - 1e-9) & (zs < top + 1e-9)
            occupancy |= mask[:, :, None] & span[None, None, :]
            continue
        else:
            continue

        floor = _floor_z(model, feature)
        top = float(params.get("top_z", model.z_top))
        access = feature.access
        if access[2] < 0:
            # Machined from below: the cut runs from the feature's reference
            # face upward, not downward.
            span = (zs > top - 1e-9) & (zs < floor + 1e-9) if floor > top else (zs > floor - 1e-9) & (zs < top + 1e-9)
        else:
            span = (zs > floor - 1e-9) & (zs < top + 1e-9)
        occupancy &= ~(mask[:, :, None] & span[None, None, :])

    return occupancy


def compare_occupancy(machined: np.ndarray, target: np.ndarray, pitch: float) -> dict[str, Any]:
    """Volume conformance between machined stock and the finished part."""
    voxel_volume = pitch**3
    missing = target & ~machined
    extra = machined & ~target
    missing_count = int(missing.sum())
    extra_count = int(extra.sum())
    return {
        "voxel_pitch_mm": pitch,
        "target_volume_mm3": round(float(target.sum()) * voxel_volume, 2),
        "machined_volume_mm3": round(float(machined.sum()) * voxel_volume, 2),
        "overcut_voxels": missing_count,
        "overcut_volume_mm3": round(missing_count * voxel_volume, 2),
        "remaining_voxels": extra_count,
        "remaining_volume_mm3": round(extra_count * voxel_volume, 2),
        "conformant": missing_count == 0 and extra_count == 0,
    }


def worst_occupancy_location(mask: np.ndarray, lo: list[float], pitch: float) -> list[float] | None:
    """Centre of the largest contiguous run of flagged voxels, for reporting."""
    if not mask.any():
        return None
    indices = np.argwhere(mask)
    centre = indices.mean(axis=0)
    return [round(float(lo[i] + (centre[i] + 0.5) * pitch), 3) for i in range(3)]
