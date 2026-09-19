"""Process plans, setups, operations and toolpaths."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import LifecycleStatus, OperationType
from app.db import Base
from app.models.base import IdMixin, JSONDict, TenantMixin, TimestampMixin, VersionMixin


class ManufacturingPlan(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "manufacturing_plans"

    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("part_projects.id", ondelete="CASCADE"), index=True
    )
    geometry_version_id: Mapped[str] = mapped_column(String(32), ForeignKey("geometry_versions.id"))
    machine_version_id: Mapped[str] = mapped_column(String(32), ForeignKey("machine_versions.id"))
    fixture_version_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("fixture_versions.id"), nullable=True
    )
    material_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("materials.id"), nullable=True)

    label: Mapped[str] = mapped_column(String(120), default="")
    objective: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    candidate_rank: Mapped[int] = mapped_column(Integer, default=1)
    objective_score: Mapped[float] = mapped_column(Float, default=0.0)
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)

    stock: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    #: Decision record backing every recommendation in this plan (PRD 10.4).
    decision_record: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)

    status: Mapped[str] = mapped_column(String(24), default=LifecycleStatus.DRAFT)
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    stale_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), default="")

    approved_by_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("users.id"), nullable=True)
    audit: Mapped[list[dict[str, Any]]] = mapped_column(JSONDict, default=list)

    setups: Mapped[list[Setup]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="Setup.sequence"
    )


class MachineFeasibility(IdMixin, TenantMixin, TimestampMixin, Base):
    """Why a registered machine was accepted or rejected (FR-PLN-002).

    Infeasibility always names the exact binding constraint; "not feasible"
    without a cause code is not an acceptable answer.
    """

    __tablename__ = "machine_feasibility"

    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("part_projects.id", ondelete="CASCADE"))
    plan_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("manufacturing_plans.id", ondelete="CASCADE"), nullable=True
    )
    machine_version_id: Mapped[str] = mapped_column(String(32), ForeignKey("machine_versions.id"))
    feasible: Mapped[bool] = mapped_column(Boolean, default=True)
    cause_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    binding_constraint: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    estimated_cycle_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_cost: Mapped[float | None] = mapped_column(Float, nullable=True)


class Setup(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "setups"

    plan_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("manufacturing_plans.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(80), default="")

    work_offset: Mapped[str] = mapped_column(String(8), default="G54")
    #: Rotation of the part into the setup frame, degrees about X, Y, Z.
    orientation_deg: Mapped[list[float]] = mapped_column(JSONDict, default=lambda: [0.0, 0.0, 0.0])
    #: Indexed rotary position for 3+2 setups.
    index_position: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    origin_mm: Mapped[list[float]] = mapped_column(JSONDict, default=lambda: [0.0, 0.0, 0.0])

    datum_scheme: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    fixture_version_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("fixture_versions.id"), nullable=True
    )
    stock_orientation: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    clearance_plane_mm: Mapped[float] = mapped_column(Float, default=25.0)
    setup_minutes: Mapped[float] = mapped_column(Float, default=15.0)

    plan: Mapped[ManufacturingPlan] = relationship(back_populates="setups")
    operations: Mapped[list[Operation]] = relationship(
        back_populates="setup", cascade="all, delete-orphan", order_by="Operation.sequence"
    )


class Operation(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "operations"

    setup_id: Mapped[str] = mapped_column(String(32), ForeignKey("setups.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer, default=1)
    operation_type: Mapped[str] = mapped_column(String(32), default=OperationType.POCKET)
    feature_keys: Mapped[list[str]] = mapped_column(JSONDict, default=list)
    tool_assembly_id: Mapped[str] = mapped_column(String(32), ForeignKey("tool_assembly_versions.id"))

    #: Deterministically derived cutting parameters with the rule that produced
    #: them, so the value is explainable (FR-CAM-002, PRD 10.4).
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    parameter_rationale: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)

    coolant: Mapped[str] = mapped_column(String(24), default="flood")
    suppressed: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    setup: Mapped[Setup] = relationship(back_populates="operations")


class ToolpathVersion(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    """Generated path for one operation, stored as controller-neutral moves."""

    __tablename__ = "toolpath_versions"

    operation_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("operations.id", ondelete="CASCADE"), index=True
    )
    plan_id: Mapped[str] = mapped_column(String(32), ForeignKey("manufacturing_plans.id", ondelete="CASCADE"))
    revision: Mapped[int] = mapped_column(Integer, default=1)

    generator: Mapped[str] = mapped_column(String(64), default="")
    generator_version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    tolerance_mm: Mapped[float] = mapped_column(Float, default=0.01)

    #: Move list. Kept in the object store when large; inline for MVP scale.
    moves: Mapped[list[dict[str, Any]]] = mapped_column(JSONDict, default=list)
    move_count: Mapped[int] = mapped_column(Integer, default=0)
    cutting_length_mm: Mapped[float] = mapped_column(Float, default=0.0)
    rapid_length_mm: Mapped[float] = mapped_column(Float, default=0.0)
    removed_volume_mm3: Mapped[float] = mapped_column(Float, default=0.0)

    status: Mapped[str] = mapped_column(String(24), default=LifecycleStatus.CURRENT)
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
