"""Process plan and toolpath generation.

Bridges the deterministic engines to persisted, versioned plans. Every derived
value keeps the rule that produced it, so the CAM studio can answer "why this
speed, why this tool, why this order" from stored data rather than a narrative
(PRD 10.4).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import events, staleness
from app.core.enums import Gate, LifecycleStatus, OperationType
from app.core.errors import GateBlocked, ValidationFailed
from app.core.hashing import sha256_json
from app.engines import cutting, geom2d, planner, toolpath
from app.engines.partmodel import (
    Grid,
    PartModel,
    grid_for,
    stock_heightfield,
    target_heightfield,
)
from app.engines.stock import HeightFieldStock
from app.models.base import audit_entry
from app.models.factory import FixtureVersion, MachineVersion, Material, ToolAssemblyVersion
from app.models.geometry import GeometryVersion
from app.models.planning import MachineFeasibility, ManufacturingPlan, Operation, Setup, ToolpathVersion
from app.models.project import PartProject
from app.services import policy

#: Maps an operation type to the material rule class used for its parameters.
OPERATION_CLASS = {
    OperationType.FACING: cutting.CLASS_ROUGH,
    OperationType.ADAPTIVE_ROUGH: cutting.CLASS_ROUGH,
    OperationType.POCKET: cutting.CLASS_ROUGH,
    OperationType.REST_ROUGH: cutting.CLASS_FINISH,
    OperationType.CONTOUR: cutting.CLASS_FINISH,
    OperationType.FINISH_3D: cutting.CLASS_FINISH,
    OperationType.SLOT: cutting.CLASS_SLOT,
    OperationType.DRILL: cutting.CLASS_DRILL,
    OperationType.BORE: cutting.CLASS_BORE,
    OperationType.REAM: cutting.CLASS_REAM,
    OperationType.TAP: cutting.CLASS_TAP,
    OperationType.CHAMFER: cutting.CLASS_CHAMFER,
}


# --------------------------------------------------------------- adapters
def tool_candidate(record: ToolAssemblyVersion) -> planner.ToolCandidate:
    cutter = record.cutter or {}
    holder = record.holder or {}
    return planner.ToolCandidate(
        id=record.id,
        code=record.code,
        diameter=float(cutter.get("diameter", 10.0)),
        flutes=int(cutter.get("flutes", 2)),
        flute_length=float(cutter.get("flute_length", 20.0)),
        corner_radius=float(cutter.get("corner_radius", 0.0)),
        tool_type=cutter.get("type", "endmill"),
        material=cutter.get("material", "carbide"),
        max_rpm=record.max_rpm,
        gauge_length=record.gauge_length_mm,
        available=record.available,
        has_collision_model=record.has_collision_model,
        cost_per_edge=record.cost_per_edge,
        expected_life_minutes=record.expected_life_minutes,
        holder_diameter=float(holder.get("diameter", 0.0)),
        shank_diameter=float(cutter.get("shank_diameter", cutter.get("diameter", 10.0))),
    )


def machine_candidate(record: MachineVersion) -> planner.MachineCandidate:
    return planner.MachineCandidate(
        id=record.id,
        code=record.code,
        name=record.name,
        kinematics=record.kinematics,
        travels_mm=dict(record.travels_mm or {}),
        max_rpm=record.max_rpm,
        min_rpm=record.min_rpm,
        max_feed_mm_min=record.max_feed_mm_min,
        magazine_capacity=record.magazine_capacity,
        hourly_rate=record.hourly_rate,
        setup_rate=record.setup_rate,
        tool_change_seconds=record.tool_change_seconds,
        index_seconds=record.index_seconds,
        controller=record.controller,
        geometry_qualified=record.geometry_qualified,
    )


def fixture_candidate(record: FixtureVersion) -> planner.FixtureCandidate:
    return planner.FixtureCandidate(
        id=record.id,
        code=record.code,
        name=record.name,
        jaw_opening_mm=record.jaw_opening_mm,
        max_part_height_mm=record.max_part_height_mm,
        clamp_height_mm=record.clamp_height_mm,
        safe_clearance_mm=record.safe_clearance_mm,
        verified=record.verified,
        solids=list(record.solids or []),
    )


def machine_limits(record: MachineVersion) -> cutting.MachineLimits:
    return cutting.MachineLimits(
        max_rpm=record.max_rpm,
        min_rpm=record.min_rpm,
        max_feed_mm_min=record.max_feed_mm_min,
        spindle_curve=[list(p) for p in (record.spindle_curve or [])],
    )


def tool_geometry(record: ToolAssemblyVersion) -> cutting.ToolGeometry:
    cutter = record.cutter or {}
    return cutting.ToolGeometry(
        diameter=float(cutter.get("diameter", 10.0)),
        flutes=int(cutter.get("flutes", 2)),
        flute_length=float(cutter.get("flute_length", 20.0)),
        corner_radius=float(cutter.get("corner_radius", 0.0)),
        material=cutter.get("material", "carbide"),
        coating=cutter.get("coating", "TiAlN"),
        max_rpm=record.max_rpm,
        tool_type=cutter.get("type", "endmill"),
        point_angle=float(cutter.get("point_angle", 140.0)),
    )


# --------------------------------------------------------------- generation
def generate_plans(
    db: Session,
    *,
    project: PartProject,
    geometry: GeometryVersion,
    material: Material,
    fixture: FixtureVersion | None,
    machines: list[MachineVersion],
    objective: dict[str, Any] | str = "balanced",
    actor_id: str | None = None,
    progress: Callable[[str, float, str], None] | None = None,
) -> list[ManufacturingPlan]:
    """Produce one candidate per feasible machine, and a cause code per rejected one."""
    if geometry.status != LifecycleStatus.APPROVED:
        raise GateBlocked(
            f"{Gate.GEOMETRY_APPROVAL.value} has not been signed for this geometry version",
            detail={"geometry_version_id": geometry.id, "status": geometry.status},
        )
    if not material.rules_validated:
        # Not a hard stop: an engineer may enter rules for a new material, but
        # the plan must carry the fact that they are unvalidated.
        pass

    model = PartModel.from_dict(geometry.part_model)
    tools = [
        tool_candidate(t)
        for t in db.execute(
            select(ToolAssemblyVersion).where(
                ToolAssemblyVersion.tenant_id == project.tenant_id,
                ToolAssemblyVersion.status == LifecycleStatus.CURRENT.value,
            )
        ).scalars()
    ]
    if not tools:
        raise ValidationFailed("The tool crib is empty; register tool assemblies before planning", code="no_tools")

    stock = planner.choose_stock(model)
    access = sorted({planner.access_label(f) for f in model.features}) or ["+Z"]
    fixture_view = fixture_candidate(fixture) if fixture else None
    required_rpm = planner.required_rpm_for(tools, material.cutting_rules or {})

    # Clear any previous feasibility rows so the machine selector always shows
    # the current assessment rather than an accumulation of stale ones.
    for row in db.execute(
        select(MachineFeasibility).where(
            MachineFeasibility.project_id == project.id, MachineFeasibility.plan_id.is_(None)
        )
    ).scalars():
        db.delete(row)
    # Sessions run with autoflush off, so the delete is made visible explicitly
    # before the new assessments are written.
    db.flush()

    plans: list[ManufacturingPlan] = []
    for index, machine in enumerate(machines):
        if progress:
            progress("assessing machines", 10 + 40 * index / max(len(machines), 1), f"{machine.code}")
        candidate = machine_candidate(machine)
        feasibility = planner.assess_machine(
            candidate,
            model=model,
            stock=stock,
            access_directions=access,
            tool_count=min(len(tools), 8),
            fixture=fixture_view,
            required_rpm=required_rpm,
        )
        db.add(
            MachineFeasibility(
                tenant_id=project.tenant_id,
                project_id=project.id,
                machine_version_id=machine.id,
                feasible=feasibility.feasible,
                cause_code=feasibility.cause_code,
                binding_constraint=feasibility.binding_constraint,
                detail=feasibility.detail,
            )
        )
        # Flushed now so the assessment is readable even on the path where no
        # machine is feasible and the caller needs every cause code.
        db.flush()
        if not feasibility.feasible:
            continue

        strategy = objective if isinstance(objective, str) else objective.get("preset", "balanced")
        candidate_plan = planner.plan_candidate(
            model=model,
            features=model.features,
            machine=candidate,
            tools=tools,
            fixture=fixture_view,
            label=f"{machine.code} - {strategy}",
            stock=stock,
            strategy=strategy,
        )
        plans.append(
            _persist_plan(
                db,
                project=project,
                geometry=geometry,
                machine=machine,
                fixture=fixture,
                material=material,
                candidate=candidate_plan,
                objective=objective if isinstance(objective, dict) else {"preset": objective},
                actor_id=actor_id,
            )
        )

    if not plans:
        raise GateBlocked(
            "No registered machine can make this part as modelled",
            detail={
                "machines": [
                    {
                        "code": db.get(MachineVersion, row.machine_version_id).code,
                        "cause_code": row.cause_code,
                        "binding_constraint": row.binding_constraint,
                    }
                    for row in db.execute(
                        select(MachineFeasibility).where(
                            MachineFeasibility.project_id == project.id, MachineFeasibility.plan_id.is_(None)
                        )
                    ).scalars()
                ]
            },
            code="no_feasible_machine",
        )

    for rank, plan in enumerate(sorted(plans, key=lambda p: p.machine_version_id), start=1):
        plan.candidate_rank = rank
    if progress:
        progress("plans created", 90, f"{len(plans)} candidates")
    return plans


def _persist_plan(
    db: Session,
    *,
    project: PartProject,
    geometry: GeometryVersion,
    machine: MachineVersion,
    fixture: FixtureVersion | None,
    material: Material,
    candidate: planner.PlanCandidate,
    objective: dict[str, Any],
    actor_id: str | None,
) -> ManufacturingPlan:
    decision = {**candidate.decision_record, "unplanned": candidate.unplanned}
    plan = ManufacturingPlan(
        tenant_id=project.tenant_id,
        project_id=project.id,
        geometry_version_id=geometry.id,
        machine_version_id=machine.id,
        fixture_version_id=fixture.id if fixture else None,
        material_id=material.id,
        label=candidate.label,
        objective=objective,
        stock=candidate.stock,
        decision_record=decision,
        status=LifecycleStatus.DRAFT.value,
        audit=[audit_entry(actor_id or "system", "generated", candidate.label)],
    )
    db.add(plan)
    db.flush()

    for planned_setup in candidate.setups:
        setup = Setup(
            tenant_id=project.tenant_id,
            plan_id=plan.id,
            sequence=planned_setup.sequence,
            name=planned_setup.name,
            work_offset=planned_setup.work_offset,
            orientation_deg=planned_setup.orientation_deg,
            index_position={} if planned_setup.access in ("+Z", "-Z") else {"B": 90.0},
            origin_mm=[0.0, 0.0, 0.0],
            fixture_version_id=fixture.id if fixture else None,
            clearance_plane_mm=planned_setup.clearance_plane_mm,
            setup_minutes=planned_setup.setup_minutes,
            datum_scheme={
                "primary": "Stock top face",
                "secondary": "Fixed vise jaw",
                "tertiary": "Jaw stop",
                "origin": "Part centre, top of finished face",
                "access": planned_setup.access,
            },
            stock_orientation={"access": planned_setup.access, "rotation_deg": planned_setup.orientation_deg},
        )
        db.add(setup)
        db.flush()

        for planned_operation in planned_setup.operations:
            db.add(
                Operation(
                    tenant_id=project.tenant_id,
                    setup_id=setup.id,
                    sequence=planned_operation.sequence,
                    operation_type=planned_operation.operation_type,
                    feature_keys=planned_operation.feature_keys,
                    tool_assembly_id=planned_operation.tool.id,
                    parameters={
                        "z_start": planned_operation.z_start,
                        "z_target": planned_operation.z_target,
                        "operation_class": planned_operation.operation_class,
                        "label": planned_operation.label,
                        "geometry": _jsonable(planned_operation.geometry),
                    },
                    parameter_rationale=planned_operation.rationale,
                )
            )

    plan.content_hash = _plan_hash(db, plan)
    db.flush()

    staleness.link(
        db,
        tenant_id=project.tenant_id,
        downstream_kind="plan",
        downstream_id=plan.id,
        upstream_kind="geometry",
        upstream_id=geometry.id,
        upstream_hash=geometry.content_hash,
    )
    for kind, record in (("machine", machine), ("fixture", fixture), ("material", material)):
        if record is not None:
            staleness.link(
                db,
                tenant_id=project.tenant_id,
                downstream_kind="plan",
                downstream_id=plan.id,
                upstream_kind=kind,
                upstream_id=record.id,
                upstream_hash=getattr(record, "content_hash", "") or "",
            )

    events.publish(
        db,
        tenant_id=project.tenant_id,
        topic=events.Topic.PLAN_CANDIDATE_CREATED,
        project_id=project.id,
        actor_id=actor_id,
        payload={
            "plan_id": plan.id,
            "machine": machine.code,
            "setup_count": len(candidate.setups),
            "operation_count": candidate.operation_count,
            "tool_count": candidate.tool_count,
            "unplanned": len(candidate.unplanned),
        },
    )
    return plan


def _jsonable(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def _plan_hash(db: Session, plan: ManufacturingPlan) -> str:
    setups = db.execute(select(Setup).where(Setup.plan_id == plan.id).order_by(Setup.sequence)).scalars().all()
    payload = {
        "geometry": plan.geometry_version_id,
        "machine": plan.machine_version_id,
        "fixture": plan.fixture_version_id,
        "material": plan.material_id,
        "stock": plan.stock,
        "setups": [
            {
                "sequence": s.sequence,
                "work_offset": s.work_offset,
                "orientation": s.orientation_deg,
                "index": s.index_position,
                "operations": [
                    {
                        "sequence": o.sequence,
                        "type": o.operation_type,
                        "tool": o.tool_assembly_id,
                        "features": o.feature_keys,
                        "parameters": o.parameters,
                    }
                    for o in sorted(s.operations, key=lambda o: o.sequence)
                ],
            }
            for s in setups
        ],
    }
    return sha256_json(payload)


# --------------------------------------------------------------- toolpaths
def generate_toolpaths(
    db: Session,
    *,
    plan: ManufacturingPlan,
    actor_id: str | None = None,
    progress: Callable[[str, float, str], None] | None = None,
) -> list[ToolpathVersion]:
    """Generate every operation path, carrying remaining stock forward.

    The stock height field is updated as each operation is generated, so a rest
    operation sees what the previous tool actually left rather than the original
    stock envelope (FR-CAM-003).
    """
    evaluation = policy.evaluate_plan(db, plan)
    if not evaluation.passed:
        raise GateBlocked(
            f"{Gate.PLAN_APPROVAL.value} did not pass; {evaluation.blocks} remains blocked",
            detail=evaluation.to_dict(),
        )

    geometry = db.get(GeometryVersion, plan.geometry_version_id)
    machine = db.get(MachineVersion, plan.machine_version_id)
    material = db.get(Material, plan.material_id)
    model = PartModel.from_dict(geometry.part_model)
    model.stock = plan.stock or model.stock_block()

    grid = grid_for(model, 1.0)
    target = target_heightfield(model, grid)
    limits = machine_limits(machine)
    rules = material.cutting_rules or {}

    for existing in db.execute(
        select(ToolpathVersion).where(ToolpathVersion.plan_id == plan.id)
    ).scalars():
        db.delete(existing)
    db.flush()

    created: list[ToolpathVersion] = []
    setups = sorted(plan.setups, key=lambda s: s.sequence)
    total_operations = sum(len(s.operations) for s in setups) or 1
    done = 0

    for setup in setups:
        stock = HeightFieldStock(stock_heightfield(model, grid), grid, float(model.stock_block()["min"][2]))
        for operation in sorted(setup.operations, key=lambda o: o.sequence):
            done += 1
            if progress:
                progress(
                    "generating toolpaths",
                    10 + 80 * done / total_operations,
                    f"{setup.name}: {operation.parameters.get('label', operation.operation_type)}",
                )
            if operation.suppressed:
                continue

            tool = db.get(ToolAssemblyVersion, operation.tool_assembly_id)
            path, parameters = _generate_one(
                operation=operation,
                setup=setup,
                model=model,
                grid=grid,
                target=target,
                stock=stock,
                tool=tool,
                limits=limits,
                rules=rules,
            )
            if path is None:
                continue

            operation.parameters = {**operation.parameters, **parameters.to_dict()}
            operation.coolant = parameters.coolant
            operation.parameter_rationale = {**(operation.parameter_rationale or {}), **parameters.rationale}
            db.add(operation)

            payload = path.to_dict()
            record = ToolpathVersion(
                tenant_id=plan.tenant_id,
                operation_id=operation.id,
                plan_id=plan.id,
                revision=1,
                generator=path.generator,
                generator_version=path.generator_version,
                tolerance_mm=0.01,
                moves=payload["moves"],
                move_count=payload["move_count"],
                cutting_length_mm=payload["cutting_length_mm"],
                rapid_length_mm=payload["rapid_length_mm"],
                status=LifecycleStatus.CURRENT.value,
                content_hash=sha256_json(payload),
            )
            db.add(record)
            db.flush()
            created.append(record)

            _apply_to_stock(path, stock, tool)
            record.removed_volume_mm3 = 0.0

            staleness.link(
                db,
                tenant_id=plan.tenant_id,
                downstream_kind="toolpath",
                downstream_id=record.id,
                upstream_kind="plan",
                upstream_id=plan.id,
                upstream_hash=plan.content_hash,
            )

    plan.status = LifecycleStatus.CURRENT.value
    plan.stale = False
    plan.stale_reason = None
    plan.content_hash = _plan_hash(db, plan)
    db.add(plan)
    return created


def _generate_one(
    *,
    operation: Operation,
    setup: Setup,
    model: PartModel,
    grid: Grid,
    target: Any,
    stock: HeightFieldStock,
    tool: ToolAssemblyVersion,
    limits: cutting.MachineLimits,
    rules: dict[str, Any],
) -> tuple[toolpath.Toolpath | None, cutting.CuttingParameters]:
    params_meta = operation.parameters or {}
    geometry_spec = params_meta.get("geometry") or {}
    z_start = float(params_meta.get("z_start", model.z_top))
    z_target = float(params_meta.get("z_target", model.z_bottom))
    operation_type = OperationType(operation.operation_type)
    operation_class = OPERATION_CLASS.get(operation_type, cutting.CLASS_ROUGH)

    geometry_tool = tool_geometry(tool)
    parameters = cutting.compute(
        tool=geometry_tool,
        machine=limits,
        material_rules=rules,
        operation_class=operation_class,
        depth_available_mm=abs(z_start - z_target) or None,
    )

    ctx = toolpath.PathContext(
        tool_diameter=geometry_tool.diameter,
        corner_radius=geometry_tool.corner_radius,
        clearance_z=setup.clearance_plane_mm,
        retract_z=3.0,
        finish_allowance_mm=0.2 if operation_class == cutting.CLASS_ROUGH else 0.0,
        stock_top_z=float(stock.height.max()),
    )

    if operation_type is OperationType.FACING:
        region = geom2d.rect_polygon(
            (
                (model.stock_block()["min"][0] + model.stock_block()["max"][0]) / 2.0,
                (model.stock_block()["min"][1] + model.stock_block()["max"][1]) / 2.0,
            ),
            (
                model.stock_block()["max"][0] - model.stock_block()["min"][0],
                model.stock_block()["max"][1] - model.stock_block()["min"][1],
            ),
        )
        return toolpath.facing(region=region, z_start=z_start, z_target=z_target, ctx=ctx, params=parameters), parameters

    if operation_type in (OperationType.POCKET, OperationType.ADAPTIVE_ROUGH):
        polygon = [tuple(p) for p in geometry_spec.get("polygon", [])]
        if len(polygon) < 3:
            return None, parameters
        if operation_type is OperationType.ADAPTIVE_ROUGH:
            return (
                toolpath.adaptive_rough(boundary=polygon, z_start=z_start, floor_z=z_target, ctx=ctx, params=parameters),
                parameters,
            )
        return toolpath.pocket(boundary=polygon, z_start=z_start, floor_z=z_target, ctx=ctx, params=parameters), parameters

    if operation_type is OperationType.REST_ROUGH:
        polygon = [tuple(p) for p in geometry_spec.get("polygon", [])]
        return (
            toolpath.rest_machining(
                stock_z=stock.height,
                target_z=target,
                grid=grid,
                ctx=ctx,
                params=parameters,
                region=polygon if len(polygon) >= 3 else None,
            ),
            parameters,
        )

    if operation_type is OperationType.SLOT:
        return (
            toolpath.slotting(
                start=tuple(geometry_spec["start"]),
                end=tuple(geometry_spec["end"]),
                width=float(geometry_spec["width"]),
                z_start=z_start,
                floor_z=z_target,
                ctx=ctx,
                params=parameters,
            ),
            parameters,
        )

    if operation_type is OperationType.DRILL:
        depth = abs(z_start - z_target)
        if geometry_spec.get("through"):
            # Extend past break-out by the drill point length so the cone clears.
            point_length = (geometry_tool.diameter / 2.0) / max(
                1e-6, abs(math.tan(math.radians(geometry_tool.point_angle / 2.0)))
            )
            depth += point_length + 1.5
        return (
            toolpath.drilling(
                points=[tuple(c) for c in geometry_spec.get("centers", [])],
                z_top=z_start,
                depth=depth,
                ctx=ctx,
                params=parameters,
                peck=min(geometry_tool.diameter, 4.0) if depth > geometry_tool.diameter * 3 else None,
                cycle="G83" if depth > geometry_tool.diameter * 3 else "G81",
                point_angle_deg=geometry_tool.point_angle,
            ),
            parameters,
        )

    if operation_type in (OperationType.BORE, OperationType.REAM):
        centers = geometry_spec.get("centers") or []
        merged = toolpath.Toolpath(generator="helical_bore")
        for center in centers:
            single = toolpath.helical_bore(
                center=tuple(center),
                diameter=float(geometry_spec.get("diameter", geometry_tool.diameter * 1.5)),
                z_start=z_start,
                floor_z=z_target,
                ctx=ctx,
                params=parameters,
            )
            merged.moves.extend(single.moves)
            merged.cutting_length_mm += single.cutting_length_mm
            merged.rapid_length_mm += single.rapid_length_mm
            merged.warnings.extend(single.warnings)
        return merged, parameters

    if operation_type is OperationType.CONTOUR:
        polygon = [tuple(p) for p in geometry_spec.get("polygon", [])]
        if len(polygon) < 3:
            return None, parameters
        return (
            toolpath.contour(
                outline=polygon,
                z_start=z_start,
                z_target=z_target,
                ctx=ctx,
                params=parameters,
                side=geometry_spec.get("side", "outside"),
            ),
            parameters,
        )

    if operation_type is OperationType.FINISH_3D:
        return (
            toolpath.finish_3d(target_z=target, grid=grid, ctx=ctx, params=parameters, ball_nose=geometry_tool.corner_radius > 0),
            parameters,
        )

    if operation_type is OperationType.CHAMFER:
        edge = model.outline_polygon() if geometry_spec.get("on", "outline") == "outline" else None
        if edge is None:
            return None, parameters
        return (
            toolpath.chamfering(
                edge=edge,
                z_top=model.z_top,
                width=float(geometry_spec.get("width", 0.5)),
                ctx=ctx,
                params=parameters,
            ),
            parameters,
        )

    return None, parameters


def _apply_to_stock(path: toolpath.Toolpath, stock: HeightFieldStock, tool: ToolAssemblyVersion) -> None:
    """Replay a generated path against the height field so the next one sees it."""
    cutter = tool.cutter or {}
    radius = float(cutter.get("diameter", 10.0)) / 2.0
    corner = float(cutter.get("corner_radius", 0.0))
    previous: tuple[float, float, float] | None = None

    for move in path.moves:
        if move.get("t") == "drill":
            point = (float(move["x"]), float(move["y"]), float(move["cycle"]["z_depth"]))
            stock.cut(point, point, radius, 0.0)
            previous = point
            continue
        point = (
            float(move.get("x", previous[0] if previous else 0.0)),
            float(move.get("y", previous[1] if previous else 0.0)),
            float(move.get("z", previous[2] if previous else 0.0)),
        )
        if previous is not None and move.get("t") in ("linear", "plunge"):
            stock.cut(previous, point, radius, corner)
        previous = point


def approve_plan(db: Session, plan: ManufacturingPlan, *, actor_id: str, note: str | None = None) -> ManufacturingPlan:
    evaluation = policy.evaluate_plan(db, plan)
    if not evaluation.passed:
        raise GateBlocked(
            f"{Gate.PLAN_APPROVAL.value} did not pass; {evaluation.blocks} remains blocked",
            detail=evaluation.to_dict(),
        )
    plan.status = LifecycleStatus.APPROVED.value
    plan.approved_by_id = actor_id
    plan.version += 1
    plan.audit = [*(plan.audit or []), audit_entry(actor_id, "approved", note)]
    db.add(plan)
    return plan
