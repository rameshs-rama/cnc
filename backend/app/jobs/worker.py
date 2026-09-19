"""Compute job worker.

Long-running engineering work runs here, not in a request. Each handler gets a
fresh session, reports stage and percent, and honours a cancellation request at
the next checkpoint (PRD 6.1, long-running jobs).
"""

from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.core import events
from app.core.enums import JobKind, JobStatus, ProjectState
from app.db import SessionLocal
from app.jobs import queue
from app.models.factory import FixtureVersion, MachineVersion, Material, PostProcessorVersion
from app.models.geometry import GeometryVersion
from app.models.planning import ManufacturingPlan
from app.models.platform import Job
from app.models.project import PartProject, SourceArtifact
from app.models.verification import SimulationRun
from app.services import evidence, planning, postprocess, verification, workflow
from app.services import geometry as geometry_service


class JobCancelled(RuntimeError):
    """Raised at a checkpoint when the operator asked the job to stop."""


def _progress(db, job: Job) -> Callable[[str, float, str], None]:
    def report(stage: str, percent: float, message: str = "") -> None:
        db.refresh(job)
        if job.cancel_requested:
            raise JobCancelled(f"Cancelled during {stage}")
        queue.progress(db, job, stage=stage, percent=percent, message=message)
        events.publish(
            db,
            tenant_id=job.tenant_id,
            topic=events.Topic.JOB_PROGRESS,
            project_id=job.project_id,
            payload={"job_id": job.id, "kind": job.kind, "stage": stage, "percent": percent, "message": message},
        )
        db.commit()

    return report


# --------------------------------------------------------------------- handlers
def _handle_artifact_parse(db, job: Job) -> dict[str, Any]:
    report = _progress(db, job)
    project = db.get(PartProject, job.project_id)
    artifact = db.get(SourceArtifact, job.payload["artifact_id"])
    report("importing observations", 40, artifact.filename)
    created = evidence.import_extracted_observations(
        db, project=project, artifact=artifact, actor_id=job.requested_by_id
    )
    workflow.advance_at_least(db, project, ProjectState.EVIDENCE_COLLECTION, actor_id=job.requested_by_id)
    db.commit()
    return {"artifact_id": artifact.id, "observations_created": len(created)}


def _handle_reconstruction(db, job: Job) -> dict[str, Any]:
    report = _progress(db, job)
    project = db.get(PartProject, job.project_id)
    artifact_ids = job.payload.get("artifact_ids") or []
    artifacts = [db.get(SourceArtifact, a) for a in artifact_ids]
    artifacts = [a for a in artifacts if a is not None]
    if not artifacts:
        artifacts = list(
            db.execute(select(SourceArtifact).where(SourceArtifact.project_id == project.id)).scalars()
        )

    parent = db.get(GeometryVersion, job.payload["parent_id"]) if job.payload.get("parent_id") else None
    workflow.advance_at_least(db, project, ProjectState.RECONSTRUCTION, actor_id=job.requested_by_id)
    db.commit()

    version = geometry_service.reconstruct(
        db,
        project=project,
        artifacts=artifacts,
        known_dimensions=job.payload.get("known_dimensions") or {},
        parent=parent,
        actor_id=job.requested_by_id,
        progress=lambda stage, percent, message="": report(stage, percent, message),
    )
    workflow.advance_at_least(db, project, ProjectState.ENGINEERING_REVIEW, actor_id=job.requested_by_id)
    db.commit()
    return {
        "geometry_version_id": version.id,
        "revision": version.revision,
        "scale_established": version.scale_established,
        "feature_count": len(version.part_model.get("features", [])),
    }


def _handle_plan_generate(db, job: Job) -> dict[str, Any]:
    report = _progress(db, job)
    project = db.get(PartProject, job.project_id)
    geometry = db.get(GeometryVersion, job.payload["geometry_version_id"])
    material = db.get(Material, job.payload["material_id"])
    fixture = db.get(FixtureVersion, job.payload["fixture_id"]) if job.payload.get("fixture_id") else None
    machine_ids = job.payload.get("machine_ids") or []
    machines = [db.get(MachineVersion, m) for m in machine_ids]
    machines = [m for m in machines if m is not None]
    if not machines:
        machines = list(
            db.execute(
                select(MachineVersion).where(
                    MachineVersion.tenant_id == project.tenant_id, MachineVersion.status == "Current"
                )
            ).scalars()
        )

    workflow.advance_at_least(db, project, ProjectState.PLANNING, actor_id=job.requested_by_id)
    db.commit()

    plans = planning.generate_plans(
        db,
        project=project,
        geometry=geometry,
        material=material,
        fixture=fixture,
        machines=machines,
        objective=job.payload.get("objective", "balanced"),
        actor_id=job.requested_by_id,
        progress=report,
    )
    db.commit()
    return {"plan_ids": [p.id for p in plans], "count": len(plans)}


