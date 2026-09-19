"""Geometry versions and recognized manufacturing features."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import Criticality, FeatureSupport, FeatureType, LifecycleStatus, VerificationStatus
from app.db import Base
from app.models.base import IdMixin, JSONDict, TenantMixin, TimestampMixin, VersionMixin


class GeometryVersion(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    """An immutable geometry revision.

    Editing geometry never overwrites the previous revision; it creates N+1 and
    every downstream artifact that referenced N becomes stale (PRD 8.2).
    """

    __tablename__ = "geometry_versions"
    __table_args__ = (UniqueConstraint("project_id", "revision", name="uq_geometry_revision"),)

    project_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("part_projects.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer, default=1)
    parent_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("geometry_versions.id"), nullable=True)

    #: Canonical part model: stock envelope plus parametric features in the part
    #: coordinate system. This is the engineering representation; the mesh
    #: derivative below is the visual one (PRD 10.1).
    part_model: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    mesh_derivative_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kernel_format: Mapped[str] = mapped_column(String(32), default="mip-parametric-1")

    units: Mapped[str] = mapped_column(String(8), default="mm")

    #: Whether a traceable dimension or calibration reference established scale.
    #: Without one, scaled manufacturing geometry is refused (FR-REC-004).
    scale_established: Mapped[bool] = mapped_column(Boolean, default=False)
    scale_source: Mapped[str | None] = mapped_column(String(160), nullable=True)
    scale_uncertainty_mm: Mapped[float | None] = mapped_column(Float, nullable=True)

    #: Reconstruction quality report: coverage, reprojection error, residuals,
    #: occlusion (FR-REC-001, PRD 10.1).
    quality_report: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    provisional: Mapped[bool] = mapped_column(Boolean, default=True)

    status: Mapped[str] = mapped_column(String(24), default=LifecycleStatus.DRAFT)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    source_artifact_hashes: Mapped[list[str]] = mapped_column(JSONDict, default=list)

    approved_by_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("users.id"), nullable=True)
    approval_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    audit: Mapped[list[dict[str, Any]]] = mapped_column(JSONDict, default=list)


class ManufacturingFeature(IdMixin, TenantMixin, TimestampMixin, Base):
    """A recognized feature with a stable identity across revisions (FR-FTR-002)."""

    __tablename__ = "manufacturing_features"
    __table_args__ = (UniqueConstraint("geometry_version_id", "stable_key", name="uq_feature_stable"),)

    geometry_version_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("geometry_versions.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("part_projects.id", ondelete="CASCADE"))

    #: Deterministic hash of type plus invariant geometry, so the same physical
    #: feature keeps its identity when unrelated geometry changes.
    stable_key: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    feature_type: Mapped[str] = mapped_column(String(32), default=FeatureType.POCKET)
    label: Mapped[str] = mapped_column(String(80), default="")

    parameters: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    access_direction: Mapped[list[float]] = mapped_column(JSONDict, default=lambda: [0.0, 0.0, 1.0])

    support: Mapped[str] = mapped_column(String(32), default=FeatureSupport.SUPPORTED)
    support_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    criticality: Mapped[str] = mapped_column(String(32), default=Criticality.NONCRITICAL)
    status: Mapped[str] = mapped_column(String(32), default=VerificationStatus.INFERRED)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)

    tolerance: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    surface_finish_ra: Mapped[float | None] = mapped_column(Float, nullable=True)
    thread_spec: Mapped[str | None] = mapped_column(String(64), nullable=True)

    predecessor_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
