"""Production feedback and governed learning (PRD 4.6, E11)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import ProposalStatus, RunResult
from app.db import Base
from app.models.base import IdMixin, JSONDict, TenantMixin, TimestampMixin, VersionMixin


class MachineRun(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "machine_runs"

    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("part_projects.id", ondelete="CASCADE"))
    release_id: Mapped[str] = mapped_column(String(32), ForeignKey("releases.id"), index=True)
    #: The exact released NC program that ran. Unmatched telemetry is rejected
    #: rather than attached to an approximate program (FR-FBK-001).
    nc_program_id: Mapped[str] = mapped_column(String(32), ForeignKey("nc_programs.id"))
    nc_program_hash: Mapped[str] = mapped_column(String(64), default="")

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    operator: Mapped[str] = mapped_column(String(120), default="")

    actual_setup_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_cycle_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_tool_changes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    alarms: Mapped[list[dict[str, Any]]] = mapped_column(JSONDict, default=list)
    tool_outcomes: Mapped[list[dict[str, Any]]] = mapped_column(JSONDict, default=list)
    scrap_count: Mapped[int] = mapped_column(Integer, default=0)
    pieces: Mapped[int] = mapped_column(Integer, default=1)
    result: Mapped[str] = mapped_column(String(16), default=RunResult.PENDING)
    telemetry_suspect: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class InspectionResult(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "inspection_results"

    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("part_projects.id", ondelete="CASCADE"))
    machine_run_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("machine_runs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    feature_key: Mapped[str] = mapped_column(String(64), nullable=False)
    characteristic: Mapped[str] = mapped_column(String(80), default="diameter")
    nominal: Mapped[float] = mapped_column(Float, default=0.0)
    tolerance_plus: Mapped[float] = mapped_column(Float, default=0.05)
    tolerance_minus: Mapped[float] = mapped_column(Float, default=0.05)
    actual: Mapped[float] = mapped_column(Float, default=0.0)
    unit: Mapped[str] = mapped_column(String(16), default="mm")
    instrument: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    in_tolerance: Mapped[bool] = mapped_column(Boolean, default=True)
    disposition: Mapped[str] = mapped_column(String(32), default="Accept")
    inspector_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("users.id"), nullable=True)


class RuleProposal(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    """A learned suggestion. Never a tenant default until approved (FR-FBK-003)."""

    __tablename__ = "rule_proposals"

    project_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("part_projects.id", ondelete="SET NULL"), nullable=True
    )
    scope: Mapped[str] = mapped_column(String(64), default="cutting_rule")
    target_ref: Mapped[str] = mapped_column(String(160), default="")
    current_value: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    proposed_value: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONDict, default=list)
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    expected_impact: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    validation_state: Mapped[str] = mapped_column(String(32), default="shadow")
    status: Mapped[str] = mapped_column(String(24), default=ProposalStatus.PROPOSED)
    reviewed_by_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("users.id"), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
