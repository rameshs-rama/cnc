"""Simulation runs, cost estimates, NC programs and releases."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import LifecycleStatus, ReleaseStatus, Severity
from app.db import Base
from app.models.base import IdMixin, JSONDict, TenantMixin, TimestampMixin, VersionMixin


class SimulationRun(IdMixin, TenantMixin, TimestampMixin, Base):
    """Stock removal plus full machine verification.

    Simulation identity is the tuple of engine version, numeric tolerances and
    the hashes of machine, tool, fixture, stock and toolpaths. Change any of
    them and the previous result no longer applies (PRD 8.2).
    """

    __tablename__ = "simulation_runs"

    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("part_projects.id", ondelete="CASCADE"))
    plan_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("manufacturing_plans.id", ondelete="CASCADE"), index=True
    )

    engine_version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    identity_hash: Mapped[str] = mapped_column(String(64), index=True, default="")
    input_hashes: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    voxel_size_mm: Mapped[float] = mapped_column(Float, default=1.0)

    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    events: Mapped[list[dict[str, Any]]] = mapped_column(JSONDict, default=list)
    event_counts: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    max_severity: Mapped[str | None] = mapped_column(String(8), nullable=True)

    #: Cycle time decomposition: cutting, rapid, dwell, tool change, spindle,
    #: indexing (FR-SIM-003).
    cycle_time_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    time_breakdown: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)

    remaining_stock: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    stock_comparison: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    programmed_envelope: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)

    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    dispositions: Mapped[list[dict[str, Any]]] = mapped_column(JSONDict, default=list)


class CostEstimate(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "cost_estimates"

    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("part_projects.id", ondelete="CASCADE"))
    plan_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("manufacturing_plans.id", ondelete="CASCADE"), index=True
    )
    simulation_run_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("simulation_runs.id"), nullable=True
    )

    currency: Mapped[str] = mapped_column(String(8), default="EUR")
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    rates: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    assumptions: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    breakdown: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    unit_price: Mapped[float] = mapped_column(Float, default=0.0)
    quantity_breaks: Mapped[list[dict[str, Any]]] = mapped_column(JSONDict, default=list)
    sensitivity: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    stale: Mapped[bool] = mapped_column(Boolean, default=False)


class NCProgram(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "nc_programs"

    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("part_projects.id", ondelete="CASCADE"))
    plan_id: Mapped[str] = mapped_column(String(32), ForeignKey("manufacturing_plans.id", ondelete="CASCADE"))
    simulation_run_id: Mapped[str] = mapped_column(String(32), ForeignKey("simulation_runs.id"))
    post_version_id: Mapped[str] = mapped_column(String(32), ForeignKey("postprocessor_versions.id"))
    machine_version_id: Mapped[str] = mapped_column(String(32), ForeignKey("machine_versions.id"))

    program_number: Mapped[str] = mapped_column(String(16), default="O0001")
    setup_sequence: Mapped[int] = mapped_column(Integer, default=1)
    storage_key: Mapped[str] = mapped_column(String(255), default="")
    program_hash: Mapped[str] = mapped_column(String(64), default="")
    line_count: Mapped[int] = mapped_column(Integer, default=0)

    #: Controller-neutral IR this program was produced from (FR-PST-001).
    ir_hash: Mapped[str] = mapped_column(String(64), default="")
    ir_storage_key: Mapped[str] = mapped_column(String(255), default="")

    validations: Mapped[list[dict[str, Any]]] = mapped_column(JSONDict, default=list)
    validation_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    max_severity: Mapped[str | None] = mapped_column(String(8), nullable=True)
    programmed_envelope: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)

    status: Mapped[str] = mapped_column(String(24), default=LifecycleStatus.DRAFT)
    stale: Mapped[bool] = mapped_column(Boolean, default=False)


class Release(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    """Immutable release package (FR-REL-002).

    A released program is never edited. A change produces a new candidate and a
    new release revision; the prior release becomes superseded, not deleted.
    """

    __tablename__ = "releases"

    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("part_projects.id", ondelete="CASCADE"))
    plan_id: Mapped[str] = mapped_column(String(32), ForeignKey("manufacturing_plans.id"))
    revision: Mapped[int] = mapped_column(Integer, default=1)

    nc_program_ids: Mapped[list[str]] = mapped_column(JSONDict, default=list)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    package_hash: Mapped[str] = mapped_column(String(64), default="")
    package_signature: Mapped[str | None] = mapped_column(String(128), nullable=True)
    package_storage_key: Mapped[str | None] = mapped_column(String(255), nullable=True)

    gate_results: Mapped[list[dict[str, Any]]] = mapped_column(JSONDict, default=list)
    status: Mapped[str] = mapped_column(String(24), default=ReleaseStatus.CANDIDATE)
    superseded_by_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revocation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class Approval(IdMixin, TenantMixin, TimestampMixin, Base):
    """Electronic signature against a gate and a specific versioned target."""

    __tablename__ = "approvals"

    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("part_projects.id", ondelete="CASCADE"))
    gate: Mapped[str] = mapped_column(String(48), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(48), nullable=False)
    target_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    target_hash: Mapped[str] = mapped_column(String(64), default="")

    actor_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"))
    actor_role: Mapped[str] = mapped_column(String(48), default="")
    decision: Mapped[str] = mapped_column(String(16), default="approved")
    statement: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    mfa_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    signature: Mapped[str] = mapped_column(String(128), default="")


class GateEvaluationRecord(IdMixin, TenantMixin, TimestampMixin, Base):
    """Persisted gate evaluation for audit and for the release checklist UI."""

    __tablename__ = "gate_evaluations"

    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("part_projects.id", ondelete="CASCADE"))
    gate: Mapped[str] = mapped_column(String(48), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(48), default="")
    target_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    blocks: Mapped[str] = mapped_column(String(64), default="")
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSONDict, default=list)
    max_severity: Mapped[str | None] = mapped_column(String(8), nullable=True)
    evaluated_by_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("users.id"), nullable=True)

    @property
    def is_stop(self) -> bool:
        return self.max_severity == Severity.S1_STOP
