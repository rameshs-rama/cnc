"""Platform plumbing: jobs, events, idempotency and the dependency graph."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import JobStatus
from app.db import Base
from app.models.base import IdMixin, JSONDict, TenantMixin, TimestampMixin


class Job(IdMixin, TenantMixin, TimestampMixin, Base):
    """Durable unit of asynchronous engineering work.

    Inputs are immutable and outputs are content addressed, so a retried job
    produces the same result rather than a divergent one (PRD 7.2).
    """

    __tablename__ = "jobs"

    kind: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("part_projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.QUEUED, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    input_hash: Mapped[str] = mapped_column(String(64), default="")

    stage: Mapped[str] = mapped_column(String(64), default="queued")
    percent: Mapped[float] = mapped_column(Float, default=0.0)
    #: Diagnostic lines tagged with the minimum role needed to read them.
    logs: Mapped[list[dict[str, Any]]] = mapped_column(JSONDict, default=list)

    result: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requested_by_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("users.id"), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(32), default="")


class DomainEvent(IdMixin, TenantMixin, TimestampMixin, Base):
    """Append-only event store backing the topics in PRD 9.2."""

    __tablename__ = "domain_events"
    __table_args__ = (Index("ix_event_topic_created", "topic", "created_at"),)

    topic: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(32), default="")
    #: Monotonic ordering within a tenant, assigned by the event publisher so
    #: consumers can replay deterministically even when timestamps collide.
    sequence: Mapped[int] = mapped_column(Integer, default=0, index=True)


class IdempotencyRecord(IdMixin, TenantMixin, TimestampMixin, Base):
    """Replay protection for mutations (PRD 9.1)."""

    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_idempotency"),)

    key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    route: Mapped[str] = mapped_column(String(160), default="")
    request_hash: Mapped[str] = mapped_column(String(64), default="")
    response: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    status_code: Mapped[int] = mapped_column(Integer, default=200)


class ArtifactDependency(IdMixin, TenantMixin, TimestampMixin, Base):
    """Edge in the invalidation graph.

    When an upstream object gains a new version, every downstream object that
    referenced the old hash is marked stale automatically (PRD 12, "Stale
    downstream artifact").
    """

    __tablename__ = "artifact_dependencies"
    __table_args__ = (
        UniqueConstraint(
            "downstream_kind", "downstream_id", "upstream_kind", "upstream_id", name="uq_dependency_edge"
        ),
        Index("ix_dependency_upstream", "upstream_kind", "upstream_id"),
    )

    downstream_kind: Mapped[str] = mapped_column(String(48), nullable=False)
    downstream_id: Mapped[str] = mapped_column(String(32), nullable=False)
    upstream_kind: Mapped[str] = mapped_column(String(48), nullable=False)
    upstream_id: Mapped[str] = mapped_column(String(32), nullable=False)
    upstream_hash: Mapped[str] = mapped_column(String(64), default="")


class AuditRecord(IdMixin, TenantMixin, TimestampMixin, Base):
    """Append-only audit of evidence, approvals, configuration and release."""

    __tablename__ = "audit_records"
    __table_args__ = (Index("ix_audit_object", "object_kind", "object_id"),)

    object_kind: Mapped[str] = mapped_column(String(48), nullable=False)
    object_id: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    before: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    after: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str] = mapped_column(String(32), default="")
