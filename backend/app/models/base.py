"""Shared mapped columns and mixins."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

JSONDict = JSON().with_variant(JSON(), "postgresql")


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class IdMixin:
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, server_default=func.now()
    )


class TenantMixin:
    """Tenant scoping is mandatory on every business entity (PRD 7.3)."""

    tenant_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )


class VersionMixin:
    """Optimistic concurrency.

    Every mutation accepts an expected version; a mismatch is rejected rather
    than silently overwriting a concurrent engineering edit (PRD 9.1).
    """

    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


def audit_entry(actor_id: str, action: str, reason: str | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "actor_id": actor_id,
        "action": action,
        "reason": reason,
        "at": utcnow().isoformat(),
        **extra,
    }