def _handle_toolpath_generate(db, job: Job) -> dict[str, Any]:
    report = _progress(db, job)
    plan = db.get(ManufacturingPlan, job.payload["plan_id"])
    project = db.get(PartProject, plan.project_id)
    workflow.advance_at_least(db, project, ProjectState.CAM_GENERATION, actor_id=job.requested_by_id)
    db.commit()

    paths = planning.generate_toolpaths(db, plan=plan, actor_id=job.requested_by_id, progress=report)
    db.commit()
    return {
        "plan_id": plan.id,
        "toolpath_count": len(paths),
        "total_moves": sum(p.move_count for p in paths),
        "cutting_length_mm": round(sum(p.cutting_length_mm for p in paths), 1),
    }


def _handle_simulation(db, job: Job) -> dict[str, Any]:
    report = _progress(db, job)
    plan = db.get(ManufacturingPlan, job.payload["plan_id"])
    project = db.get(PartProject, plan.project_id)
    workflow.advance_at_least(db, project, ProjectState.SIMULATION, actor_id=job.requested_by_id)
    db.commit()

    run = verification.run_simulation(
        db,
        plan=plan,
        voxel_pitch=float(job.payload.get("voxel_pitch", 1.0)),
        tolerance_mm=float(job.payload.get("tolerance_mm", 0.05)),
        actor_id=job.requested_by_id,
        progress=report,
    )
    if run.passed:
        workflow.advance_at_least(db, project, ProjectState.OPTIMIZATION, actor_id=job.requested_by_id)
    db.commit()
    return {
        "simulation_id": run.id,
        "passed": run.passed,
        "cycle_time_seconds": run.cycle_time_seconds,
        "event_counts": run.event_counts,
        "max_severity": run.max_severity,
    }


def _handle_postprocess(db, job: Job) -> dict[str, Any]:
    report = _progress(db, job)
    plan = db.get(ManufacturingPlan, job.payload["plan_id"])
    simulation = db.get(SimulationRun, job.payload["simulation_id"])
    post = db.get(PostProcessorVersion, job.payload["post_id"])
    project = db.get(PartProject, plan.project_id)

    report("postprocessing", 40, f"{post.code} r{post.revision}")
    workflow.advance_at_least(db, project, ProjectState.POSTPROCESSING, actor_id=job.requested_by_id)
    db.commit()

    program = postprocess.postprocess(
        db,
        plan=plan,
        simulation=simulation,
        post=post,
        program_number=job.payload.get("program_number"),
        actor_id=job.requested_by_id,
    )
    report("validating", 85, f"{program.line_count} blocks")
    if program.validation_passed:
        workflow.advance_at_least(db, project, ProjectState.RELEASE_REVIEW, actor_id=job.requested_by_id)
    db.commit()
    return {
        "nc_program_id": program.id,
        "program_number": program.program_number,
        "line_count": program.line_count,
        "validation_passed": program.validation_passed,
        "max_severity": program.max_severity,
    }


HANDLERS: dict[str, Callable[[Any, Job], dict[str, Any]]] = {
    JobKind.ARTIFACT_PARSE: _handle_artifact_parse,
    JobKind.RECONSTRUCTION: _handle_reconstruction,
    JobKind.PLAN_GENERATE: _handle_plan_generate,
    JobKind.TOOLPATH_GENERATE: _handle_toolpath_generate,
    JobKind.SIMULATION: _handle_simulation,
    JobKind.POSTPROCESS: _handle_postprocess,
}


def run_job(job_id: str) -> None:
    """Execute one job to completion. Used by the worker loop and by tests."""
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None or job.status != JobStatus.RUNNING:
            return
        handler = HANDLERS.get(job.kind)
        if handler is None:
            queue.fail(db, job, error=f"No handler is registered for job kind {job.kind}")
            return
        try:
            result = handler(db, job)
            queue.finish(db, job, result=result)
        except JobCancelled as cancelled:
            db.rollback()
            job = db.get(Job, job_id)
            job.status = JobStatus.CANCELLED
            job.stage = "cancelled"
            job.error = str(cancelled)
            db.add(job)
            db.commit()
        except Exception as exc:  # noqa: BLE001 - a failed job must not kill the worker
            db.rollback()
            job = db.get(Job, job_id)
            detail = f"{type(exc).__name__}: {exc}"
            queue.progress(db, job, stage="failed", percent=job.percent, message=traceback.format_exc()[-1500:], role="administrator")
            queue.fail(db, job, error=detail, retryable=isinstance(exc, TimeoutError))
    finally:
        db.close()


class Worker(threading.Thread):
    """Background thread that drains the job queue.

    Runs in-process for the pilot profile. The same entry point is used by the
    standalone worker container when compute is scaled separately.
    """

    def __init__(self, name: str = "mip-worker") -> None:
        super().__init__(name=name, daemon=True)
        self._stop = threading.Event()
        self.settings = get_settings()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:  # pragma: no cover - thread loop
        while not self._stop.is_set():
            db = SessionLocal()
            try:
                job = queue.claim_next(db)
            finally:
                db.close()
            if job is None:
                time.sleep(self.settings.job_poll_seconds)
                continue
            run_job(job.id)


def drain(limit: int = 100) -> int:
    """Run queued jobs synchronously. Used by tests and the seeding CLI."""
    processed = 0
    while processed < limit:
        db = SessionLocal()
        try:
            job = queue.claim_next(db)
        finally:
            db.close()
        if job is None:
            break
        run_job(job.id)
        processed += 1
    return processed
