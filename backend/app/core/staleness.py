"""Dependency graph and automatic invalidation.

Any edit that invalidates downstream results marks the affected plans, paths,
simulations, costs and NC programs as stale (PRD 6.1). Stale artifacts can be
inspected but never released.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import LifecycleStatus
from app.models.planning import ManufacturingPlan, ToolpathVersion
from app.models.platform import ArtifactDependency
from app.models.verification import CostEstimate, NCProgram, SimulationRun

#: Downstream kind -> (model, does the model carry a ``status`` column?)
_STALEABLE = {
    "plan": (ManufacturingPlan, True),
    "toolpath": (ToolpathVersion, True),
    "simulation": (SimulationRun, False),
    "cost": (CostEstimate, False),
    "nc_program": (NCProgram, True),
}


def link(
    db: Session,
    *,
    tenant_id: str,
    downstream_kind: str,
    downstream_id: str,
    upstream_kind: str,
    upstream_id: str,
    upstream_hash: str = "",
) -> None:
    """Record that ``downstream`` was produced from ``upstream``."""
    existing = db.execute(
        select(ArtifactDependency).where(
            ArtifactDependency.downstream_kind == downstream_kind,
            ArtifactDependency.downstream_id == downstream_id,
            ArtifactDependency.upstream_kind == upstream_kind,
            ArtifactDependency.upstream_id == upstream_id,
        )
    ).scalar_one_or_none()
    if existing:
        existing.upstream_hash = upstream_hash
        return
    db.add(
        ArtifactDependency(
            tenant_id=tenant_id,
            downstream_kind=downstream_kind,
            downstream_id=downstream_id,
            upstream_kind=upstream_kind,
            upstream_id=upstream_id,
            upstream_hash=upstream_hash,
        )
    )


def invalidate(db: Session, *, upstream_kind: str, upstream_id: str, reason: str) -> list[tuple[str, str]]:
    """Mark everything transitively downstream of an object as stale.

    Returns the ``(kind, id)`` pairs that changed, so the caller can report
    exactly what a seemingly small edit invalidated.
    """
    affected: list[tuple[str, str]] = []
    frontier = [(upstream_kind, upstream_id)]
    seen: set[tuple[str, str]] = set(frontier)

    while frontier:
        kind, object_id = frontier.pop()
        edges = db.execute(
            select(ArtifactDependency).where(
                ArtifactDependency.upstream_kind == kind,
                ArtifactDependency.upstream_id == object_id,
            )
        ).scalars()
        for edge in edges:
            key = (edge.downstream_kind, edge.downstream_id)
            if key in seen:
                continue
            seen.add(key)
            frontier.append(key)
            if _mark_stale(db, edge.downstream_kind, edge.downstream_id, reason):
                affected.append(key)
    return affected


def _mark_stale(db: Session, kind: str, object_id: str, reason: str) -> bool:
    entry = _STALEABLE.get(kind)
    if not entry:
        return False
    model, has_status = entry
    obj = db.get(model, object_id)
    if obj is None or getattr(obj, "stale", False):
        return False
    obj.stale = True
    if hasattr(obj, "stale_reason"):
        obj.stale_reason = reason
    if has_status and getattr(obj, "status", None) != LifecycleStatus.STALE:
        obj.status = LifecycleStatus.STALE
    return True


def upstream_of(db: Session, *, downstream_kind: str, downstream_id: str) -> list[ArtifactDependency]:
    return list(
        db.execute(
            select(ArtifactDependency).where(
                ArtifactDependency.downstream_kind == downstream_kind,
                ArtifactDependency.downstream_id == downstream_id,
            )
        ).scalars()
    )
