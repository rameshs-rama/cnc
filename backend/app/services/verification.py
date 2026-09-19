"""Simulation orchestration and disposition (FR-SIM-001 to FR-SIM-003)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import events, staleness
from app.core.enums import Severity
from app.core.errors import NotFound, ValidationFailed
from app.core.hashing import sha256_json
from app.engines import simulate as sim_engine
from app.engines.collision import build_segments, load_solids
from app.engines.kinematics import MachineModel
from app.engines.partmodel import PartModel
from app.models.factory import FixtureVersion, MachineVersion, ToolAssemblyVersion
from app.models.geometry import GeometryVersion
from app.models.planning import ManufacturingPlan, ToolpathVersion
from app.models.verification import SimulationRun
from app.services import policy


def run_simulation(
    db: Session,
    *,
    plan: ManufacturingPlan,
    voxel_pitch: float = 1.0,
    tolerance_mm: float = 0.05,
    actor_id: str | None = None,
    progress: Callable[[str, float, str], None] | None = None,
) -> SimulationRun:
    geometry = db.get(GeometryVersion, plan.geometry_version_id)
    machine = db.get(MachineVersion, plan.machine_version_id)
    fixture = db.get(FixtureVersion, plan.fixture_version_id) if plan.fixture_version_id else None
    if geometry is None or machine is None:
        raise NotFound("The plan references geometry or a machine that no longer exists")

    model = PartModel.from_dict(geometry.part_model)
    model.stock = plan.stock or model.stock_block()

    if progress:
        progress("preparing", 10, f"voxel pitch {voxel_pitch} mm")

    setups, input_hashes = _build_setups(db, plan, fixture)
    if not setups:
        raise ValidationFailed(
            "The plan has no generated toolpaths to verify", code="no_toolpaths"
        )

    identity = sha256_json(
        {
            "engine": sim_engine.ENGINE_VERSION,
            "voxel_pitch": voxel_pitch,
            "tolerance_mm": tolerance_mm,
            "machine": machine.content_hash or machine.id,
            "fixture": (fixture.content_hash or fixture.id) if fixture else None,
            "geometry": geometry.content_hash,
            "plan": plan.content_hash,
            "toolpaths": input_hashes["toolpaths"],
            "tools": input_hashes["tools"],
            "stock": plan.stock,
        }
    )

    if progress:
        progress("simulating", 30, f"{sum(len(s.operations) for s in setups)} operations")

    result = sim_engine.simulate(
        part_model=model,
        setups=setups,
        machine=MachineModel.from_record(machine),
        voxel_pitch=voxel_pitch,
        tolerance_mm=tolerance_mm,
    )

    if progress:
        progress("storing result", 90, f"{len(result.events)} events, {result.cycle_time_seconds:.0f} s cycle")

    run = SimulationRun(
        tenant_id=plan.tenant_id,
        project_id=plan.project_id,
        plan_id=plan.id,
        engine_version=result.engine_version,
        identity_hash=identity,
        input_hashes=input_hashes,
        voxel_size_mm=voxel_pitch,
        passed=result.passed,
        events=result.events,
        event_counts=result.event_counts,
        max_severity=result.max_severity,
        cycle_time_seconds=result.cycle_time_seconds,
        time_breakdown={**result.time_breakdown, "per_operation": result.per_operation},
        remaining_stock=result.remaining_stock,
        stock_comparison=result.stock_comparison,
        programmed_envelope=result.programmed_envelope,
        content_hash=sha256_json({"identity": identity, "events": result.event_counts, "time": result.time_breakdown}),
    )
    db.add(run)
    db.flush()

    staleness.link(
        db,
        tenant_id=plan.tenant_id,
        downstream_kind="simulation",
        downstream_id=run.id,
        upstream_kind="plan",
        upstream_id=plan.id,
        upstream_hash=plan.content_hash,
    )
    for toolpath_id, toolpath_hash in input_hashes["toolpath_map"].items():
        staleness.link(
            db,
            tenant_id=plan.tenant_id,
            downstream_kind="simulation",
            downstream_id=run.id,
            upstream_kind="toolpath",
            upstream_id=toolpath_id,
            upstream_hash=toolpath_hash,
        )

    events.publish(
        db,
        tenant_id=plan.tenant_id,
        topic=events.Topic.SIMULATION_COMPLETED,
        project_id=plan.project_id,
        actor_id=actor_id,
        payload={
            "simulation_id": run.id,
            "input_hashes": {k: v for k, v in input_hashes.items() if k != "toolpath_map"},
            "identity_hash": identity,
            "result": "passed" if run.passed else "failed",
            "event_counts": run.event_counts,
            "cycle_time_seconds": run.cycle_time_seconds,
        },
    )
    return run


def _build_setups(
    db: Session, plan: ManufacturingPlan, fixture: FixtureVersion | None
) -> tuple[list[sim_engine.SetupSim], dict[str, Any]]:
    solids = load_solids(list(fixture.solids or [])) if fixture else []
    toolpath_map: dict[str, str] = {}
    tool_hashes: dict[str, str] = {}
    setups: list[sim_engine.SetupSim] = []

    for setup in sorted(plan.setups, key=lambda s: s.sequence):
        operations: list[sim_engine.OperationSim] = []
        for operation in sorted(setup.operations, key=lambda o: o.sequence):
            path = db.execute(
                select(ToolpathVersion)
                .where(ToolpathVersion.operation_id == operation.id)
                .order_by(ToolpathVersion.revision.desc())
                .limit(1)
            ).scalar_one_or_none()
            if path is None or not path.moves:
                continue
            toolpath_map[path.id] = path.content_hash

            tool = db.get(ToolAssemblyVersion, operation.tool_assembly_id)
            tool_hashes[tool.code] = tool.content_hash or tool.id
            cutter = tool.cutter or {}
            spec = sim_engine.ToolSpec(
                id=tool.id,
                code=tool.code,
                number=int(cutter.get("number", 1)),
                diameter=float(cutter.get("diameter", 10.0)),
                flute_length=float(cutter.get("flute_length", 20.0)),
                corner_radius=float(cutter.get("corner_radius", 0.0)),
                gauge_length=tool.gauge_length_mm,
                max_rpm=tool.max_rpm,
                has_collision_model=tool.has_collision_model,
                segments=build_segments(
                    cutter_diameter=float(cutter.get("diameter", 10.0)),
                    flute_length=float(cutter.get("flute_length", 20.0)),
                    shank_diameter=float(cutter.get("shank_diameter", cutter.get("diameter", 10.0))),
                    collision_profile=list(tool.collision_profile or []),
                    gauge_length=tool.gauge_length_mm,
                ),
            )
            parameters = operation.parameters or {}
            operations.append(
                sim_engine.OperationSim(
                    operation_id=operation.id,
                    label=parameters.get("label", operation.operation_type),
                    tool=spec,
                    moves=path.moves,
                    rpm=float(parameters.get("rpm", 1000.0)),
                    feed_mm_min=float(parameters.get("feed_mm_min", 500.0)),
                    plunge_feed_mm_min=float(parameters.get("plunge_feed_mm_min", 200.0)),
                    coolant=operation.coolant,
                    suppressed=operation.suppressed,
                )
            )
        if operations:
            setups.append(
                sim_engine.SetupSim(
                    setup_id=setup.id,
                    sequence=setup.sequence,
                    name=setup.name,
                    orientation_deg=list(setup.orientation_deg or [0, 0, 0]),
                    origin_mm=list(setup.origin_mm or [0, 0, 0]),
                    index_position=dict(setup.index_position or {}),
                    clearance_plane_mm=setup.clearance_plane_mm,
                    fixture_solids=solids,
                    fixture_verified=bool(fixture and fixture.verified),
                    operations=operations,
                )
            )

    return setups, {
        "toolpaths": sha256_json(sorted(toolpath_map.values())),
        "tools": sha256_json(tool_hashes),
        "toolpath_map": toolpath_map,
    }


def disposition_event(
    db: Session,
    run: SimulationRun,
    *,
    event_code: str,
    decision: str,
    actor_id: str,
    reason: str,
) -> SimulationRun:
    """Record an engineer's decision on a simulation finding.

    An S1 event can be acknowledged but never resolved away: the only way past
    a collision or an overcut is to correct the cause and re-simulate (PRD 5.3).
    """
    event = next((e for e in (run.events or []) if e["code"] == event_code), None)
    if event is None:
        raise NotFound(f"Simulation has no event {event_code}")
    if Severity(event["severity"]) is Severity.S1_STOP and decision == "resolved":
        raise ValidationFailed(
            "An S1 stop condition cannot be dispositioned as resolved. Correct the cause and re-simulate.",
            code="s1_not_waivable",
        )
    if not reason.strip():
        raise ValidationFailed("A disposition requires a reason", code="reason_required")

    run.dispositions = [
        *(run.dispositions or []),
        {"event_code": event_code, "decision": decision, "actor_id": actor_id, "reason": reason},
    ]
    db.add(run)
    return run


def simulation_gate(db: Session, run: SimulationRun) -> dict[str, Any]:
    return policy.evaluate_simulation(db, run).to_dict()


def latest_passing(db: Session, plan: ManufacturingPlan) -> SimulationRun | None:
    return db.execute(
        select(SimulationRun)
        .where(
            SimulationRun.plan_id == plan.id,
            SimulationRun.passed.is_(True),
            SimulationRun.stale.is_(False),
        )
        .order_by(SimulationRun.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
