"""Production feedback, inspection, variance and governed learning."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, require, scoped, tenant_project
from app.core.rbac import Permission
from app.models.production import InspectionResult, MachineRun, RuleProposal
from app.models.verification import Release
from app.schemas.api import InspectionCreate, MachineRunCreate, ProposalReview
from app.services import learning

router = APIRouter(tags=["production"])


@router.post("/machine-runs", status_code=201, summary="Record an actual production run")
def record_run(
    payload: MachineRunCreate,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.RUN_RECORD))],
) -> dict[str, Any]:
    """Actuals link to the exact released NC program; an unmatched program is refused."""
    release = scoped(db, user, Release, payload.release_id, "Release")
    run = learning.record_run(
        db,
        release=release,
        nc_program_id=payload.nc_program_id,
        payload=payload.model_dump(),
        actor_id=user.id,
    )
    db.commit()
    db.refresh(run)
    return {
        "id": run.id,
        "release_id": run.release_id,
        "nc_program_id": run.nc_program_id,
        "nc_program_hash": run.nc_program_hash,
        "actual_cycle_seconds": run.actual_cycle_seconds,
        "result": run.result,
        "telemetry_suspect": run.telemetry_suspect,
    }


@router.get("/projects/{project_id}/machine-runs", summary="Production run history")
def list_runs(project_id: str, db: DbSession, user: CurrentUser) -> list[dict[str, Any]]:
    tenant_project(db, user, project_id)
    return [
        {
            "id": r.id,
            "release_id": r.release_id,
            "nc_program_id": r.nc_program_id,
            "operator": r.operator,
            "started_at": r.started_at,
            "ended_at": r.ended_at,
            "actual_setup_seconds": r.actual_setup_seconds,
            "actual_cycle_seconds": r.actual_cycle_seconds,
            "actual_tool_changes": r.actual_tool_changes,
            "alarms": r.alarms,
            "tool_outcomes": r.tool_outcomes,
            "scrap_count": r.scrap_count,
            "pieces": r.pieces,
            "result": r.result,
            "telemetry_suspect": r.telemetry_suspect,
            "notes": r.notes,
        }
        for r in db.execute(
            select(MachineRun).where(MachineRun.project_id == project_id).order_by(MachineRun.created_at.desc())
        ).scalars()
    ]


@router.post("/inspection-results", status_code=201, summary="Record a measurement against a feature")
def record_inspection(
    payload: InspectionCreate,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.RUN_RECORD))],
) -> dict[str, Any]:
    project = tenant_project(db, user, payload.project_id)
    run = scoped(db, user, MachineRun, payload.machine_run_id, "Machine run") if payload.machine_run_id else None
    result = learning.record_inspection(db, project=project, run=run, payload=payload.model_dump(), actor_id=user.id)
    db.commit()
    db.refresh(result)
    return {
        "id": result.id,
        "feature_key": result.feature_key,
        "characteristic": result.characteristic,
        "nominal": result.nominal,
        "actual": result.actual,
        "in_tolerance": result.in_tolerance,
        "disposition": result.disposition,
    }


@router.get("/projects/{project_id}/inspection-results", summary="Inspection history")
def list_inspections(project_id: str, db: DbSession, user: CurrentUser) -> list[dict[str, Any]]:
    tenant_project(db, user, project_id)
    return [
        {
            "id": i.id,
            "machine_run_id": i.machine_run_id,
            "feature_key": i.feature_key,
            "characteristic": i.characteristic,
            "nominal": i.nominal,
            "actual": i.actual,
            "tolerance_plus": i.tolerance_plus,
            "tolerance_minus": i.tolerance_minus,
            "unit": i.unit,
            "instrument": i.instrument,
            "in_tolerance": i.in_tolerance,
            "disposition": i.disposition,
        }
        for i in db.execute(
            select(InspectionResult).where(InspectionResult.project_id == project_id)
        ).scalars()
    ]


@router.get("/projects/{project_id}/variance", summary="Predicted against actual")
def variance(project_id: str, db: DbSession, user: CurrentUser) -> dict[str, Any]:
    """Reported only. No production rule changes without an approved proposal."""
    project = tenant_project(db, user, project_id)
    return learning.variance_report(db, project=project)


@router.post("/projects/{project_id}/rule-proposals", status_code=201, summary="Derive governed rule proposals")
def propose(
    project_id: str,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.RUN_RECORD))],
) -> list[dict[str, Any]]:
    project = tenant_project(db, user, project_id)
    proposals = learning.propose_rules(db, project=project, actor_id=user.id)
    db.commit()
    return [
        {
            "id": p.id,
            "scope": p.scope,
            "target_ref": p.target_ref,
            "current_value": p.current_value,
            "proposed_value": p.proposed_value,
            "sample_size": p.sample_size,
            "expected_impact": p.expected_impact,
            "validation_state": p.validation_state,
            "status": p.status,
        }
        for p in proposals
    ]


@router.get("/rule-proposals", summary="List rule proposals")
def list_proposals(db: DbSession, user: CurrentUser, status: str | None = None) -> list[dict[str, Any]]:
    query = select(RuleProposal).where(RuleProposal.tenant_id == user.tenant_id)
    if status:
        query = query.where(RuleProposal.status == status)
    return [
        {
            "id": p.id,
            "project_id": p.project_id,
            "scope": p.scope,
            "target_ref": p.target_ref,
            "current_value": p.current_value,
            "proposed_value": p.proposed_value,
            "evidence_count": len(p.evidence or []),
            "sample_size": p.sample_size,
            "expected_impact": p.expected_impact,
            "validation_state": p.validation_state,
            "status": p.status,
            "review_note": p.review_note,
            "created_at": p.created_at,
        }
        for p in db.execute(query.order_by(RuleProposal.created_at.desc())).scalars()
    ]


@router.post("/rule-proposals/{proposal_id}/review", summary="Promote or reject a proposal")
def review_proposal(
    proposal_id: str,
    payload: ProposalReview,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.RULE_PROMOTE))],
) -> dict[str, Any]:
    """Promotion is the only path by which a learned value becomes a default."""
    proposal = scoped(db, user, RuleProposal, proposal_id, "Rule proposal")
    learning.review_proposal(db, proposal, approve=payload.approve, actor_id=user.id, note=payload.note)
    db.commit()
    db.refresh(proposal)
    return {"id": proposal.id, "status": proposal.status, "validation_state": proposal.validation_state}
