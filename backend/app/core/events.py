"""Domain event publication (PRD 9.2).

Events are appended inside the caller's transaction so an event never describes
a state change that was rolled back. Live consumers read them through the
server-sent event stream.
"""

from __future__ import annotations

import threading
from queue import Empty, Full, Queue
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.platform import DomainEvent


class Topic:
    ARTIFACT_PROCESSED = "artifact.processed"
    GEOMETRY_VERSION_CREATED = "geometry.version.created"
    ENGINEERING_CONFLICT_DETECTED = "engineering.conflict.detected"
    PLAN_CANDIDATE_CREATED = "plan.candidate.created"
    SIMULATION_COMPLETED = "simulation.completed"
    RELEASE_STATUS_CHANGED = "release.status.changed"
    MACHINE_RUN_COMPLETED = "machine.run.completed"
    LEARNING_RULE_PROPOSED = "learning.rule.proposed"
    # Operational topics outside the published contract.
    JOB_PROGRESS = "job.progress"
    PROJECT_STATE_CHANGED = "project.state.changed"


ALL_TOPICS = [
    Topic.ARTIFACT_PROCESSED,
    Topic.GEOMETRY_VERSION_CREATED,
    Topic.ENGINEERING_CONFLICT_DETECTED,
    Topic.PLAN_CANDIDATE_CREATED,
    Topic.SIMULATION_COMPLETED,
    Topic.RELEASE_STATUS_CHANGED,
    Topic.MACHINE_RUN_COMPLETED,
    Topic.LEARNING_RULE_PROPOSED,
    Topic.JOB_PROGRESS,
    Topic.PROJECT_STATE_CHANGED,
]


class _Broker:
    """In-process fan-out to connected SSE clients.

    Deliberately best effort: durability lives in the event store, not here. A
    slow client drops frames instead of stalling the writer.
    """

    def __init__(self) -> None:
        self._subscribers: dict[int, Queue] = {}
        self._lock = threading.Lock()
        self._next = 0

    def subscribe(self) -> tuple[int, Queue]:
        with self._lock:
            token = self._next
            self._next += 1
            queue: Queue = Queue(maxsize=256)
            self._subscribers[token] = queue
        return token, queue

    def unsubscribe(self, token: int) -> None:
        with self._lock:
            self._subscribers.pop(token, None)

    def publish(self, frame: dict[str, Any]) -> None:
        with self._lock:
            targets = list(self._subscribers.values())
        for queue in targets:
            try:
                queue.put_nowait(frame)
            except Full:
                try:
                    queue.get_nowait()
                    queue.put_nowait(frame)
                except (Empty, Full):
                    pass


broker = _Broker()


def publish(
    db: Session,
    *,
    tenant_id: str,
    topic: str,
    payload: dict[str, Any],
    project_id: str | None = None,
    actor_id: str | None = None,
    trace_id: str = "",
) -> DomainEvent:
    next_sequence = (
        db.execute(select(func.coalesce(func.max(DomainEvent.sequence), 0)).where(DomainEvent.tenant_id == tenant_id))
        .scalar_one()
        + 1
    )
    event = DomainEvent(
        tenant_id=tenant_id,
        topic=topic,
        payload=payload,
        project_id=project_id,
        actor_id=actor_id,
        trace_id=trace_id,
        sequence=next_sequence,
    )
    db.add(event)
    db.flush()
    broker.publish(
        {
            "id": event.id,
            "topic": topic,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "sequence": next_sequence,
            "payload": payload,
        }
    )
    return event
