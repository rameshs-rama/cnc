"""Production feedback and governed learning (FR-FBK-001 to FR-FBK-003).

Actuals are recorded against the exact released program. Variance is shown.
Nothing changes a production rule without an approval: a learned suggestion is
a proposal until a manufacturing engineer promotes it.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import audit, events
from app.core.enums import ProjectState, ProposalStatus, ReleaseStatus, RunResult
from app.core.errors import ValidationFailed
from app.models.factory import Material
from app.models.planning import ManufacturingPlan
from app.models.production import InspectionResult, MachineRun, RuleProposal
from app.models.project import PartProject
from app.models.verification import NCProgram, Release, SimulationRun
from app.services import workflow

#: Number of comparable runs before a variance is worth proposing a rule from.
MIN_SAMPLE = 3
#: Relative variance below which the model is considered calibrated.
CALIBRATION_BAND = 0.15


def record_run(
    db: Session,
    *,
    release: Release,
    nc_program_id: str,
    payload: dict[str, Any],
    actor_id: str,
) -> MachineRun:
    """Attach a production record to the exact released NC program."""
    if release.status not in (ReleaseStatus.RELEASED.value, ReleaseStatus.SUPERSEDED.value):
        raise ValidationFailed(
            "Production can only be recorded against a released program", code="release_not_released"
        )
    if nc_program_id not in (release.nc_program_ids or []):
        raise ValidationFailed(
            "The program is not part of this release; telemetry is never attached to an approximate match",
            code="program_not_in_release",
        )
    program = db.get(NCProgram, nc_program_id)
    if program is None:
        raise ValidationFailed("NC program not found", code="program_missing")

    supplied_hash = payload.get("nc_program_hash")
    suspect = bool(supplied_hash and supplied_hash != program.program_hash)

    run = MachineRun(
        tenant_id=release.tenant_id,
        project_id=release.project_id,
        release_id=release.id,
        nc_program_id=program.id,
        nc_program_hash=program.program_hash,
        started_at=_parse_time(payload.get("started_at")),
        ended_at=_parse_time(payload.get("ended_at")),
        operator=payload.get("operator", ""),
        actual_setup_seconds=payload.get("actual_setup_seconds"),
        actual_cycle_seconds=payload.get("actual_cycle_seconds"),
        actual_tool_changes=payload.get("actual_tool_changes"),
        alarms=payload.get("alarms", []),
        tool_outcomes=payload.get("tool_outcomes", []),
        scrap_count=int(payload.get("scrap_count", 0)),
        pieces=int(payload.get("pieces", 1)),
        result=payload.get("result", RunResult.PENDING.value),
        telemetry_suspect=suspect,
        notes=payload.get("notes"),
    )
    db.add(run)
    db.flush()

    project = db.get(PartProject, release.project_id)
    if project and project.state == ProjectState.RELEASED.value:
        workflow.advance_at_least(db, project, ProjectState.IN_PRODUCTION, actor_id=actor_id)

    audit.record(
        db,
        tenant_id=release.tenant_id,
        object_kind="machine_run",
        object_id=run.id,
        action="record",
        actor_id=actor_id,
        after={"nc_program": program.program_number, "result": run.result, "suspect": suspect},
    )
    events.publish(
        db,
        tenant_id=release.tenant_id,
        topic=events.Topic.MACHINE_RUN_COMPLETED,
        project_id=release.project_id,
        actor_id=actor_id,
        payload={
            "run_id": run.id,
            "released_nc_id": program.id,
            "actual_cycle_seconds": run.actual_cycle_seconds,
            "result": run.result,
            "telemetry_suspect": suspect,
        },
    )
    return run


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def record_inspection(
    db: Session, *, project: PartProject, run: MachineRun | None, payload: dict[str, Any], actor_id: str
) -> InspectionResult:
    nominal = float(payload["nominal"])
    actual = float(payload["actual"])
    plus = float(payload.get("tolerance_plus", 0.05))
    minus = float(payload.get("tolerance_minus", 0.05))
    in_tolerance = (nominal - minus) <= actual <= (nominal + plus)

    result = InspectionResult(
        tenant_id=project.tenant_id,
        project_id=project.id,
        machine_run_id=run.id if run else None,
        feature_key=payload["feature_key"],
        characteristic=payload.get("characteristic", "diameter"),
        nominal=nominal,
        tolerance_plus=plus,
        tolerance_minus=minus,
        actual=actual,
        unit=payload.get("unit", "mm"),
        instrument=payload.get("instrument", {}),
        in_tolerance=in_tolerance,
        disposition=payload.get("disposition", "Accept" if in_tolerance else "Review"),
        inspector_id=actor_id,
    )
    db.add(result)
    db.flush()

    if run and not in_tolerance and run.result == RunResult.PENDING.value:
        run.result = RunResult.REWORK.value
        db.add(run)
    return result


def variance_report(db: Session, *, project: PartProject) -> dict[str, Any]:
    """Predicted against actual, with nothing applied automatically (FR-FBK-002)."""
    runs = db.execute(
        select(MachineRun).where(MachineRun.project_id == project.id).order_by(MachineRun.created_at)
    ).scalars().all()

    rows: list[dict[str, Any]] = []
    ratios: list[float] = []
    for run in runs:
        release = db.get(Release, run.release_id)
        plan = db.get(ManufacturingPlan, release.plan_id) if release else None
        program = db.get(NCProgram, run.nc_program_id)
        simulation = db.get(SimulationRun, program.simulation_run_id) if program else None
        predicted = simulation.cycle_time_seconds if simulation else None
        actual = run.actual_cycle_seconds
        variance = None
        if predicted and actual:
            variance = (actual - predicted) / predicted
            if not run.telemetry_suspect:
                ratios.append(variance)
        rows.append(
            {
                "run_id": run.id,
                "release_revision": release.revision if release else None,
                "machine_plan": plan.label if plan else None,
                "program": program.program_number if program else None,
                "predicted_cycle_s": predicted,
                "actual_cycle_s": actual,
                "variance": round(variance, 4) if variance is not None else None,
                "variance_percent": round(variance * 100, 2) if variance is not None else None,
                "result": run.result,
                "alarms": len(run.alarms or []),
                "scrap": run.scrap_count,
                "telemetry_suspect": run.telemetry_suspect,
            }
        )

    inspections = db.execute(
        select(InspectionResult).where(InspectionResult.project_id == project.id)
    ).scalars().all()
    failed = [i for i in inspections if not i.in_tolerance]

    summary: dict[str, Any] = {
        "run_count": len(runs),
        "sample_size": len(ratios),
        "median_variance_percent": round(statistics.median(ratios) * 100, 2) if ratios else None,
        "mean_variance_percent": round(statistics.fmean(ratios) * 100, 2) if ratios else None,
        "within_calibration_band": bool(ratios) and abs(statistics.median(ratios)) <= CALIBRATION_BAND,
        "calibration_band_percent": CALIBRATION_BAND * 100,
        "inspection_count": len(inspections),
        "inspection_failures": len(failed),
        "failed_features": sorted({i.feature_key for i in failed}),
        "note": (
            "Variance is reported only. No production rule changes without an approved proposal."
        ),
    }
    return {"summary": summary, "runs": rows}


def propose_rules(db: Session, *, project: PartProject, actor_id: str | None = None) -> list[RuleProposal]:
    """Derive governed proposals from observed variance.

    A proposal is created only with enough comparable runs and a variance
    outside the calibration band; it enters shadow validation and stays
    Proposed until approved (FR-FBK-003).
    """
    report = variance_report(db, project=project)
    summary = report["summary"]
    proposals: list[RuleProposal] = []

    if summary["sample_size"] >= MIN_SAMPLE and not summary["within_calibration_band"]:
        median = summary["median_variance_percent"] / 100.0
        plan = db.execute(
            select(ManufacturingPlan).where(ManufacturingPlan.project_id == project.id).limit(1)
        ).scalar_one_or_none()
        material = db.get(Material, plan.material_id) if plan and plan.material_id else None
        proposal = RuleProposal(
            tenant_id=project.tenant_id,
            project_id=project.id,
            scope="cycle_time_calibration",
            target_ref=f"machine:{plan.machine_version_id}" if plan else "machine:unknown",
            current_value={"correction_factor": 1.0},
            proposed_value={"correction_factor": round(1.0 + median, 4)},
            evidence=report["runs"],
            sample_size=summary["sample_size"],
            expected_impact={
                "metric": "cycle time prediction error",
                "current_median_percent": summary["median_variance_percent"],
                "target_band_percent": CALIBRATION_BAND * 100,
            },
            validation_state="shadow",
            status=ProposalStatus.PROPOSED.value,
        )
        db.add(proposal)
        proposals.append(proposal)
        if material:
            proposal.target_ref += f"|material:{material.code}"

    if summary["inspection_failures"] and summary["failed_features"]:
        proposal = RuleProposal(
            tenant_id=project.tenant_id,
            project_id=project.id,
            scope="finishing_allowance",
            target_ref=f"features:{','.join(summary['failed_features'][:5])}",
            current_value={"stock_to_leave_mm": 0.0},
            proposed_value={"stock_to_leave_mm": 0.05},
            evidence=[
                {"feature": key, "note": "Inspection outside tolerance on a released program"}
                for key in summary["failed_features"]
            ],
            sample_size=summary["inspection_failures"],
            expected_impact={"metric": "inspection conformance", "current_failures": summary["inspection_failures"]},
            validation_state="shadow",
            status=ProposalStatus.PROPOSED.value,
        )
        db.add(proposal)
        proposals.append(proposal)

    db.flush()
    for proposal in proposals:
        events.publish(
            db,
            tenant_id=project.tenant_id,
            topic=events.Topic.LEARNING_RULE_PROPOSED,
            project_id=project.id,
            actor_id=actor_id,
            payload={
                "proposal_id": proposal.id,
                "scope": proposal.scope,
                "evidence_count": proposal.sample_size,
                "expected_impact": proposal.expected_impact,
                "validation_state": proposal.validation_state,
            },
        )
    return proposals


def review_proposal(
    db: Session, proposal: RuleProposal, *, approve: bool, actor_id: str, note: str
) -> RuleProposal:
    """Promote or reject a proposal. Promotion is the only way a rule changes."""
    if not note.strip():
        raise ValidationFailed("A review decision requires a note", code="note_required")

    proposal.status = (ProposalStatus.APPROVED if approve else ProposalStatus.REJECTED).value
    proposal.reviewed_by_id = actor_id
    proposal.review_note = note
    proposal.validation_state = "promoted" if approve else "rejected"
    proposal.version += 1
    db.add(proposal)

    if approve and proposal.scope == "finishing_allowance" and proposal.project_id:
        # A promoted rule becomes a tenant default; it is never applied to an
        # already-released plan, only to future planning.
        project = db.get(PartProject, proposal.project_id)
        if project:
            plan = db.execute(
                select(ManufacturingPlan).where(ManufacturingPlan.project_id == project.id).limit(1)
            ).scalar_one_or_none()
            material = db.get(Material, plan.material_id) if plan and plan.material_id else None
            if material:
                material.notes = f"{material.notes or ''}\nPromoted rule {proposal.id}: {proposal.proposed_value}".strip()
                db.add(material)

    audit.record(
        db,
        tenant_id=proposal.tenant_id,
        object_kind="rule_proposal",
        object_id=proposal.id,
        action="approve" if approve else "reject",
        actor_id=actor_id,
        after={"status": proposal.status, "proposed_value": proposal.proposed_value},
        reason=note,
    )
    return proposal
