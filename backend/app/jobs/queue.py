"""Database-backed job queue.

Jobs survive a process restart because state lives in the database, not in
memory. Claiming is a guarded UPDATE so two workers cannot take the same job.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.enums import JobStatus
from app.core.hashing import sha256_json
from app.models.base import new_id
from app.models.platform import Job


def enqueue(
    db: Session,
    *,
    tenant_id: str,
    kind: str,
    payload: dict[str, Any],
    project_id: str | None = None,
    requested_by_id: str | None = None,
    trace_id: str = "",
) -> Job:
    job = Job(
        tenant_id=tenant_id,
        kind=kind,
        project_id=project_id,
        payload=payload,
        input_hash=sha256_json(payload),
        requested_by_id=requested_by_id,
        trace_id=trace_id or new_id()[:16],
        status=JobStatus.QUEUED,
        stage="queued",
    )
    db.add(job)
    db.flush()
    return job


def claim_next(db: Session) -> Job | None:
    """Atomically move one queued job to running and return it."""
    candidate = db.execute(
        select(Job.id).where(Job.status == JobStatus.QUEUED).order_by(Job.created_at).limit(1)
    ).scalar_one_or_none()
    if candidate is None:
        return None

    updated = db.execute(
        update(Job)
        .where(Job.id == candidate, Job.status == JobStatus.QUEUED)
        .values(
            status=JobStatus.RUNNING,
            started_at=datetime.now(UTC),
            attempts=Job.attempts + 1,
            stage="starting",
            percent=0.0,
        )
    )
    db.commit()
    if updated.rowcount != 1:
        return None
    return db.get(Job, candidate)


def progress(db: Session, job: Job, *, stage: str, percent: float, message: str = "", role: str = "engineer") -> None:
    job.stage = stage
    job.percent = max(0.0, min(100.0, percent))
    if message:
        logs = list(job.logs or [])
        logs.append({"at": datetime.now(UTC).isoformat(), "stage": stage, "message": message, "role": role})
        job.logs = logs[-200:]
    db.add(job)
    db.commit()


def finish(db: Session, job: Job, *, result: dict[str, Any]) -> None:
    job.status = JobStatus.SUCCEEDED
    job.result = result
    job.percent = 100.0
    job.stage = "complete"
    job.finished_at = datetime.now(UTC)
    db.add(job)
    db.commit()


def fail(db: Session, job: Job, *, error: str, retryable: bool = False) -> None:
    if retryable and job.attempts < job.max_attempts:
        job.status = JobStatus.QUEUED
        job.stage = "retry pending"
    else:
        job.status = JobStatus.FAILED
        job.stage = "failed"
        job.finished_at = datetime.now(UTC)
    job.error = error
    db.add(job)
    db.commit()


def cancel(db: Session, job: Job) -> None:
    job.cancel_requested = True
    if job.status == JobStatus.QUEUED:
        job.status = JobStatus.CANCELLED
        job.stage = "cancelled"
        job.finished_at = datetime.now(UTC)
    db.add(job)
    db.commit()
