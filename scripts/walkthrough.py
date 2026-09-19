#!/usr/bin/env python3
"""End-to-end walkthrough of the controlled workflow.

Runs the whole digital thread against the seeded benchmark part: evidence
intake, reconstruction, engineering review, planning, CAM, simulation, costing,
postprocessing, release and production feedback.

    python scripts/walkthrough.py

This is the script the Definition of Done in PRD section 17 describes: a
qualified user completing intake through signed release and feedback.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("MIP_DATABASE_URL", f"sqlite:///{ROOT}/var/walkthrough.db")
os.environ.setdefault("MIP_OBJECT_STORE", str(ROOT / "var" / "walkthrough-store"))
os.environ.setdefault("MIP_SEED_DEMO", "false")

from app.core.enums import Criticality, ProjectState, VerificationStatus
from app.core.errors import GateBlocked, PermissionDenied
from app.core.security import totp_now
from app.db import SessionLocal, create_all
from app.models.factory import (
    FixtureVersion,
    MachineVersion,
    Material,
    PostProcessorVersion,
)
from app.models.identity import User
from app.models.project import PartProject
from app.seed.demo import sample_files, seed
from app.services import (
    economics,
    evidence,
    learning,
    planning,
    policy,
    postprocess,
    verification,
    workflow,
)
from app.services import (
    geometry as geometry_service,
)
from app.services import (
    release as release_service,
)
from sqlalchemy import select

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def step(number: str, title: str) -> None:
    print(f"\n{BOLD}[{number}] {title}{RESET}")


def ok(message: str) -> None:
    print(f"  {GREEN}OK{RESET}  {message}")


def note(message: str) -> None:
    print(f"  {DIM}--  {message}{RESET}")


def blocked(message: str) -> None:
    print(f"  {RED}BLOCKED{RESET}  {message}")


def main() -> int:
    create_all()
    tenant_id = seed()
    db = SessionLocal()

    engineer = db.execute(select(User).where(User.email == "engineer@example.com")).scalar_one()
    manufacturing = db.execute(select(User).where(User.email == "manufacturing@example.com")).scalar_one()
    admin = db.execute(select(User).where(User.email == "admin@example.com")).scalar_one()
    approver = db.execute(select(User).where(User.email == "approver@example.com")).scalar_one()
    project = db.execute(
        select(PartProject).where(PartProject.tenant_id == tenant_id, PartProject.part_number == "BRK-1042")
    ).scalar_one()

    # ---------------------------------------------------------------- 1 intake
    step("1", "Intake: upload and classify source evidence")
    workflow.transition(db, project, ProjectState.EVIDENCE_COLLECTION, actor_id=engineer.id)
    artifacts = []
    for path in sample_files().values():
        with path.open("rb") as handle:
            artifact = evidence.ingest_artifact(
                db,
                project=project,
                filename=path.name,
                stream=handle,
                uploader_id=engineer.id,
                provenance={"declared_by": engineer.email, "source": "customer package"},
            )
        artifacts.append(artifact)
        ok(f"{path.name}: {artifact.kind}, authority {artifact.authority}, sha256 {artifact.content_hash[:12]}")
    db.commit()

    for artifact in artifacts:
        created = evidence.import_extracted_observations(db, project=project, artifact=artifact, actor_id=engineer.id)
        if created:
            note(f"{artifact.filename}: {len(created)} candidate observations imported")
    db.commit()

    # ------------------------------------------------------- 2 reconstruction
    step("2", "Reconstruct: build provisional geometry from the evidence")
    geometry = geometry_service.reconstruct(
        db, project=project, artifacts=artifacts, known_dimensions={}, actor_id=engineer.id
    )
    db.commit()
    ok(
        f"geometry revision {geometry.revision}, scale from {geometry.scale_source}, "
        f"{len(geometry.part_model['features'])} features, hash {geometry.content_hash[:12]}"
    )
    note(f"quality notes: {'; '.join(geometry.quality_report.get('notes', []))[:150]}")

    # ------------------------------------------------- 3 engineering review
    step("3", "Engineering review: add the features the evidence cannot establish")
    from app.engines.partmodel import Feature, PartModel

    model = PartModel.from_dict(geometry.part_model)
    model.features.append(
        Feature(
            key="P1",
            feature_type="Pocket",
            label="P1 central pocket",
            params={"shape": "rect", "center": [0.0, 0.0], "size": [60.0, 40.0], "corner_radius": 6.0, "depth": 8.0, "top_z": model.z_top},
            criticality=Criticality.QUALITY,
            status=VerificationStatus.CONFIRMED,
            confidence=0.97,
            tolerance={"depth_plus": 0.05, "depth_minus": 0.05},
            surface_finish_ra=1.6,
        )
    )
    for feature in model.features:
        if feature.feature_type == "Hole":
            feature.status = VerificationStatus.CONFIRMED
            feature.confidence = 0.96
            feature.criticality = Criticality.FUNCTION
    geometry = geometry_service.edit_features(
        db,
        project=project,
        geometry=geometry,
        part_model=model,
        actor_id=engineer.id,
        reason="Pocket confirmed against the drawing; hole diameters verified by first-piece measurement",
    )
    db.commit()
    ok(f"geometry revision {geometry.revision} created; revision {geometry.revision - 1} preserved")

    conflicts = policy.evaluate_geometry(db, geometry)
    for finding in conflicts.findings:
        note(f"{finding.severity.value} {finding.code}: {finding.message[:110]}")

    # Resolve every open conflict and disposition the pending observations.
    from app.models.project import EvidenceConflict, EvidenceObservation

    for conflict in db.execute(
        select(EvidenceConflict).where(EvidenceConflict.project_id == project.id, EvidenceConflict.resolved.is_(False))
    ).scalars().all():
        evidence.resolve_conflict(
            db,
            conflict,
            chosen_observation_id=conflict.authoritative_observation_id,
            actor_id=engineer.id,
            reason="Authoritative source accepted after engineering review",
        )
        note(f"conflict on {conflict.attribute} resolved in favour of the authoritative source")
    for observation in db.execute(
        select(EvidenceObservation).where(
            EvidenceObservation.geometry_version_id == geometry.id,
            EvidenceObservation.disposition == "Pending",
        )
    ).scalars().all():
        from app.core.enums import Disposition

        evidence.disposition_observation(
            db, observation, disposition=Disposition.ACCEPT, actor_id=engineer.id, reason="Reviewed against the drawing"
        )
    db.commit()

    geometry_service.approve(db, geometry, actor_id=engineer.id, note="Reviewed against drawing BRK-1042 rev C")
    db.commit()
    ok(f"geometry approved by {engineer.email}")

    # ------------------------------------------------------------- 4 planning
    step("4", "Plan: compare machines and generate route candidates")
    material = db.execute(select(Material).where(Material.code == "AL6082-T6")).scalar_one()
    fixture = db.execute(select(FixtureVersion).where(FixtureVersion.code == "VISE-160")).scalar_one()
    machines = list(db.execute(select(MachineVersion).where(MachineVersion.tenant_id == tenant_id)).scalars())

    workflow.advance_to(db, project, ProjectState.PLANNING, actor_id=manufacturing.id, reason="Geometry approved")
    plans = planning.generate_plans(
        db,
        project=project,
        geometry=geometry,
        material=material,
        fixture=fixture,
        machines=machines,
        objective="balanced",
        actor_id=manufacturing.id,
    )
    db.commit()

    from app.models.planning import MachineFeasibility

    for row in db.execute(select(MachineFeasibility).where(MachineFeasibility.project_id == project.id)).scalars():
        machine = db.get(MachineVersion, row.machine_version_id)
        if row.feasible:
            ok(f"{machine.code} feasible")
        else:
            blocked(f"{machine.code}: {row.cause_code} - {row.binding_constraint}")

    plan = plans[0]
    ok(f"selected plan {plan.label}: {len(plan.setups)} setup(s), {sum(len(s.operations) for s in plan.setups)} operations")
    for setup in sorted(plan.setups, key=lambda s: s.sequence):
        for operation in sorted(setup.operations, key=lambda o: o.sequence):
            from app.models.factory import ToolAssemblyVersion

            tool = db.get(ToolAssemblyVersion, operation.tool_assembly_id)
            note(f"{setup.sequence}.{operation.sequence} {operation.operation_type:<18} {tool.code:<10} {operation.parameters.get('label', '')}")
    for entry in plan.decision_record.get("unplanned", []):
        note(f"not planned: {entry.get('feature')} - {entry.get('reason')[:100]}")

    # ------------------------------------------------------------------ 5 CAM
    step("5", "CAM: generate toolpaths with deterministic cutting parameters")
    paths = planning.generate_toolpaths(db, plan=plan, actor_id=manufacturing.id)
    db.commit()
    ok(f"{len(paths)} toolpaths, {sum(p.move_count for p in paths)} moves, {sum(p.cutting_length_mm for p in paths):.0f} mm of cutting")
    for setup in sorted(plan.setups, key=lambda s: s.sequence):
        for operation in sorted(setup.operations, key=lambda o: o.sequence):
            parameters = operation.parameters
            if "rpm" in parameters:
                note(
                    f"{parameters.get('label', operation.operation_type)[:34]:<34} "
                    f"S{parameters['rpm']:>7.0f}  F{parameters['feed_mm_min']:>7.0f}  "
                    f"ap {parameters['ap_mm']:>5.2f}  ae {parameters['ae_mm']:>5.2f}  "
                    f"{parameters['power_kw']:.2f} kW"
                )

    # ----------------------------------------------------------- 6 simulation
    step("6", "Simulate: stock removal, kinematics and collision")
    run = verification.run_simulation(db, plan=plan, voxel_pitch=1.0, actor_id=manufacturing.id)
    db.commit()
    if run.passed:
        ok(f"simulation passed, cycle time {run.cycle_time_seconds:.1f} s")
    else:
        blocked(f"simulation failed: {run.event_counts}")
    note(f"time breakdown: {run.time_breakdown.get('cutting_s')} s cutting, {run.time_breakdown.get('rapid_s')} s rapid, {run.time_breakdown.get('tool_change_s')} s tool changes")
    for event in run.events:
        marker = blocked if event["severity"] in ("S1", "S2") else note
        marker(f"{event['severity']} {event['code']}: {event['message'][:110]}")
    note(f"stock comparison: {run.stock_comparison}")

    # ----------------------------------------------------------------- 7 cost
    step("7", "Cost: decompose the estimate")
    estimate = economics.estimate(db, plan=plan, simulation=run, actor_id=manufacturing.id)
    db.commit()
    ok(f"unit cost {estimate.unit_cost:.2f} {estimate.currency}, price {estimate.unit_price:.2f} at quantity {estimate.quantity}")
    for key, value in estimate.breakdown.items():
        note(f"{key:<14} {value:>10.2f}")

    # ------------------------------------------------------- 8 postprocessing
    step("8", "Postprocess: FANUC output through a certified post")
    post = db.execute(select(PostProcessorVersion).where(PostProcessorVersion.code == "FANUC-VMC01")).scalar_one()
    machine = db.get(MachineVersion, plan.machine_version_id)

    try:
        postprocess.postprocess(db, plan=plan, simulation=run, post=post, actor_id=manufacturing.id)
        blocked("an uncertified post produced output - this must never happen")
        return 1
    except GateBlocked as refusal:
        ok(f"uncertified post refused: {refusal.message}")
        db.rollback()

    wrong_post = db.execute(select(PostProcessorVersion).where(PostProcessorVersion.code == "FANUC-VMC02")).scalar_one()
    postprocess.certify_post(
        db,
        wrong_post,
        machine=db.execute(select(MachineVersion).where(MachineVersion.code == "VMC-02")).scalar_one(),
        actor_id=admin.id,
        note="Certified against trunnion cell 2 test programs",
        test_program_hashes=["a" * 64],
    )
    db.commit()
    try:
        postprocess.postprocess(db, plan=plan, simulation=run, post=wrong_post, actor_id=manufacturing.id)
        blocked("a post certified for another machine produced output - this must never happen")
        return 1
    except GateBlocked as refusal:
        codes = {f["code"] for f in refusal.detail["findings"]}
        ok(f"machine/post mismatch refused: {', '.join(sorted(codes))}")
        db.rollback()

    postprocess.certify_post(
        db,
        post,
        machine=machine,
        actor_id=admin.id,
        note="Certified against Robodrill cell 1 approved test programs",
        test_program_hashes=["b" * 64, "c" * 64],
    )
    db.commit()
    program = postprocess.postprocess(db, plan=plan, simulation=run, post=post, actor_id=manufacturing.id)
    db.commit()
    if program.validation_passed:
        ok(f"{program.program_number}: {program.line_count} blocks, validation passed, hash {program.program_hash[:12]}")
    else:
        blocked(f"NC validation failed: {program.max_severity}")
        for finding in program.validations[:6]:
            note(f"{finding['severity']} {finding['code']}: {finding['message'][:110]}")

    # -------------------------------------------------------------- 9 release
    step("9", "Release: gates, second factor and the immutable package")
    workflow.advance_at_least(db, project, ProjectState.RELEASE_REVIEW, actor_id=manufacturing.id)
    db.commit()
    release = release_service.prepare(
        db, project=project, plan=plan, simulation=run, nc_programs=[program], actor_id=manufacturing.id
    )
    db.commit()
    gate = release.gate_results[-1]
    if gate["passed"]:
        ok(f"release gate passed; package hash {release.package_hash[:16]}")
    else:
        blocked(f"release gate: {gate['max_severity']}")
        for finding in gate["findings"]:
            note(f"{finding['severity']} {finding['code']}: {finding['message'][:110]}")

    try:
        release_service.approve(
            db,
            release=release,
            approver=approver,
            totp_code="000000",
            statement="Attempted approval with an invalid second factor",
            checklist={item["key"]: True for item in release_service.release_checklist(db, release)},
        )
        blocked("an invalid second factor was accepted - this must never happen")
        return 1
    except PermissionDenied as refusal:
        ok(f"invalid second factor refused: {refusal.message}")
        db.rollback()

    release_service.approve(
        db,
        release=release,
        approver=approver,
        totp_code=totp_now(approver.totp_secret),
        statement="Reviewed simulation, tool list and workholding. Approved for controlled proof-out.",
        checklist={item["key"]: True for item in release_service.release_checklist(db, release)},
    )
    db.commit()
    ok(f"released revision {release.revision} by {approver.email}, signature {release.package_signature[:16]}")
    data, filename, controlled = release_service.package_bytes(db, release)
    ok(f"package {filename}: {len(data)} bytes, controlled={controlled}")

    # ------------------------------------------------------------ 10 feedback
    step("10", "Produce and learn: record actuals and inspection")
    for index, (cycle, result) in enumerate([(None, "Good"), (None, "Good"), (None, "Good")]):
        actual = run.cycle_time_seconds * (1.24 + index * 0.02)
        machine_run = learning.record_run(
            db,
            release=release,
            nc_program_id=program.id,
            payload={
                "actual_cycle_seconds": actual,
                "actual_setup_seconds": 1500,
                "operator": "N. Silva",
                "result": result,
                "pieces": 5,
                "nc_program_hash": program.program_hash,
            },
            actor_id=approver.id,
        )
        db.commit()
    ok(f"3 runs recorded against released program {program.program_number}")

    learning.record_inspection(
        db,
        project=project,
        run=machine_run,
        payload={"feature_key": "P1", "characteristic": "depth", "nominal": 8.0, "actual": 8.02, "tolerance_plus": 0.05, "tolerance_minus": 0.05, "instrument": {"type": "depth micrometer"}},
        actor_id=approver.id,
    )
    db.commit()
    report = learning.variance_report(db, project=project)
    ok(f"median cycle-time variance {report['summary']['median_variance_percent']}% over {report['summary']['sample_size']} runs")
    note(report["summary"]["note"])

    proposals = learning.propose_rules(db, project=project, actor_id=manufacturing.id)
    db.commit()
    for proposal in proposals:
        ok(f"proposal {proposal.scope}: {proposal.current_value} -> {proposal.proposed_value} (status {proposal.status})")
        note("no production rule changes until a manufacturing engineer promotes it")

    print(f"\n{BOLD}Walkthrough complete: intake through signed release and feedback.{RESET}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
