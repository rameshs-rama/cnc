"""Part projects, source artifacts and evidence observations."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    ArtifactKind,
    ArtifactStatus,
    AuthorityRank,
    Criticality,
    Disposition,
    ProjectState,
    Severity,
    VerificationStatus,
)
from app.db import Base
from app.models.base import IdMixin, JSONDict, TenantMixin, TimestampMixin, VersionMixin


class PartProject(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "part_projects"

    part_number: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    revision: Mapped[str] = mapped_column(String(16), default="A")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[str] = mapped_column(String(40), default=ProjectState.DRAFT)

    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_system: Mapped[str] = mapped_column(String(16), default="metric")
    intended_use: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_material_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Declared origin of the source data. Required before release so IP
    #: provenance is auditable (PRD 12, "IP misuse").
    provenance_declaration: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)

    owner_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("users.id"), nullable=True)
    audit: Mapped[list[dict[str, Any]]] = mapped_column(JSONDict, default=list)

    artifacts: Mapped[list[SourceArtifact]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class SourceArtifact(IdMixin, TenantMixin, TimestampMixin, Base):
    """Immutable evidence.

    Source bytes are never modified. A correction creates a new artifact and a
    ``supersedes`` relationship (PRD 8.2).
    """

    __tablename__ = "source_artifacts"

    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("part_projects.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    kind: Mapped[str] = mapped_column(String(32), default=ArtifactKind.UNKNOWN)
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False)

    authority: Mapped[str] = mapped_column(String(48), default=AuthorityRank.PHOTO)
    authority_overridden: Mapped[bool] = mapped_column(Boolean, default=False)
    authority_override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(24), default=ArtifactStatus.UPLOADED)
    parser: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parse_warnings: Mapped[list[str]] = mapped_column(JSONDict, default=list)
    extracted: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)

    uploader_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("users.id"), nullable=True)
    provenance_declaration: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    supersedes_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("source_artifacts.id"), nullable=True)

    #: Guided-capture quality metrics when the artifact came from a capture
    #: session: blur, glare, calibration target, covered region (FR-CAP-001).
    capture_metrics: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    capture_session_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    project: Mapped[PartProject] = relationship(back_populates="artifacts")


class CaptureSession(IdMixin, TenantMixin, TimestampMixin, Base):
    """Guided capture session with live coverage accounting (FR-CAP-001/002)."""

    __tablename__ = "capture_sessions"

    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("part_projects.id", ondelete="CASCADE"), index=True
    )
    calibration_target: Mapped[str] = mapped_column(String(64), default="checkerboard-25mm")
    required_regions: Mapped[list[str]] = mapped_column(JSONDict, default=list)
    covered_regions: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    completeness: Mapped[float] = mapped_column(Float, default=0.0)
    threshold: Mapped[float] = mapped_column(Float, default=0.85)
    waiver_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)


class EvidenceObservation(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    """The confidence object of PRD 5.1.

    One row is one claim about one attribute, bound to the evidence that
    produced it. Nothing in the system may assert an engineering value without
    one of these rows behind it.
    """

    __tablename__ = "evidence_observations"

    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("part_projects.id", ondelete="CASCADE"), index=True
    )
    geometry_version_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("geometry_versions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    feature_key: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    attribute: Mapped[str] = mapped_column(String(80), nullable=False)

    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    value_text: Mapped[str | None] = mapped_column(String(160), nullable=True)
    unit: Mapped[str] = mapped_column(String(24), default="mm")
    original_representation: Mapped[str | None] = mapped_column(String(160), nullable=True)

    source_artifact_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("source_artifacts.id", ondelete="SET NULL"), nullable=True
    )
    source_region: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    authority: Mapped[str] = mapped_column(String(48), default=AuthorityRank.AI_INFERENCE)
    method: Mapped[str] = mapped_column(String(80), default="unspecified")

    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    uncertainty: Mapped[float | None] = mapped_column(Float, nullable=True)
    residual: Mapped[float | None] = mapped_column(Float, nullable=True)

    status: Mapped[str] = mapped_column(String(32), default=VerificationStatus.INFERRED)
    criticality: Mapped[str] = mapped_column(String(32), default=Criticality.NONCRITICAL)
    disposition: Mapped[str] = mapped_column(String(16), default=Disposition.PENDING)
    disposition_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    superseded_by_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    verified_by_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("users.id"), nullable=True)
    audit: Mapped[list[dict[str, Any]]] = mapped_column(JSONDict, default=list)

    @property
    def interval(self) -> tuple[float, float] | None:
        if self.value is None:
            return None
        half = self.uncertainty or 0.0
        return (self.value - half, self.value + half)


class EvidenceConflict(IdMixin, TenantMixin, TimestampMixin, Base):
    """Recorded disagreement between observations (FR-INT-006).

    The conflict stays visible until an engineer dispositions it; the
    authoritative observation is identified but never silently applied.
    """

    __tablename__ = "evidence_conflicts"

    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("part_projects.id", ondelete="CASCADE"), index=True
    )
    feature_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attribute: Mapped[str] = mapped_column(String(80), nullable=False)
    observation_ids: Mapped[list[str]] = mapped_column(JSONDict, default=list)
    authoritative_observation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    severity: Mapped[str] = mapped_column(String(8), default=Severity.S2_ENGINEER)
    summary: Mapped[str] = mapped_column(Text, default="")
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolution: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)


class Waiver(IdMixin, TenantMixin, TimestampMixin, Base):
    """Authorized, reasoned exception to a non-S1 gate finding (PRD 5.3)."""

    __tablename__ = "waivers"

    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("part_projects.id", ondelete="CASCADE"), index=True
    )
    gate: Mapped[str] = mapped_column(String(48), nullable=False)
    finding_code: Mapped[str] = mapped_column(String(80), nullable=False)
    object_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    severity: Mapped[str] = mapped_column(String(8), default=Severity.S2_ENGINEER)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    granted_by_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
