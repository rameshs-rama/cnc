"""Plans, toolpaths, machine feasibility and optimisation."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, require, scoped, tenant_project
from app.core.enums import JobKind
from app.core.rbac import Permission
from app.engines import optimize
from app.jobs import queue
from app.models.factory import MachineVersion, ToolAssemblyVersion
from app.models.planning import MachineFeasibility, ManufacturingPlan, Operation, Setup, ToolpathVersion
from app.schemas.api import (
    ApprovalRequest,
    JobOut,
    OptimizationRequest,
    PlanGenerateRequest,
    PlanOut,
    SetupOut,
)
from app.services import planning as planning_service
from app.services import policy

router = APIRouter(tags=["planning"])


@router.post("/plans:generate", response_model=JobOut, status_code=202, summary="Generate route candidates")
def generate_plans(
    payload: PlanGenerateRequest,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.PLAN_CREATE))],
) -> Any:
    project = tenant_project(db, user, payload.project_id)
    job = queue.enqueue(
        db,
        tenant_id=user.tenant_id,
        kind=JobKind.PLAN_GENERATE.value,
        project_id=project.id,
        payload={
            "geometry_version_id": payload.geometry_version_id,
            "material_id": payload.material_id,
            "fixture_id": payload.fixture_id,
            "machine_ids": payload.machine_ids,
            "objective": payload.objective,
        },
        requested_by_id=user.id,
    )
    db.commit()
    db.refresh(job)
    return job


@router.get("/projects/{project_id}/plans", response_model=list[PlanOut], summary="List plan candidates")
def list_plans(project_id: str, db: DbSession, user: CurrentUser) -> list[ManufacturingPlan]:
    tenant_project(db, user, project_id)
    return list(
        db.execute(
            select(ManufacturingPlan)
            .where(ManufacturingPlan.project_id == project_id)
            .order_by(ManufacturingPlan.candidate_rank)
        ).scalars()
    )


@router.get("/plans/{plan_id}", response_model=PlanOut, summary="Read one plan")
def get_plan(plan_id: str, db: DbSession, user: CurrentUser) -> ManufacturingPlan:
    return scoped(db, user, ManufacturingPlan, plan_id, "Plan")


@router.get("/plans/{plan_id}/setups", response_model=list[SetupOut], summary="Setups and operations")
def plan_setups(plan_id: str, db: DbSession, user: CurrentUser) -> list[Setup]:
    plan = scoped(db, user, ManufacturingPlan, plan_id, "Plan")
    return sorted(plan.setups, key=lambda s: s.sequence)


@router.get("/projects/{project_id}/machine-feasibility", summary="Machine comparison with cause codes")
def machine_feasibility(project_id: str, db: DbSession, user: CurrentUser) -> list[dict[str, Any]]:
    """Every registered machine with feasibility and the exact binding constraint."""
    tenant_project(db, user, project_id)
    rows = db.execute(
        select(MachineFeasibility).where(MachineFeasibility.project_id == project_id)
    ).scalars().all()
    out: list[dict[str, Any]] = []
    for row in rows:
        machine = db.get(MachineVersion, row.machine_version_id)
        plan = db.execute(
            select(ManufacturingPlan).where(
                ManufacturingPlan.project_id == project_id,
                ManufacturingPlan.machine_version_id == row.machine_version_id,
            ).limit(1)
        ).scalar_one_or_none()
        out.append(
            {
                "machine_version_id": row.machine_version_id,
                "machine_code": machine.code if machine else None,
                "machine_name": machine.name if machine else None,
                "controller": machine.controller if machine else None,
                "kinematics": machine.kinematics if machine else None,
                "hourly_rate": machine.hourly_rate if machine else None,
                "feasible": row.feasible,
                "cause_code": row.cause_code,
                "binding_constraint": row.binding_constraint,
                "detail": row.detail,
                "plan_id": plan.id if plan else None,
                "estimated_cycle_seconds": row.estimated_cycle_seconds,
                "estimated_cost": row.estimated_cost,
            }
        )
    return sorted(out, key=lambda r: (not r["feasible"], r["machine_code"] or ""))


@router.post(
    "/plans/{plan_id}/toolpaths:generate", response_model=JobOut, status_code=202, summary="Generate toolpaths"
)
def generate_toolpaths(
    plan_id: str,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.TOOLPATH_EDIT))],
) -> Any:
    plan = scoped(db, user, ManufacturingPlan, plan_id, "Plan")
    job = queue.enqueue(
        db,
        tenant_id=user.tenant_id,
        kind=JobKind.TOOLPATH_GENERATE.value,
        project_id=plan.project_id,
        payload={"plan_id": plan.id},
        requested_by_id=user.id,
    )
    db.commit()
    db.refresh(job)
    return job


@router.get("/plans/{plan_id}/toolpaths", summary="Generated toolpaths")
def list_toolpaths(
    plan_id: str, db: DbSession, user: CurrentUser, include_moves: bool = False
) -> list[dict[str, Any]]:
    plan = scoped(db, user, ManufacturingPlan, plan_id, "Plan")
    rows = db.execute(select(ToolpathVersion).where(ToolpathVersion.plan_id == plan.id)).scalars().all()
    out = []
    for path in rows:
        operation = db.get(Operation, path.operation_id)
        setup = db.get(Setup, operation.setup_id) if operation else None
        tool = db.get(ToolAssemblyVersion, operation.tool_assembly_id) if operation else None
        entry: dict[str, Any] = {
            "id": path.id,
            "operation_id": path.operation_id,
            "setup_id": setup.id if setup else None,
            "setup_name": setup.name if setup else None,
            "setup_sequence": setup.sequence if setup else None,
            "sequence": operation.sequence if operation else None,
            "operation_type": operation.operation_type if operation else None,
            "label": (operation.parameters or {}).get("label") if operation else None,
            "feature_keys": operation.feature_keys if operation else [],
            "tool": {"id": tool.id, "code": tool.code, "cutter": tool.cutter} if tool else None,
            "parameters": operation.parameters if operation else {},
            "parameter_rationale": operation.parameter_rationale if operation else {},
            "generator": path.generator,
            "generator_version": path.generator_version,
            "move_count": path.move_count,
            "cutting_length_mm": path.cutting_length_mm,
            "rapid_length_mm": path.rapid_length_mm,
            "content_hash": path.content_hash,
            "stale": path.stale,
        }
        if include_moves:
            entry["moves"] = path.moves
        out.append(entry)
    return sorted(out, key=lambda e: (e["setup_sequence"] or 0, e["sequence"] or 0))


@router.post("/plans/{plan_id}:approve", response_model=PlanOut, summary="Approve the plan")
def approve_plan(
    plan_id: str,
    payload: ApprovalRequest,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.PLAN_APPROVE))],
) -> ManufacturingPlan:
    plan = scoped(db, user, ManufacturingPlan, plan_id, "Plan")
    planning_service.approve_plan(db, plan, actor_id=user.id, note=payload.note)
    db.commit()
    db.refresh(plan)
    return plan


@router.get("/plans/{plan_id}/gate", summary="Evaluate the plan approval gate")
def plan_gate(plan_id: str, db: DbSession, user: CurrentUser) -> dict[str, Any]:
    plan = scoped(db, user, ManufacturingPlan, plan_id, "Plan")
    return policy.evaluate_plan(db, plan).to_dict()


@router.post("/optimizations", summary="Search plan candidates against a weighted objective")
def optimise(
    payload: OptimizationRequest,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.PLAN_CREATE))],
) -> dict[str, Any]:
    """Explore the decision variables the planner leaves open.

    Hard constraints are re-checked for every candidate, so a weight can change
    the ranking but never produce an infeasible plan (PRD 10.3).
    """
    from app.models.verification import SimulationRun

    plan = scoped(db, user, ManufacturingPlan, payload.plan_id, "Plan")
    machine = db.get(MachineVersion, plan.machine_version_id)
    simulation = db.execute(
        select(SimulationRun)
        .where(SimulationRun.plan_id == plan.id)
        .order_by(SimulationRun.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    baseline_cycle = simulation.cycle_time_seconds if simulation else 600.0
    baseline_rapid = float(
        sum(t.rapid_length_mm for t in db.execute(select(ToolpathVersion).where(ToolpathVersion.plan_id == plan.id)).scalars())
    ) or 1000.0
    setup_minutes = sum(s.setup_minutes for s in plan.setups) or 15.0
    tool_ids = {o.tool_assembly_id for s in plan.setups for o in s.operations}

    variables = [
        optimize.Variable(
            "stepover_ratio",
            [0.40, 0.30, 0.50, 0.25, 0.60],
            locked="stepover_ratio" in payload.locked_variables,
            description="Radial engagement as a fraction of cutter diameter",
        ),
        optimize.Variable(
            "stepdown_ratio",
            [1.0, 0.75, 1.5, 2.0],
            locked="stepdown_ratio" in payload.locked_variables,
            description="Axial depth as a fraction of cutter diameter",
        ),
        optimize.Variable(
            "tool_grouping",
            ["by_tool", "by_feature"],
            locked="tool_grouping" in payload.locked_variables,
            description="Group operations by tool to cut tool changes, or by feature to cut repositioning",
        ),
        optimize.Variable(
            "link_strategy",
            ["retract_minimal", "retract_safe"],
            locked="link_strategy" in payload.locked_variables,
            description="How far the tool retracts between passes",
        ),
    ]

    def evaluate(assignment: dict[str, Any]) -> optimize.Metrics:
        stepover = float(assignment["stepover_ratio"])
        stepdown = float(assignment["stepdown_ratio"])
        grouping = assignment["tool_grouping"]
        linking = assignment["link_strategy"]

        # Removal rate scales with the product of the engagements; time scales
        # inversely. Reference point is the baseline assignment.
        removal_scale = (stepover / 0.40) * (stepdown / 1.0)
        cutting = (baseline_cycle * 0.75) / max(removal_scale, 0.2)
        non_cutting = baseline_rapid * (0.7 if linking == "retract_minimal" else 1.0)
        tool_changes = len(tool_ids) if grouping == "by_tool" else len(tool_ids) * 2
        rapid_seconds = non_cutting / 20000.0 * 60.0
        cycle = cutting + rapid_seconds + tool_changes * (machine.tool_change_seconds if machine else 6.0)

        # Heavier engagement raises deflection and finish risk, and eats tool life.
        quality_risk = min(1.0, 0.05 + 0.35 * (stepover - 0.25) + 0.12 * (stepdown - 0.75))
        wear = (cutting / 60.0) * (1.0 + 0.8 * (stepover - 0.25) + 0.4 * (stepdown - 1.0))
        margin = 5.0 if linking == "retract_safe" else 2.0

        feasible = stepover <= 0.65 and stepdown <= 2.0
        return optimize.Metrics(
            cycle_time_s=cycle,
            setup_count=len(plan.setups),
            setup_minutes=setup_minutes,
            machine_cost=(cycle / 3600.0) * (machine.hourly_rate if machine else 65.0),
            tool_changes=tool_changes,
            tool_wear_minutes=max(wear, 0.1),
            non_cutting_mm=non_cutting,
            energy_kj=cutting * 2.5 * removal_scale,
            quality_risk=max(0.0, quality_risk),
            collision_margin_mm=margin,
            feasible=feasible,
            infeasible_reason=None if feasible else "Engagement beyond the validated envelope for this tooling",
        )

    result = optimize.search(
        variables=variables,
        evaluate=evaluate,
        weights=payload.weights,
        seed=payload.seed,
        budget=payload.budget,
    )
    plan.objective_score = result.best.objective
    plan.score_breakdown = {
        "weights": result.weights,
        "best": result.best.to_dict(),
        "baseline": result.baseline.to_dict(),
        "improvement": result.improvement,
    }
    db.add(plan)
    db.commit()

    payload_out = result.to_dict()
    payload_out["variables"] = [
        {"name": v.name, "values": v.values, "locked": v.locked, "description": v.description} for v in variables
    ]
    payload_out["note"] = (
        "Applying a candidate regenerates toolpaths and invalidates the previous simulation."
    )
    return payload_out
