"""Append-only audit trail.

Audit rows are written in the same transaction as the change they describe.
Nothing deletes them; a correction is a new row (PRD 11, Audit).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.platform import AuditRecord


def record(
    db: Session,
    *,
    tenant_id: str,
    object_kind: str,
    object_id: str,
    action: str,
    actor_id: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    reason: str | None = None,
    trace_id: str = "",
) -> AuditRecord:
    entry = AuditRecord(
        tenant_id=tenant_id,
        object_kind=object_kind,
        object_id=object_id,
        action=action,
        actor_id=actor_id,
        before=before or {},
        after=after or {},
        reason=reason,
        trace_id=trace_id,
    )
    db.add(entry)
    return entry
