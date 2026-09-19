"""Manufacturing IR assembly, postprocessing and NC validation (PRD 4.6)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import audit, staleness
from app.core.enums import Gate, LifecycleStatus
from app.core.errors import GateBlocked, NotFound, ValidationFailed
from app.core.storage import get_store
from app.engines import ir as ir_module
from app.engines import ncvalidate
from app.engines import post as post_module
from app.models.factory import MachineVersion, Material, PostProcessorVersion, ToolAssemblyVersion
from app.models.geometry import GeometryVersion
from app.models.identity import Tenant
from app.models.planning import ManufacturingPlan, ToolpathVersion
from app.models.project import PartProject
from app.models.verification import NCProgram, SimulationRun
from app.services import policy


def _prohibited_codes(db: Session, tenant_id: str) -> list[str]:
    """Tenant NC policy, falling back to the platform default list."""
    tenant = db.get(Tenant, tenant_id)
    configured = (tenant.policy or {}).get("prohibited_codes") if tenant else None
    return list(configured) if configured else list(ncvalidate.DEFAULT_PROHIBITED_CODES)


def build_ir(db: Session, *, plan: ManufacturingPlan, simulation: SimulationRun) -> ir_module.ManufacturingIR:
    """Assemble the controller-neutral IR from verified, current inputs."""
    project = db.get(PartProject, plan.project_id)
    geometry = db.get(GeometryVersion, plan.geometry_version_id)
    machine = db.get(MachineVersion, plan.machine_version_id)
    material = db.get(Material, plan.material_id) if plan.material_id else None

    tool_numbers: dict[str, int] = {}
    tools: list[ir_module.ToolRef] = []
    setups: list[ir_module.IRSetup] = []
    coordinate_systems: list[ir_module.CoordinateSystem] = []

    for setup in sorted(plan.setups, key=lambda s: s.sequence):
        coordinate_systems.append(
            ir_module.CoordinateSystem(
                name=setup.work_offset,
                origin_mm=list(setup.origin_mm or [0, 0, 0]),
                orientation_deg=list(setup.orientation_deg or [0, 0, 0]),
                description=f"{setup.name}: {(setup.datum_scheme or {}).get('origin', 'part origin')}",
            )
        )
        operations: list[ir_module.IROperation] = []
        for operation in sorted(setup.operations, key=lambda o: o.sequence):
            if operation.suppressed:
                continue
            path = db.execute(
                select(ToolpathVersion)
                .where(ToolpathVersion.operation_id == operation.id)
                .order_by(ToolpathVersion.revision.desc())
                .limit(1)
            ).scalar_one_or_none()
            if path is None or not path.moves:
                continue

            assembly = db.get(ToolAssemblyVersion, operation.tool_assembly_id)
            if assembly.id not in tool_numbers:
                number = len(tool_numbers) + 1
                tool_numbers[assembly.id] = number
                cutter = assembly.cutter or {}
                tools.append(
                    ir_module.ToolRef(
                        number=number,
                        code=assembly.code,
                        description=assembly.description,
                        diameter_mm=float(cutter.get("diameter", 10.0)),
                        flutes=int(cutter.get("flutes", 2)),
                        flute_length_mm=float(cutter.get("flute_length", 20.0)),
                        corner_radius_mm=float(cutter.get("corner_radius", 0.0)),
                        gauge_length_mm=assembly.gauge_length_mm,
                        length_offset=number,
                        diameter_offset=number,
                        max_rpm=assembly.max_rpm,
                        assembly_hash=assembly.content_hash or assembly.id,
                    )
                )

            parameters = operation.parameters or {}
            operations.append(
                ir_module.IROperation(
                    id=operation.id,
                    sequence=operation.sequence,
                    type=operation.operation_type,
                    label=parameters.get("label", ""),
                    feature_keys=list(operation.feature_keys or []),
                    tool_number=tool_numbers[assembly.id],
                    spindle_rpm=float(parameters.get("rpm", 1000.0)),
                    feed_mm_min=float(parameters.get("feed_mm_min", 500.0)),
                    plunge_feed_mm_min=float(parameters.get("plunge_feed_mm_min", 200.0)),
                    coolant=operation.coolant if operation.coolant in ("flood", "mist", "air", "through", "off") else "flood",
                    tolerance_mm=path.tolerance_mm,
                    stock_to_leave_mm=float(parameters.get("finish_allowance_mm", 0.0)),
                    moves=[ir_module.Move(**move) for move in path.moves],
                )
            )
        if operations:
            setups.append(
                ir_module.IRSetup(
                    id=setup.id,
                    sequence=setup.sequence,
                    name=setup.name,
                    work_offset=setup.work_offset,
                    clearance_plane_mm=setup.clearance_plane_mm,
                    retract_plane_mm=3.0,
                    orientation_deg=list(setup.orientation_deg or [0, 0, 0]),
                    index_position={k: float(v) for k, v in (setup.index_position or {}).items()},
                    datum_note=str((setup.datum_scheme or {}).get("primary", "")),
                    operations=operations,
                )
            )

    stock = plan.stock or {}
    return ir_module.ManufacturingIR(
        units=geometry.units if geometry.units in ("mm", "inch") else "mm",
        provenance=ir_module.Provenance(
            project_id=project.id,
            part_number=project.part_number,
            revision=project.revision,
            geometry_version_id=geometry.id,
            geometry_hash=geometry.content_hash,
            plan_id=plan.id,
            plan_hash=plan.content_hash,
            simulation_run_id=simulation.id,
            simulation_identity_hash=simulation.identity_hash,
        ),
        machine=ir_module.MachineRef(
            code=machine.code,
            manufacturer=machine.manufacturer,
            model=machine.model,
            controller=machine.controller,
            controller_version=machine.controller_version,
            kinematics=machine.kinematics,
            travels_mm={k: list(v) for k, v in (machine.travels_mm or {}).items()},
            max_rpm=machine.max_rpm,
            max_feed_mm_min=machine.max_feed_mm_min,
            configuration_hash=machine.content_hash or machine.id,
        ),
        stock=ir_module.Stock(
            min_mm=list(stock.get("min", [0, 0, 0])),
            max_mm=list(stock.get("max", [0, 0, 0])),
            material_code=material.code if material else "",
            material_name=material.name if material else "",
        ),
        coordinate_systems=coordinate_systems,
        tools=tools,
        setups=setups,
        verification={
            "simulation_run_id": simulation.id,
            "passed": simulation.passed,
            "cycle_time_seconds": simulation.cycle_time_seconds,
            "event_counts": simulation.event_counts,
            "engine_version": simulation.engine_version,
        },
    )


def postprocess(
    db: Session,
    *,
    plan: ManufacturingPlan,
    simulation: SimulationRun,
    post: PostProcessorVersion,
    program_number: str | None = None,
    actor_id: str | None = None,
    release_header: dict[str, Any] | None = None,
) -> NCProgram:
    """Generate and validate an NC candidate.

    Refuses unless the simulation passed and the post is certified for this
    exact machine and controller (FR-PST-002, PRD 5.2).
    """
    machine = db.get(MachineVersion, plan.machine_version_id)
    if machine is None:
        raise NotFound("Plan references a machine that no longer exists")

    simulation_gate = policy.evaluate_simulation(db, simulation)
    if not simulation_gate.passed:
        raise GateBlocked(
            f"{Gate.SIMULATION_PASS.value} did not pass; {simulation_gate.blocks} remains blocked",
            detail=simulation_gate.to_dict(),
        )
    post_gate = policy.evaluate_post(db, post, machine)
    if not post_gate.passed:
        raise GateBlocked(
            f"{Gate.POST_VALIDATION.value} did not pass; {post_gate.blocks} remains blocked",
            detail=post_gate.to_dict(),
        )

    ir = build_ir(db, plan=plan, simulation=simulation)
    if not ir.setups:
        raise ValidationFailed("The plan produced no postprocessable operations", code="empty_ir")

    ir_hash = ir.content_hash()
    store = get_store()
    ir_key, _ = store.put_bytes(plan.tenant_id, f"ir/{plan.project_id}", ir.model_dump_json(indent=2).encode(), ".json")

    project = db.get(PartProject, plan.project_id)
    number = program_number or f"O{abs(hash(project.part_number)) % 9000 + 1000}"
    header = {
        "post_version": f"{post.code} r{post.revision}",
        "ir_hash": ir_hash[:16],
        "plan_hash": plan.content_hash[:16],
        "simulation_hash": simulation.identity_hash[:16],
        **(release_header or {}),
    }

    runtime = post_module.build_post(post.definition)
    result = runtime.run(ir, program_number=number, release_header=header)
    program_key, program_hash = store.put_bytes(
        plan.tenant_id, f"nc/{plan.project_id}", result.text.encode(), ".nc"
    )

    magazine = {tool.number: {"code": tool.code, "diameter": tool.diameter_mm} for tool in ir.tools}
    report = ncvalidate.validate(
        result.text,
        ncvalidate.ValidationContext(
            machine_travels={k: list(v) for k, v in (machine.travels_mm or {}).items()},
            magazine_tools=magazine,
            allowed_work_offsets=list(machine.work_offsets or ["G54"]),
            max_rpm=machine.max_rpm,
            max_feed_mm_min=machine.max_feed_mm_min,
            expected_units=ir.units,
            simulated_envelope=simulation.programmed_envelope,
            prohibited_codes=_prohibited_codes(db, plan.tenant_id),
            required_header_tokens=[t for t in [header.get("release_id")] if t],
        ),
    )

    program = NCProgram(
        tenant_id=plan.tenant_id,
        project_id=plan.project_id,
        plan_id=plan.id,
        simulation_run_id=simulation.id,
        post_version_id=post.id,
        machine_version_id=machine.id,
        program_number=number,
        setup_sequence=ir.setups[0].sequence,
        storage_key=program_key,
        program_hash=program_hash,
        line_count=result.line_count,
        ir_hash=ir_hash,
        ir_storage_key=ir_key,
        validations=report.to_dict()["findings"],
        validation_passed=report.passed,
        max_severity=report.max_severity,
        programmed_envelope=report.envelope,
        status=LifecycleStatus.CURRENT.value if report.passed else LifecycleStatus.DRAFT.value,
    )
    db.add(program)
    db.flush()

    for kind, object_id, object_hash in (
        ("plan", plan.id, plan.content_hash),
        ("simulation", simulation.id, simulation.content_hash),
        ("post", post.id, post.runtime_hash),
    ):
        staleness.link(
            db,
            tenant_id=plan.tenant_id,
            downstream_kind="nc_program",
            downstream_id=program.id,
            upstream_kind=kind,
            upstream_id=object_id,
            upstream_hash=object_hash,
        )

    audit.record(
        db,
        tenant_id=plan.tenant_id,
        object_kind="nc_program",
        object_id=program.id,
        action="postprocess",
        actor_id=actor_id,
        after={
            "program_hash": program_hash,
            "ir_hash": ir_hash,
            "post": f"{post.code} r{post.revision}",
            "validation_passed": report.passed,
            "max_severity": report.max_severity,
        },
    )
    return program


def certify_post(
    db: Session,
    post: PostProcessorVersion,
    *,
    machine: MachineVersion,
    actor_id: str,
    note: str,
    test_program_hashes: list[str],
) -> PostProcessorVersion:
    """Certify a post against one exact machine and controller pair."""
    if post.machine_code != machine.code:
        raise ValidationFailed(
            f"Post targets {post.machine_code}; certification was requested against {machine.code}",
            code="machine_mismatch",
        )
    if not test_program_hashes:
        raise ValidationFailed(
            "Certification requires the hashes of the approved test programs", code="test_evidence_required"
        )

    runtime = post_module.build_post(post.definition)
    post.runtime_hash = runtime.runtime_hash
    post.certified = True
    post.certified_by_id = actor_id
    post.certification_note = note
    post.test_program_hashes = test_program_hashes
    post.enabled = True
    post.status = LifecycleStatus.APPROVED.value
    post.controller_version = post.controller_version or machine.controller_version
    db.add(post)

    audit.record(
        db,
        tenant_id=post.tenant_id,
        object_kind="post",
        object_id=post.id,
        action="certify",
        actor_id=actor_id,
        after={
            "machine": machine.code,
            "controller": machine.controller,
            "controller_version": post.controller_version,
            "runtime_hash": post.runtime_hash,
            "tests": test_program_hashes,
        },
        reason=note,
    )
    return post


def revoke_post(db: Session, post: PostProcessorVersion, *, actor_id: str, reason: str) -> PostProcessorVersion:
    """Revoke a post. Future releases are blocked; history stays auditable (US-12)."""
    if not reason.strip():
        raise ValidationFailed("Revocation requires a reason", code="reason_required")
    post.revoked = True
    post.enabled = False
    post.revocation_reason = reason
    post.status = LifecycleStatus.REVOKED.value
    db.add(post)

    affected = staleness.invalidate(
        db, upstream_kind="post", upstream_id=post.id, reason=f"Postprocessor revoked: {reason}"
    )
    audit.record(
        db,
        tenant_id=post.tenant_id,
        object_kind="post",
        object_id=post.id,
        action="revoke",
        actor_id=actor_id,
        after={"revoked": True, "invalidated": affected},
        reason=reason,
    )
    return post
