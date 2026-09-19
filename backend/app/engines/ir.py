"""Manufacturing intermediate representation (FR-PST-001, PRD 8.1).

The IR is the controller-neutral contract between verified planning and
postprocessing. It carries explicit coordinate systems, units, stock, setup,
machine, tool assemblies, operations, paths, feeds, speeds, coolant, safe
planes, work offsets, tolerances, simulation identity and provenance.

It deliberately contains no free-text motion instructions. Everything a post
needs is a typed field, because a post that has to interpret prose is a post
that can emit motion nobody verified.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.core.hashing import sha256_json

IR_SCHEMA_VERSION = "1.0.0"


class Provenance(BaseModel):
    """Exactly which verified inputs this program was built from."""

    project_id: str
    part_number: str
    revision: str
    geometry_version_id: str
    geometry_hash: str
    plan_id: str
    plan_hash: str
    simulation_run_id: str
    simulation_identity_hash: str
    generated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    generator: str = f"mip-ir/{IR_SCHEMA_VERSION}"


class MachineRef(BaseModel):
    code: str
    manufacturer: str = ""
    model: str = ""
    controller: str = "FANUC"
    controller_version: str = ""
    kinematics: str = "3axis"
    travels_mm: dict[str, list[float]] = Field(default_factory=dict)
    max_rpm: float = 12000.0
    max_feed_mm_min: float = 10000.0
    configuration_hash: str = ""


class ToolRef(BaseModel):
    number: int
    code: str
    description: str = ""
    diameter_mm: float
    flutes: int = 2
    flute_length_mm: float = 0.0
    corner_radius_mm: float = 0.0
    gauge_length_mm: float = 0.0
    length_offset: int = 0
    diameter_offset: int = 0
    max_rpm: float = 12000.0
    assembly_hash: str = ""


class CoordinateSystem(BaseModel):
    name: str = "G54"
    origin_mm: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    orientation_deg: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    description: str = ""


class Stock(BaseModel):
    type: Literal["block", "cylinder"] = "block"
    min_mm: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    max_mm: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    material_code: str = ""
    material_name: str = ""


class Move(BaseModel):
    t: Literal["rapid", "linear", "plunge", "arc", "drill", "dwell"]
    x: float | None = None
    y: float | None = None
    z: float | None = None
    f: float | None = None
    i: float | None = None
    j: float | None = None
    cw: bool | None = None
    seconds: float | None = None
    cycle: dict[str, Any] | None = None


class IROperation(BaseModel):
    id: str
    sequence: int
    type: str
    label: str = ""
    feature_keys: list[str] = Field(default_factory=list)
    tool_number: int
    spindle_rpm: float
    spindle_direction: Literal["cw", "ccw"] = "cw"
    feed_mm_min: float
    plunge_feed_mm_min: float
    coolant: Literal["flood", "mist", "air", "through", "off"] = "flood"
    tolerance_mm: float = 0.01
    stock_to_leave_mm: float = 0.0
    moves: list[Move] = Field(default_factory=list)

    @field_validator("spindle_rpm", "feed_mm_min")
    @classmethod
    def _positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("Spindle speed and feed must be positive in released IR")
        return value


class IRSetup(BaseModel):
    id: str
    sequence: int
    name: str = ""
    work_offset: str = "G54"
    clearance_plane_mm: float = 25.0
    retract_plane_mm: float = 5.0
    orientation_deg: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    index_position: dict[str, float] = Field(default_factory=dict)
    datum_note: str = ""
    operations: list[IROperation] = Field(default_factory=list)


class ManufacturingIR(BaseModel):
    schema_version: str = IR_SCHEMA_VERSION
    units: Literal["mm", "inch"] = "mm"
    provenance: Provenance
    machine: MachineRef
    stock: Stock
    coordinate_systems: list[CoordinateSystem] = Field(default_factory=list)
    tools: list[ToolRef] = Field(default_factory=list)
    setups: list[IRSetup] = Field(default_factory=list)
    #: Cycle time and event summary from the simulation this IR was cleared by.
    verification: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    def content_hash(self) -> str:
        return sha256_json(self.model_dump(mode="json"))

    def tool_by_number(self, number: int) -> ToolRef | None:
        return next((t for t in self.tools if t.number == number), None)

    def total_moves(self) -> int:
        return sum(len(op.moves) for setup in self.setups for op in setup.operations)
