"""Factory digital twin: machines, tool assemblies, fixtures and materials.

All four are versioned master data. A plan records the exact version it used so
a later edit to the shop configuration can never silently change a historical
plan (FR-MCH-002, PRD 8.2).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import LifecycleStatus
from app.db import Base
from app.models.base import IdMixin, JSONDict, TenantMixin, TimestampMixin, VersionMixin


class MachineVersion(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "machine_versions"
    __table_args__ = (UniqueConstraint("tenant_id", "code", "revision", name="uq_machine_rev"),)

    code: Mapped[str] = mapped_column(String(48), index=True, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    manufacturer: Mapped[str] = mapped_column(String(80), default="")
    model: Mapped[str] = mapped_column(String(80), default="")

    controller: Mapped[str] = mapped_column(String(48), default="FANUC")
    controller_version: Mapped[str] = mapped_column(String(48), default="")

    #: "3axis" or "3+2" indexed. Simultaneous 5-axis is outside the MVP support
    #: matrix (Appendix A).
    kinematics: Mapped[str] = mapped_column(String(24), default="3axis")
    rotary_axes: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)

    travels_mm: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    #: [[rpm, kW, Nm], ...] sampled spindle curve.
    spindle_curve: Mapped[list[list[float]]] = mapped_column(JSONDict, default=list)
    max_rpm: Mapped[float] = mapped_column(Float, default=12000.0)
    min_rpm: Mapped[float] = mapped_column(Float, default=60.0)
    max_feed_mm_min: Mapped[float] = mapped_column(Float, default=10000.0)
    rapid_mm_min: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    acceleration_mm_s2: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)

    tool_change_seconds: Mapped[float] = mapped_column(Float, default=6.0)
    index_seconds: Mapped[float] = mapped_column(Float, default=8.0)
    magazine_capacity: Mapped[int] = mapped_column(Integer, default=24)
    coolant: Mapped[list[str]] = mapped_column(JSONDict, default=lambda: ["flood"])
    work_offsets: Mapped[list[str]] = mapped_column(JSONDict, default=lambda: ["G54"])

    hourly_rate: Mapped[float] = mapped_column(Float, default=65.0)
    setup_rate: Mapped[float] = mapped_column(Float, default=55.0)

    #: Validated machine geometry used by the full-machine collision engine.
    machine_geometry: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    geometry_qualified: Mapped[bool] = mapped_column(Boolean, default=False)

    status: Mapped[str] = mapped_column(String(24), default=LifecycleStatus.CURRENT)
    content_hash: Mapped[str] = mapped_column(String(64), default="")


class ToolAssemblyVersion(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    """Cutter plus holder, extension and collet treated as one rigid assembly.

    The collision profile is the assembly silhouette, not just the cutter, so
    holder crashes are detectable (FR-TOL-001).
    """

    __tablename__ = "tool_assembly_versions"
    __table_args__ = (UniqueConstraint("tenant_id", "code", "revision", name="uq_tool_rev"),)

    code: Mapped[str] = mapped_column(String(48), index=True, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str] = mapped_column(String(200), default="")

    cutter: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    holder: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    extension: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    collet: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)

    gauge_length_mm: Mapped[float] = mapped_column(Float, default=90.0)
    #: Stacked [diameter_mm, length_mm] discs from the tip upward.
    collision_profile: Mapped[list[list[float]]] = mapped_column(JSONDict, default=list)
    has_collision_model: Mapped[bool] = mapped_column(Boolean, default=False)

    max_rpm: Mapped[float] = mapped_column(Float, default=12000.0)
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    location: Mapped[str] = mapped_column(String(80), default="")
    remaining_life_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_per_edge: Mapped[float] = mapped_column(Float, default=18.0)
    expected_life_minutes: Mapped[float] = mapped_column(Float, default=90.0)

    status: Mapped[str] = mapped_column(String(24), default=LifecycleStatus.CURRENT)
    content_hash: Mapped[str] = mapped_column(String(64), default="")


class FixtureVersion(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    """Vise, jaws, clamps and safe clearance geometry (FR-FIX-001)."""

    __tablename__ = "fixture_versions"
    __table_args__ = (UniqueConstraint("tenant_id", "code", "revision", name="uq_fixture_rev"),)

    code: Mapped[str] = mapped_column(String(48), index=True, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(160), default="")
    fixture_type: Mapped[str] = mapped_column(String(48), default="vise")

    #: Solids in fixture coordinates: boxes and cylinders the collision engine
    #: tests against.
    solids: Mapped[list[dict[str, Any]]] = mapped_column(JSONDict, default=list)
    jaw_opening_mm: Mapped[float] = mapped_column(Float, default=200.0)
    max_part_height_mm: Mapped[float] = mapped_column(Float, default=150.0)
    clamp_height_mm: Mapped[float] = mapped_column(Float, default=25.0)
    safe_clearance_mm: Mapped[float] = mapped_column(Float, default=5.0)
    datum_scheme: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)

    status: Mapped[str] = mapped_column(String(24), default=LifecycleStatus.CURRENT)
    content_hash: Mapped[str] = mapped_column(String(64), default="")


class Material(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    """Workpiece material with validated cutting rules.

    A material without validated rules can be entered by an engineer but is
    flagged; material is never inferred from appearance (Appendix A).
    """

    __tablename__ = "materials"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_material_code"),)

    code: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), default="")
    group: Mapped[str] = mapped_column(String(40), default="aluminium")
    density_g_cm3: Mapped[float] = mapped_column(Float, default=2.70)
    price_per_kg: Mapped[float] = mapped_column(Float, default=6.5)
    hardness_hb: Mapped[float | None] = mapped_column(Float, nullable=True)

    #: Kienzle specific cutting force coefficients.
    kc11: Mapped[float] = mapped_column(Float, default=700.0)
    mc: Mapped[float] = mapped_column(Float, default=0.25)

    #: Cutting rules keyed by "<cutter_material>/<operation_class>".
    cutting_rules: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    rules_validated: Mapped[bool] = mapped_column(Boolean, default=False)
    entered_by_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("users.id"), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class PostProcessorVersion(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    """A postprocessor definition and its certification against one machine.

    Certification is per machine model, controller version and post version, as
    a triple. A universal post is explicitly out of scope (Appendix A).
    """

    __tablename__ = "postprocessor_versions"
    __table_args__ = (UniqueConstraint("tenant_id", "code", "revision", name="uq_post_rev"),)

    code: Mapped[str] = mapped_column(String(48), index=True, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(160), default="")
    dialect: Mapped[str] = mapped_column(String(32), default="fanuc")

    machine_code: Mapped[str] = mapped_column(String(48), nullable=False)
    machine_model: Mapped[str] = mapped_column(String(80), default="")
    controller: Mapped[str] = mapped_column(String(48), default="FANUC")
    controller_version: Mapped[str] = mapped_column(String(48), default="")

    #: Declarative configuration only. The runtime interprets data; it does not
    #: execute tenant-supplied code (PRD 7.3, "Compromised post script").
    definition: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    runtime_hash: Mapped[str] = mapped_column(String(64), default="")

    certified: Mapped[bool] = mapped_column(Boolean, default=False)
    certified_by_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("users.id"), nullable=True)
    certification_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    test_program_hashes: Mapped[list[str]] = mapped_column(JSONDict, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    revocation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(24), default=LifecycleStatus.DRAFT)
