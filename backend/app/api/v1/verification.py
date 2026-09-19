"""Simulation, costing, postprocessing and release."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, require, scoped, tenant_project
from app.core.enums import JobKind
from app.core.errors import ValidationFailed
from app.core.rbac import Permission
from app.core.storage import get_store
from app.jobs import queue
from app.models.factory import PostProcessorVersion
from app.models.planning import ManufacturingPlan
from app.models.verification import (
    Approval,
    CostEstimate,
    GateEvaluationRecord,
    NCProgram,
    Release,
    SimulationRun,
)
from app.schemas.api import (
    CostOut,
    CostRequest,
    EventDisposition,
    JobOut,
    NCProgramOut,
    PostprocessRequest,
    ReleaseApproveRequest,
    ReleaseOut,
    ReleasePrepareRequest,
    SimulationOut,
    SimulationRequest,
)
from app.schemas.common import ActionResult
from app.services import economics, policy
from app.services import release as release_service
from app.services import verification as verification_service

router = APIRouter(tags=["verification"])


# ------------------------------------------------------------------ simulation
@router.post("/simulations", response_model=JobOut, status_code=202, summary="Run stock and machine verification")
def run_simulation(
    payload: SimulationRequest,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.SIMULATION_RUN))],
) -> Any:
    plan = scoped(db, user, ManufacturingPlan, payload.plan_id, "Plan")
    job = queue.enqueue(
        db,
        tenant_id=user.tenant_id,
        kind=JobKind.SIMULATION.value,
        project_id=plan.project_id,
        payload={
            "plan_id": plan.id,
            "voxel_pitch": payload.voxel_pitch,
            "tolerance_mm": payload.tolerance_mm,
        },
        requested_by_id=user.id,
    )
    db.commit()
    db.refresh(job)
    return job


@router.get("/simulations/{simulation_id}", response_model=SimulationOut, summary="Read a simulation result")
def get_simulation(simulation_id: str, db: DbSession, user: CurrentUser) -> SimulationRun:
    return scoped(db, user, SimulationRun, simulation_id, "Simulation")


@router.get("/simulations/{simulation_id}/stock", summary="Remaining stock height field for the viewer")
def simulation_stock(simulation_id: str, db: DbSession, user: CurrentUser) -> dict[str, Any]:
    run = scoped(db, user, SimulationRun, simulation_id, "Simulation")
    return {
        "simulation_id": run.id,
        "remaining_stock": run.remaining_stock,
        "stock_comparison": run.stock_comparison,
        "programmed_envelope": run.programmed_envelope,
    }


@router.get("/plans/{plan_id}/simulations", response_model=list[SimulationOut], summary="Simulation history")
def list_simulations(plan_id: str, db: DbSession, user: CurrentUser) -> list[SimulationRun]:
    plan = scoped(db, user, ManufacturingPlan, plan_id, "Plan")
    return list(
        db.execute(
            select(SimulationRun).where(SimulationRun.plan_id == plan.id).order_by(SimulationRun.created_at.desc())
        ).scalars()
    )


@router.post("/simulations/{simulation_id}/dispositions", summary="Disposition a simulation finding")
def disposition_event(
    simulation_id: str,
    payload: EventDisposition,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.SIMULATION_DISPOSITION))],
) -> dict[str, Any]:
    """An S1 stop can be acknowledged but never resolved away (PRD 5.3)."""
    run = scoped(db, user, SimulationRun, simulation_id, "Simulation")
    verification_service.disposition_event(
        db,
        run,
        event_code=payload.event_code,
        decision=payload.decision,
        actor_id=user.id,
        reason=payload.reason,
    )
    db.commit()
    db.refresh(run)
    return {"simulation_id": run.id, "dispositions": run.dispositions, "gate": policy.evaluate_simulation(db, run).to_dict()}


@router.get("/simulations/{simulation_id}/gate", summary="Evaluate the simulation pass gate")
def simulation_gate(simulation_id: str, db: DbSession, user: CurrentUser) -> dict[str, Any]:
    run = scoped(db, user, SimulationRun, simulation_id, "Simulation")
    return policy.evaluate_simulation(db, run).to_dict()


# --------------------------------------------------------------------- costing
@router.post("/cost-estimates", response_model=CostOut, status_code=201, summary="Estimate cost for a plan")
def create_cost(
    payload: CostRequest,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.COST_EDIT))],
) -> CostEstimate:
    plan = scoped(db, user, ManufacturingPlan, payload.plan_id, "Plan")
    simulation = scoped(db, user, SimulationRun, payload.simulation_id, "Simulation")
    record = economics.estimate(
        db,
        plan=plan,
        simulation=simulation,
        quantity=payload.quantity,
        rate_overrides=payload.rate_overrides,
        actor_id=user.id,
    )
    db.commit()
    db.refresh(record)
    return record


@router.get("/plans/{plan_id}/cost-estimates", response_model=list[CostOut], summary="Cost history for a plan")
def list_costs(plan_id: str, db: DbSession, user: CurrentUser) -> list[CostEstimate]:
    plan = scoped(db, user, ManufacturingPlan, plan_id, "Plan")
    return list(
        db.execute(
            select(CostEstimate).where(CostEstimate.plan_id == plan.id).order_by(CostEstimate.created_at.desc())
        ).scalars()
    )


# --------------------------------------------------------------- postprocessing
@router.post("/nc-programs:postprocess", response_model=JobOut, status_code=202, summary="Generate an NC candidate")
def postprocess(
    payload: PostprocessRequest,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.NC_POSTPROCESS))],
) -> Any:
    """Requires a passing simulation and a certified machine and post pair."""
    plan = scoped(db, user, ManufacturingPlan, payload.plan_id, "Plan")
    scoped(db, user, SimulationRun, payload.simulation_id, "Simulation")
    scoped(db, user, PostProcessorVersion, payload.post_id, "Post")
    job = queue.enqueue(
        db,
        tenant_id=user.tenant_id,
        kind=JobKind.POSTPROCESS.value,
        project_id=plan.project_id,
        payload={
            "plan_id": payload.plan_id,
            "simulation_id": payload.simulation_id,
            "post_id": payload.post_id,
            "program_number": payload.program_number,
        },
        requested_by_id=user.id,
    )
    db.commit()
    db.refresh(job)
    return job


@router.get("/nc-programs/{program_id}", response_model=NCProgramOut, summary="Read an NC program record")
def get_program(program_id: str, db: DbSession, user: CurrentUser) -> NCProgram:
    return scoped(db, user, NCProgram, program_id, "NC program")


@router.get("/nc-programs/{program_id}/text", summary="Read the NC program text")
def get_program_text(program_id: str, db: DbSession, user: CurrentUser):
    """Anything not in a Released state is watermarked as an uncontrolled copy."""
    program = scoped(db, user, NCProgram, program_id, "NC program")
    body = get_store().get_bytes(program.storage_key).decode()
    released = program.status == "Approved"
    if not released:
        body = "(UNCONTROLLED COPY - NOT FOR PRODUCTION)\n" + body
    return Response(
        content=body,
        media_type="text/plain; charset=utf-8",
        headers={
            "X-Program-Hash": program.program_hash,
            "X-Controlled": "true" if released else "false",
            "Content-Disposition": f'inline; filename="{program.program_number}.nc"',
        },
    )


@router.get("/nc-programs/{program_id}/ir", summary="Read the manufacturing IR the program was built from")
def get_program_ir(program_id: str, db: DbSession, user: CurrentUser):
    program = scoped(db, user, NCProgram, program_id, "NC program")
    return Response(
        content=get_store().get_bytes(program.ir_storage_key),
        media_type="application/json",
        headers={"X-IR-Hash": program.ir_hash},
    )


@router.get("/plans/{plan_id}/nc-programs", response_model=list[NCProgramOut], summary="NC programs for a plan")
def list_programs(plan_id: str, db: DbSession, user: CurrentUser) -> list[NCProgram]:
    plan = scoped(db, user, ManufacturingPlan, plan_id, "Plan")
    return list(
        db.execute(select(NCProgram).where(NCProgram.plan_id == plan.id).order_by(NCProgram.created_at.desc())).scalars()
    )


# --------------------------------------------------------------------- release
@router.post("/releases", response_model=ReleaseOut, status_code=201, summary="Prepare a release candidate")
def prepare_release(
    payload: ReleasePrepareRequest,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.NC_POSTPROCESS))],
) -> Release:
    plan = scoped(db, user, ManufacturingPlan, payload.plan_id, "Plan")
    simulation = scoped(db, user, SimulationRun, payload.simulation_id, "Simulation")
    project = tenant_project(db, user, plan.project_id)
    programs = [scoped(db, user, NCProgram, pid, "NC program") for pid in payload.nc_program_ids]

    release = release_service.prepare(
        db, project=project, plan=plan, simulation=simulation, nc_programs=programs, actor_id=user.id
    )
    db.commit()
    db.refresh(release)
    return release


@router.get("/releases/{release_id}", response_model=ReleaseOut, summary="Read a release")
def get_release(release_id: str, db: DbSession, user: CurrentUser) -> Release:
    return scoped(db, user, Release, release_id, "Release")


@router.get("/projects/{project_id}/releases", response_model=list[ReleaseOut], summary="Release history")
def list_releases(project_id: str, db: DbSession, user: CurrentUser) -> list[Release]:
    tenant_project(db, user, project_id)
    return list(
        db.execute(select(Release).where(Release.project_id == project_id).order_by(Release.revision.desc())).scalars()
    )


@router.get("/releases/{release_id}/checklist", summary="The checklist an approver must sign")
def release_checklist(release_id: str, db: DbSession, user: CurrentUser) -> dict[str, Any]:
    release = scoped(db, user, Release, release_id, "Release")
    return {
        "release_id": release.id,
        "status": release.status,
        "items": release_service.release_checklist(db, release),
        "gate": (release.gate_results or [{}])[-1],
    }


@router.post("/releases/{release_id}:approve", response_model=ReleaseOut, summary="Sign the release")
def approve_release(
    release_id: str,
    payload: ReleaseApproveRequest,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.NC_RELEASE))],
) -> Release:
    """Requires the release role, a verified second factor and a signed checklist."""
    release = scoped(db, user, Release, release_id, "Release")
    release_service.approve(
        db,
        release=release,
        approver=user,
        totp_code=payload.totp_code,
        statement=payload.statement,
        checklist=payload.checklist,
    )
    db.commit()
    db.refresh(release)
    return release


@router.post("/releases/{release_id}:reject", response_model=ActionResult, summary="Reject a release candidate")
def reject_release(
    release_id: str,
    payload: dict[str, Any],
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.NC_RELEASE))],
) -> ActionResult:
    release = scoped(db, user, Release, release_id, "Release")
    reason = str(payload.get("reason", "")).strip()
    if not reason:
        raise ValidationFailed("A rejection requires a reason", code="reason_required")
    release_service.reject(db, release, actor_id=user.id, reason=reason)
    db.commit()
    return ActionResult(message="Release rejected", detail={"release_id": release.id})


@router.get("/releases/{release_id}/package", summary="Download the release package")
def download_package(release_id: str, db: DbSession, user: CurrentUser):
    """Released packages are signed; anything else is marked uncontrolled."""
    release = scoped(db, user, Release, release_id, "Release")
    data, filename, controlled = release_service.package_bytes(db, release)
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Package-Hash": release.package_hash or "",
            "X-Controlled": "true" if controlled else "false",
            "X-Signature": release.package_signature or "",
        },
    )


@router.get("/projects/{project_id}/gates", summary="Gate evaluation history")
def gate_history(project_id: str, db: DbSession, user: CurrentUser) -> list[dict[str, Any]]:
    tenant_project(db, user, project_id)
    rows = db.execute(
        select(GateEvaluationRecord)
        .where(GateEvaluationRecord.project_id == project_id)
        .order_by(GateEvaluationRecord.created_at.desc())
        .limit(100)
    ).scalars()
    return [
        {
            "id": r.id,
            "gate": r.gate,
            "target_kind": r.target_kind,
            "target_id": r.target_id,
            "passed": r.passed,
            "blocks": r.blocks,
            "max_severity": r.max_severity,
            "findings": r.findings,
            "evaluated_by_id": r.evaluated_by_id,
            "created_at": r.created_at,
        }
        for r in rows
    ]


@router.get("/projects/{project_id}/approvals", summary="Signature history")
def approvals(project_id: str, db: DbSession, user: CurrentUser) -> list[dict[str, Any]]:
    tenant_project(db, user, project_id)
    rows = db.execute(
        select(Approval).where(Approval.project_id == project_id).order_by(Approval.created_at.desc())
    ).scalars()
    return [
        {
            "id": a.id,
            "gate": a.gate,
            "target_kind": a.target_kind,
            "target_id": a.target_id,
            "target_hash": a.target_hash,
            "actor_id": a.actor_id,
            "actor_role": a.actor_role,
            "decision": a.decision,
            "statement": a.statement,
            "mfa_verified": a.mfa_verified,
            "signature": a.signature,
            "created_at": a.created_at,
        }
        for a in rows
    ]
