"""Project state machine and audit-bearing transitions (PRD 3.1)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core import audit, events
from app.core.enums import PROJECT_TRANSITIONS, ProjectState
from app.core.errors import ConflictError
from app.models.base import audit_entry
from app.models.project import PartProject


def can_transition(current: ProjectState, target: ProjectState) -> bool:
    return target in PROJECT_TRANSITIONS.get(current, ())


def transition(
    db: Session, project: PartProject, target: ProjectState, *, actor_id: str | None = None, reason: str | None = None
) -> PartProject:
    current = ProjectState(project.state)
    if current == target:
        return project
    if not can_transition(current, target):
        allowed = ", ".join(s.value for s in PROJECT_TRANSITIONS.get(current, ()))
        raise ConflictError(
            f"A project in {current.value} cannot move to {target.value}",
            detail={"current": current.value, "target": target.value, "allowed": allowed},
            code="invalid_transition",
        )

    project.state = target.value
    project.version += 1
    project.audit = [*(project.audit or []), audit_entry(actor_id or "system", f"state:{current}->{target}", reason)]
    db.add(project)
    audit.record(
        db,
        tenant_id=project.tenant_id,
        object_kind="project",
        object_id=project.id,
        action="state_transition",
        actor_id=actor_id,
        before={"state": current.value},
        after={"state": target.value},
        reason=reason,
    )
    events.publish(
        db,
        tenant_id=project.tenant_id,
        topic=events.Topic.PROJECT_STATE_CHANGED,
        project_id=project.id,
        actor_id=actor_id,
        payload={"from": current.value, "to": target.value, "reason": reason},
    )
    return project


def advance_at_least(db: Session, project: PartProject, target: ProjectState, *, actor_id: str | None = None) -> None:
    """Move forward one step toward ``target`` when the graph allows it.

    Used by the engineering services so a successful job nudges the project
    along without ever skipping a state.
    """
    current = ProjectState(project.state)
    if current == target or not can_transition(current, target):
        return
    transition(db, project, target, actor_id=actor_id, reason="Advanced automatically on successful job")


def path_to(current: ProjectState, target: ProjectState) -> list[ProjectState] | None:
    """Shortest sequence of legal transitions from ``current`` to ``target``.

    Returns ``None`` when no path exists, so a caller cannot manufacture a route
    the state machine does not allow.
    """
    if current == target:
        return []
    frontier: list[tuple[ProjectState, list[ProjectState]]] = [(current, [])]
    seen = {current}
    while frontier:
        state, route = frontier.pop(0)
        for nxt in PROJECT_TRANSITIONS.get(state, ()):
            if nxt in seen:
                continue
            step = [*route, nxt]
            if nxt == target:
                return step
            seen.add(nxt)
            frontier.append((nxt, step))
    return None


def advance_to(
    db: Session, project: PartProject, target: ProjectState, *, actor_id: str | None = None, reason: str | None = None
) -> PartProject:
    """Walk the state machine to ``target``, one legal transition at a time."""
    route = path_to(ProjectState(project.state), target)
    if route is None:
        raise ConflictError(
            f"There is no legal route from {project.state} to {target.value}",
            detail={"current": project.state, "target": target.value},
            code="no_transition_path",
        )
    for state in route:
        transition(db, project, state, actor_id=actor_id, reason=reason)
    return project
