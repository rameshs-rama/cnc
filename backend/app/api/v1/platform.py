"""Jobs, events and audit."""

from __future__ import annotations

import json
import queue as stdqueue
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, require, scoped, tenant_project
from app.core import events
from app.core.rbac import Permission, Role
from app.jobs import queue as job_queue
from app.models.platform import AuditRecord, DomainEvent, Job
from app.schemas.api import JobLogsOut, JobOut
from app.schemas.common import ActionResult

router = APIRouter(tags=["platform"])


@router.get("/jobs/{job_id}", response_model=JobOut, summary="Read job stage and diagnostics")
def get_job(job_id: str, db: DbSession, user: CurrentUser) -> Job:
    """Never returns partial geometry as approved; only stage and result."""
    return scoped(db, user, Job, job_id, "Job")


@router.get("/jobs", response_model=list[JobOut], summary="List jobs")
def list_jobs(
    db: DbSession,
    user: CurrentUser,
    project_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[Job]:
    query = select(Job).where(Job.tenant_id == user.tenant_id)
    if project_id:
        query = query.where(Job.project_id == project_id)
    if status:
        query = query.where(Job.status == status)
    return list(db.execute(query.order_by(Job.created_at.desc()).limit(limit)).scalars())


@router.get("/jobs/{job_id}/logs", response_model=JobLogsOut, summary="Job diagnostics for the caller's role")
def job_logs(job_id: str, db: DbSession, user: CurrentUser) -> JobLogsOut:
    """Administrator-level diagnostics are withheld from other roles (PRD 6.1)."""
    job = scoped(db, user, Job, job_id, "Job")
    is_admin = Role.ADMINISTRATOR.value in (user.roles or [])
    logs = [entry for entry in (job.logs or []) if is_admin or entry.get("role") != Role.ADMINISTRATOR.value]
    return JobLogsOut(job_id=job.id, status=job.status, stage=job.stage, percent=job.percent, logs=logs)


@router.post("/jobs/{job_id}:cancel", response_model=ActionResult, summary="Request job cancellation")
def cancel_job(job_id: str, db: DbSession, user: CurrentUser) -> ActionResult:
    job = scoped(db, user, Job, job_id, "Job")
    job_queue.cancel(db, job)
    db.commit()
    return ActionResult(
        message="Cancellation requested; the job stops at its next checkpoint",
        detail={"job_id": job.id, "status": job.status},
    )


@router.get("/events", summary="Read the event store")
def list_events(
    db: DbSession,
    user: CurrentUser,
    project_id: str | None = None,
    topic: str | None = None,
    after_sequence: int = 0,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    query = select(DomainEvent).where(DomainEvent.tenant_id == user.tenant_id, DomainEvent.sequence > after_sequence)
    if project_id:
        query = query.where(DomainEvent.project_id == project_id)
    if topic:
        query = query.where(DomainEvent.topic == topic)
    return [
        {
            "id": e.id,
            "sequence": e.sequence,
            "topic": e.topic,
            "project_id": e.project_id,
            "payload": e.payload,
            "actor_id": e.actor_id,
            "created_at": e.created_at,
        }
        for e in db.execute(query.order_by(DomainEvent.sequence).limit(limit)).scalars()
    ]


@router.get("/events/topics", summary="The published event topics")
def list_topics() -> list[dict[str, str]]:
    return [
        {"topic": events.Topic.ARTIFACT_PROCESSED, "produced_when": "Parsing or extraction completes"},
        {"topic": events.Topic.GEOMETRY_VERSION_CREATED, "produced_when": "New geometry is stored"},
        {"topic": events.Topic.ENGINEERING_CONFLICT_DETECTED, "produced_when": "Evidence disagrees beyond policy"},
        {"topic": events.Topic.PLAN_CANDIDATE_CREATED, "produced_when": "Planner produces a candidate"},
        {"topic": events.Topic.SIMULATION_COMPLETED, "produced_when": "Verification ends"},
        {"topic": events.Topic.RELEASE_STATUS_CHANGED, "produced_when": "Approval, release or supersession occurs"},
        {"topic": events.Topic.MACHINE_RUN_COMPLETED, "produced_when": "Production record closes"},
        {"topic": events.Topic.LEARNING_RULE_PROPOSED, "produced_when": "Feedback suggests a rule"},
        {"topic": events.Topic.JOB_PROGRESS, "produced_when": "A compute job reports progress"},
        {"topic": events.Topic.PROJECT_STATE_CHANGED, "produced_when": "The project moves along its state machine"},
    ]


@router.get("/events/stream", summary="Server-sent event stream for live job and gate updates")
def stream_events(request: Request, user: CurrentUser, project_id: str | None = None) -> StreamingResponse:
    tenant_id = user.tenant_id

    def generator():
        token, frames = events.broker.subscribe()
        try:
            yield 'event: ready\ndata: {"status":"subscribed"}\n\n'
            while True:
                try:
                    frame = frames.get(timeout=15.0)
                except stdqueue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                if frame.get("tenant_id") != tenant_id:
                    continue
                if project_id and frame.get("project_id") != project_id:
                    continue
                yield f"event: {frame['topic']}\ndata: {json.dumps(frame, default=str)}\n\n"
        finally:
            events.broker.unsubscribe(token)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.get("/projects/{project_id}/audit", summary="Append-only audit trail")
def project_audit(
    project_id: str,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.PROJECT_READ))],
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[dict[str, Any]]:
    tenant_project(db, user, project_id)
    rows = db.execute(
        select(AuditRecord)
        .where(AuditRecord.tenant_id == user.tenant_id)
        .order_by(AuditRecord.created_at.desc())
        .limit(limit)
    ).scalars()
    return [
        {
            "id": r.id,
            "object_kind": r.object_kind,
            "object_id": r.object_id,
            "action": r.action,
            "actor_id": r.actor_id,
            "before": r.before,
            "after": r.after,
            "reason": r.reason,
            "created_at": r.created_at,
        }
        for r in rows
    ]
