"""Tenancy and identity."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import IdMixin, JSONDict, TenantMixin, TimestampMixin, VersionMixin


class Tenant(IdMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    #: Default unit system presented in the UI. Conversion is never implicit
    #: at import or release (PRD 6.1).
    unit_system: Mapped[str] = mapped_column(String(16), default="metric")
    currency: Mapped[str] = mapped_column(String(8), default="EUR")

    #: Production tenants default to four-eyes approval (PRD 2.1).
    allow_self_approval: Mapped[bool] = mapped_column(Boolean, default=False)

    #: Minimum confidence for a release-critical attribute before geometry can
    #: be approved without a waiver (FR-ENG-004).
    critical_confidence_threshold: Mapped[float] = mapped_column(Float, default=0.90)

    #: Absolute agreement window, in millimetres, before two observations of the
    #: same attribute are treated as a conflict (FR-INT-006).
    conflict_tolerance_mm: Mapped[float] = mapped_column(Float, default=0.05)

    security_profile: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    policy: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)

    users: Mapped[list[User]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


class User(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_user_tenant_email"),)

    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    roles: Mapped[list[str]] = mapped_column(JSONDict, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    #: Second factor. Required to sign an NC release (FR-REL-001).
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    tenant: Mapped[Tenant] = relationship(back_populates="users")


class ProjectAssignment(IdMixin, TenantMixin, TimestampMixin, Base):
    """Project-level role assignment layered on top of tenant roles (PRD 2.1)."""

    __tablename__ = "project_assignments"
    __table_args__ = (UniqueConstraint("project_id", "user_id", name="uq_assignment"),)

    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("part_projects.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id", ondelete="CASCADE"))
    roles: Mapped[list[str]] = mapped_column(JSONDict, default=list)
