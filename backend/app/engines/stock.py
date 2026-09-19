"""Stock model for simulation and rest machining (FR-CAM-003, FR-SIM-001).

The authoritative stock is a voxel occupancy grid in part coordinates, so
material carries correctly across an indexed 3+2 setup change or a part flip.
Within one setup the tool axis is fixed, so the voxels are projected into a
height field for the setup; cutting updates the height field, and the result is
written back into the voxels when the setup closes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from app.engines.partmodel import Grid


def rotation_matrix(rx_deg: float, ry_deg: float, rz_deg: float) -> np.ndarray:
    """Intrinsic X-Y-Z rotation taking part coordinates into setup coordinates."""
    rx, ry, rz = (math.radians(a) for a in (rx_deg, ry_deg, rz_deg))
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    mx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    my = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    mz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return mz @ my @ mx


@dataclass(slots=True)
class SetupFrame:
    """Placement of the part inside one setup."""

    rotation: np.ndarray
    origin: np.ndarray

    @staticmethod
    def from_spec(orientation_deg: list[float], origin_mm: list[float]) -> SetupFrame:
        return SetupFrame(
            rotation=rotation_matrix(*[float(a) for a in orientation_deg]),
            origin=np.array([float(v) for v in origin_mm], dtype=np.float64),
        )

    def to_setup(self, points: np.ndarray) -> np.ndarray:
        return points @ self.rotation.T + self.origin

    def to_part(self, points: np.ndarray) -> np.ndarray:
        return (points - self.origin) @ self.rotation


class VoxelStock:
    """Occupancy grid over the raw stock block."""

    def __init__(self, lo: list[float], hi: list[float], pitch: float = 1.0) -> None:
        self.pitch = float(pitch)
        self.lo = np.array(lo, dtype=np.float64)
        self.hi = np.array(hi, dtype=np.float64)
        dims = np.maximum(np.ceil((self.hi - self.lo) / self.pitch).astype(int), 1)
        self.shape = (int(dims[0]), int(dims[1]), int(dims[2]))
        self.occupied = np.ones(self.shape, dtype=bool)
        self._centers: np.ndarray | None = None

    # ------------------------------------------------------------------ basics
    @property
    def voxel_volume(self) -> float:
        return self.pitch**3

    def volume(self) -> float:
        return float(self.occupied.sum()) * self.voxel_volume

    def centers(self) -> np.ndarray:
        """(N, 3) array of voxel centre coordinates in part space."""
        if self._centers is None:
            ix, iy, iz = np.indices(self.shape)
            self._centers = np.stack(
                [
                    self.lo[0] + (ix.ravel() + 0.5) * self.pitch,
                    self.lo[1] + (iy.ravel() + 0.5) * self.pitch,
                    self.lo[2] + (iz.ravel() + 0.5) * self.pitch,
                ],
                axis=1,
            )
        return self._centers

    # ------------------------------------------------------- setup projections
    def frame_floor(self, frame: SetupFrame) -> float:
        """Setup-frame height that means "this column is empty".

        It is the underside of the stock block in that frame, so a column that
        has been machined right through reads as material removed down to the
        bottom of the stock rather than as an absurd value. Getting this wrong
        makes a through hole look like a kilometre-deep overcut.
        """
        return float(frame.to_setup(self.centers())[:, 2].min() - self.pitch / 2.0)

    def project(self, frame: SetupFrame, grid: Grid) -> np.ndarray:
        """Height field of the stock top surface as seen along the setup Z axis.

        Columns with no material read as the stock underside, never as stock
        sitting at Z=0 and never as a sentinel that downstream arithmetic would
        propagate.
        """
        setup_points = frame.to_setup(self.centers())
        floor = float(setup_points[:, 2].min() - self.pitch / 2.0)
        height = np.full((grid.nx, grid.ny), floor)

        live = self.occupied.ravel()
        if not live.any():
            return height

        pts = setup_points[live]
        ix = np.floor((pts[:, 0] - grid.x0) / grid.pitch).astype(int)
        iy = np.floor((pts[:, 1] - grid.y0) / grid.pitch).astype(int)
        valid = (ix >= 0) & (ix < grid.nx) & (iy >= 0) & (iy < grid.ny)

        np.maximum.at(height, (ix[valid], iy[valid]), pts[valid, 2] + self.pitch / 2.0)
        return height

    def apply_height_field(self, frame: SetupFrame, grid: Grid, height: np.ndarray) -> float:
        """Remove every voxel that sits above the height field. Returns mm^3 removed."""
        setup_points = frame.to_setup(self.centers())
        ix = np.floor((setup_points[:, 0] - grid.x0) / grid.pitch).astype(int)
        iy = np.floor((setup_points[:, 1] - grid.y0) / grid.pitch).astype(int)
        inside = (ix >= 0) & (ix < grid.nx) & (iy >= 0) & (iy < grid.ny)

        ceiling = np.full(setup_points.shape[0], np.inf)
        ceiling[inside] = height[ix[inside], iy[inside]]
        remove = (setup_points[:, 2] - self.pitch / 2.0) >= ceiling

        flat = self.occupied.ravel()
        removed = int((flat & remove).sum())
        flat[remove] = False
        self.occupied = flat.reshape(self.shape)
        return removed * self.voxel_volume

    def part_height_field(self, grid: Grid) -> np.ndarray:
        """Height field in part coordinates, for comparison against the target."""
        return self.project(SetupFrame(rotation=np.eye(3), origin=np.zeros(3)), grid)


class HeightFieldStock:
    """Mutable stock height field for one setup.

    Every cutting move lowers the surface inside the cutter footprint. The class
    also reports how much material each move actually removed, which is what
    lets a rest operation know there was something there to cut.
    """

    def __init__(self, height: np.ndarray, grid: Grid, floor_z: float) -> None:
        self.height = height.astype(np.float64).copy()
        self.grid = grid
        self.floor_z = floor_z
        self._gx, self._gy = grid.mesh()

    def copy(self) -> HeightFieldStock:
        return HeightFieldStock(self.height, self.grid, self.floor_z)

    def footprint(self, a: tuple[float, float], b: tuple[float, float], radius: float) -> np.ndarray:
        ax, ay = a
        bx, by = b
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        if length_sq < 1e-12:
            return (self._gx - ax) ** 2 + (self._gy - ay) ** 2 <= radius * radius
        t = np.clip(((self._gx - ax) * dx + (self._gy - ay) * dy) / length_sq, 0.0, 1.0)
        px, py = ax + t * dx, ay + t * dy
        return (self._gx - px) ** 2 + (self._gy - py) ** 2 <= radius * radius

    def cut(
        self,
        a: tuple[float, float, float],
        b: tuple[float, float, float],
        radius: float,
        corner_radius: float = 0.0,
    ) -> float:
        """Sweep the cutter from ``a`` to ``b`` and return the volume removed."""
        mask = self.footprint((a[0], a[1]), (b[0], b[1]), radius)
        if not mask.any():
            return 0.0
        tip = min(a[2], b[2])
        if corner_radius > 0:
            # A ball or bull nose does not cut a flat floor at the tip height.
            distance = self._contact_distance((a[0], a[1]), (b[0], b[1]))
            reach = np.clip(radius - distance, 0.0, corner_radius)
            lift = corner_radius - np.sqrt(np.clip(corner_radius**2 - (corner_radius - reach) ** 2, 0.0, None))
            floor = np.where(mask, tip + lift, self.height)
        else:
            floor = np.where(mask, tip, self.height)
        floor = np.maximum(floor, self.floor_z)
        removed_depth = np.clip(self.height - floor, 0.0, None)
        volume = float(removed_depth[mask].sum() * self.grid.cell_area)
        self.height = np.minimum(self.height, floor)
        return volume

    def _contact_distance(self, a: tuple[float, float], b: tuple[float, float]) -> np.ndarray:
        ax, ay = a
        bx, by = b
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        if length_sq < 1e-12:
            return np.sqrt((self._gx - ax) ** 2 + (self._gy - ay) ** 2)
        t = np.clip(((self._gx - ax) * dx + (self._gy - ay) * dy) / length_sq, 0.0, 1.0)
        px, py = ax + t * dx, ay + t * dy
        return np.sqrt((self._gx - px) ** 2 + (self._gy - py) ** 2)

    def max_height_in(self, a: tuple[float, float], b: tuple[float, float], radius: float) -> float:
        mask = self.footprint(a, b, radius)
        if not mask.any():
            return -1e9
        return float(self.height[mask].max())

    def to_summary(self, downsample: int = 2) -> dict[str, Any]:
        """Compact payload for the web viewer."""
        step = max(1, downsample)
        sampled = self.height[::step, ::step]
        return {
            "grid": {**self.grid.to_dict(), "downsample": step},
            "min_z": float(sampled.min()),
            "max_z": float(sampled.max()),
            "height": [[round(float(v), 3) for v in row] for row in sampled],
        }


def compare_to_target(
    machined: np.ndarray, target: np.ndarray, grid: Grid, tolerance_mm: float = 0.05
) -> dict[str, Any]:
    """Overcut and undercut against the finished-part surface (FR-SIM-002)."""
    delta = machined - target
    # Negative delta means the machined surface sits below the finished part.
    overcut = np.clip(-delta - tolerance_mm, 0.0, None)
    excess = np.clip(delta - tolerance_mm, 0.0, None)

    overcut_cells = int((overcut > 0).sum())
    excess_cells = int((excess > 0).sum())
    return {
        "tolerance_mm": tolerance_mm,
        "overcut_cells": overcut_cells,
        "overcut_max_mm": float(overcut.max()) if overcut_cells else 0.0,
        "overcut_volume_mm3": float(overcut.sum() * grid.cell_area),
        "remaining_cells": excess_cells,
        "remaining_max_mm": float(excess.max()) if excess_cells else 0.0,
        "remaining_volume_mm3": float(excess.sum() * grid.cell_area),
        "conformant": overcut_cells == 0 and excess_cells == 0,
    }


def locate_worst(field: np.ndarray, grid: Grid) -> tuple[float, float, float]:
    """XY position and magnitude of the largest deviation, for event reporting."""
    index = int(np.argmax(field))
    ix, iy = np.unravel_index(index, field.shape)
    xs, ys = grid.centers()
    return float(xs[ix]), float(ys[iy]), float(field[ix, iy])
