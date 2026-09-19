"""Release gates, electronic signature and the immutable release package.

A release is the point where software stops advising and a human takes
responsibility. Everything here is built so that decision is recorded, bounded
and reproducible (FR-REL-001, FR-REL-002).
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import audit, events
from app.core.enums import Gate, LifecycleStatus, ProjectState, ReleaseStatus
from app.core.errors import GateBlocked, PermissionDenied, ValidationFailed
from app.core.hashing import canonical_json, merkle_manifest, sha256_bytes
from app.core.rbac import Permission, has_permission
from app.core.security import sign_payload, verify_totp
from app.core.storage import get_store
from app.models.factory import MachineVersion, PostProcessorVersion, ToolAssemblyVersion
from app.models.geometry import GeometryVersion
from app.models.identity import User
from app.models.planning import ManufacturingPlan, ToolpathVersion
from app.models.project import PartProject
from app.models.verification import (
    Approval,
    CostEstimate,
    GateEvaluationRecord,
    NCProgram,
    Release,
    SimulationRun,
)
from app.services import policy, workflow


def prepare(
    db: Session,
    *,
    project: PartProject,
    plan: ManufacturingPlan,
    simulation: SimulationRun,
    nc_programs: list[NCProgram],
    actor_id: str,
) -> Release:
    """Create the release candidate and record the gate evaluation."""
    evaluation = policy.evaluate_release(
        db, project=project, plan=plan, simulation=simulation, nc_programs=nc_programs
    )
    _record_gate(db, project, evaluation, actor_id)

    revision = (
        db.execute(
            select(func.coalesce(func.max(Release.revision), 0)).where(Release.project_id == project.id)
        ).scalar_one()
        + 1
    )
    release = Release(
        tenant_id=project.tenant_id,
        project_id=project.id,
        plan_id=plan.id,
        revision=revision,
        nc_program_ids=[p.id for p in nc_programs],
        gate_results=[evaluation.to_dict()],
        status=ReleaseStatus.IN_REVIEW.value if evaluation.passed else ReleaseStatus.CANDIDATE.value,
    )
    release.manifest = build_manifest(db, release=release, plan=plan, simulation=simulation, nc_programs=nc_programs)
    release.package_hash = merkle_manifest(
        {name: entry["hash"] for name, entry in release.manifest["artifacts"].items()}
    )
    db.add(release)
    db.flush()
    return release


def build_manifest(
    db: Session,
    *,
    release: Release,
    plan: ManufacturingPlan,
    simulation: SimulationRun,
    nc_programs: list[NCProgram],
) -> dict[str, Any]:
    """Hash every input so a released program can be reproduced and audited."""
    project = db.get(PartProject, plan.project_id)
    geometry = db.get(GeometryVersion, plan.geometry_version_id)
    machine = db.get(MachineVersion, plan.machine_version_id)

    artifacts: dict[str, dict[str, Any]] = {
        "geometry": {"id": geometry.id, "hash": geometry.content_hash, "revision": geometry.revision},
        "plan": {"id": plan.id, "hash": plan.content_hash, "label": plan.label},
        "simulation": {
            "id": simulation.id,
            "hash": simulation.content_hash,
            "identity": simulation.identity_hash,
            "engine": simulation.engine_version,
            "passed": simulation.passed,
        },
        "machine": {"id": machine.id, "hash": machine.content_hash or machine.id, "code": machine.code},
    }

    toolpaths = db.execute(select(ToolpathVersion).where(ToolpathVersion.plan_id == plan.id)).scalars().all()
    artifacts["toolpaths"] = {
        "hash": merkle_manifest({t.id: t.content_hash for t in toolpaths}),
        "count": len(toolpaths),
    }

    tool_ids = {operation.tool_assembly_id for setup in plan.setups for operation in setup.operations}
    tool_list = []
    for tool_id in sorted(tool_ids):
        tool = db.get(ToolAssemblyVersion, tool_id)
        if tool:
            tool_list.append(
                {
                    "code": tool.code,
                    "revision": tool.revision,
                    "hash": tool.content_hash or tool.id,
                    "description": tool.description,
                    "gauge_length_mm": tool.gauge_length_mm,
                }
            )
    artifacts["tool_list"] = {"hash": merkle_manifest({t["code"]: t["hash"] for t in tool_list}), "tools": tool_list}

    programs = []
    for program in nc_programs:
        post = db.get(PostProcessorVersion, program.post_version_id)
        programs.append(
            {
                "id": program.id,
                "program_number": program.program_number,
                "hash": program.program_hash,
                "ir_hash": program.ir_hash,
                "line_count": program.line_count,
                "post": f"{post.code} r{post.revision}" if post else None,
                "post_runtime_hash": post.runtime_hash if post else None,
                "validation_passed": program.validation_passed,
            }
        )
    artifacts["nc_programs"] = {"hash": merkle_manifest({p["program_number"]: p["hash"] for p in programs}), "programs": programs}

    cost = db.execute(
        select(CostEstimate).where(CostEstimate.plan_id == plan.id).order_by(CostEstimate.created_at.desc()).limit(1)
    ).scalar_one_or_none()
    if cost:
        artifacts["cost"] = {"id": cost.id, "hash": sha256_bytes(canonical_json(cost.breakdown)), "unit_price": cost.unit_price}

    return {
        "schema_version": "1.0.0",
        "project": {
            "id": project.id,
            "part_number": project.part_number,
            "revision": project.revision,
            "quantity": project.quantity,
        },
        "release": {"revision": release.revision, "created_at": datetime.now(UTC).isoformat()},
        "artifacts": artifacts,
    }


def approve(
    db: Session,
    *,
    release: Release,
    approver: User,
    totp_code: str,
    statement: str,
    checklist: dict[str, bool],
) -> Release:
    """Sign a release.

    Requires the release role, a verified second factor, a signed checklist and
    a gate evaluation that passes at signing time - not at preparation time.
    """
    if not has_permission(approver.roles or [], Permission.NC_RELEASE):
        raise PermissionDenied(
            "Signing an NC release requires the release approver role",
            detail={"required": Permission.NC_RELEASE.value},
        )
    if not approver.mfa_enabled or not approver.totp_secret:
        raise PermissionDenied(
            "Release approval requires a second factor. Enrol MFA before signing.", code="mfa_required"
        )
    if not verify_totp(approver.totp_secret, totp_code):
        raise PermissionDenied("The second factor code was not accepted", code="mfa_invalid")

    unchecked = [item for item, value in (checklist or {}).items() if not value]
    if unchecked or not checklist:
        raise ValidationFailed(
            "Every checklist item must be signed before release",
            detail={"unchecked": unchecked or ["checklist_missing"]},
            code="checklist_incomplete",
        )
    if not statement.strip():
        raise ValidationFailed("An approval statement is required", code="statement_required")

    project = db.get(PartProject, release.project_id)
    plan = db.get(ManufacturingPlan, release.plan_id)
    programs = [db.get(NCProgram, pid) for pid in release.nc_program_ids]
    simulation = db.get(SimulationRun, programs[0].simulation_run_id) if programs and programs[0] else None
    if simulation is None:
        raise ValidationFailed("The release has no simulation to verify against", code="no_simulation")

    # Re-evaluate now. Anything that changed since preparation blocks the signature.
    evaluation = policy.evaluate_release(
        db,
        project=project,
        plan=plan,
        simulation=simulation,
        nc_programs=[p for p in programs if p],
        approver_id=approver.id,
    )
    _record_gate(db, project, evaluation, approver.id)
    if not evaluation.passed:
        raise GateBlocked(
            f"{Gate.NC_RELEASE.value} did not pass; {evaluation.blocks} remains blocked",
            detail=evaluation.to_dict(),
        )

    # Confirm nothing changed since the manifest was built.
    current = build_manifest(db, release=release, plan=plan, simulation=simulation, nc_programs=[p for p in programs if p])
    current_hash = merkle_manifest({name: entry["hash"] for name, entry in current["artifacts"].items()})
    if release.package_hash and current_hash != release.package_hash:
        raise GateBlocked(
            "Inputs changed after this release candidate was prepared",
            detail={"prepared_hash": release.package_hash, "current_hash": current_hash},
            code="artifacts_changed",
        )

    release.manifest = current
    release.package_hash = current_hash
    release.gate_results = [*(release.gate_results or []), evaluation.to_dict()]
    release.status = ReleaseStatus.RELEASED.value
    release.released_at = datetime.now(UTC)
    release.package_signature = sign_payload(canonical_json({"manifest": current, "hash": current_hash}))
    release.version += 1

    signature = sign_payload(
        canonical_json(
            {
                "release": release.id,
                "package_hash": current_hash,
                "actor": approver.id,
                "at": release.released_at.isoformat(),
            }
        )
    )
    db.add(
        Approval(
            tenant_id=release.tenant_id,
            project_id=release.project_id,
            gate=Gate.NC_RELEASE.value,
            target_kind="release",
            target_id=release.id,
            target_hash=current_hash,
            actor_id=approver.id,
            actor_role="nc_release_approver",
            decision="approved",
            statement=statement,
            mfa_verified=True,
            signature=signature,
        )
    )

    for program in programs:
        if program:
            program.status = LifecycleStatus.APPROVED.value
            db.add(program)

    release.package_storage_key = _write_package(db, release, [p for p in programs if p])
    db.add(release)

    _supersede_previous(db, release)
    workflow.advance_at_least(db, project, ProjectState.RELEASED, actor_id=approver.id)

    audit.record(
        db,
        tenant_id=release.tenant_id,
        object_kind="release",
        object_id=release.id,
        action="approve",
        actor_id=approver.id,
        after={"package_hash": current_hash, "signature": signature, "checklist": checklist},
        reason=statement,
    )
    events.publish(
        db,
        tenant_id=release.tenant_id,
        topic=events.Topic.RELEASE_STATUS_CHANGED,
        project_id=release.project_id,
        actor_id=approver.id,
        payload={
            "release_id": release.id,
            "prior_state": ReleaseStatus.IN_REVIEW.value,
            "new_state": ReleaseStatus.RELEASED.value,
            "revision": release.revision,
            "package_hash": current_hash,
        },
    )
    return release


def _supersede_previous(db: Session, release: Release) -> None:
    previous = db.execute(
        select(Release).where(
            Release.project_id == release.project_id,
            Release.id != release.id,
            Release.status == ReleaseStatus.RELEASED.value,
        )
    ).scalars().all()
    for prior in previous:
        prior.status = ReleaseStatus.SUPERSEDED.value
        prior.superseded_by_id = release.id
        db.add(prior)
        events.publish(
            db,
            tenant_id=prior.tenant_id,
            topic=events.Topic.RELEASE_STATUS_CHANGED,
            project_id=prior.project_id,
            payload={
                "release_id": prior.id,
                "prior_state": ReleaseStatus.RELEASED.value,
                "new_state": ReleaseStatus.SUPERSEDED.value,
                "superseded_by": release.id,
            },
        )


def _write_package(db: Session, release: Release, programs: list[NCProgram]) -> str:
    """Zip the controlled package: manifest, signature, NC programs and IR."""
    store = get_store()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", canonical_json(release.manifest).decode())
        archive.writestr(
            "SIGNATURE.txt",
            f"package_hash={release.package_hash}\nsignature={release.package_signature}\n"
            f"released_at={release.released_at.isoformat() if release.released_at else ''}\n",
        )
        for program in programs:
            archive.writestr(f"nc/{program.program_number}.nc", store.get_bytes(program.storage_key).decode())
            archive.writestr(f"ir/{program.program_number}.ir.json", store.get_bytes(program.ir_storage_key).decode())
            archive.writestr(
                f"validation/{program.program_number}.json",
                canonical_json({"passed": program.validation_passed, "findings": program.validations}).decode(),
            )
        archive.writestr(
            "README.txt",
            "Controlled release package.\n"
            "Any modification to a file in this archive invalidates the package hash and the\n"
            "contents become an Uncontrolled Copy. Verify package_hash before loading to a machine.\n",
        )
    key, _ = store.put_bytes(release.tenant_id, f"releases/{release.project_id}", buffer.getvalue(), ".zip")
    return key


def reject(db: Session, release: Release, *, actor_id: str, reason: str) -> Release:
    if not reason.strip():
        raise ValidationFailed("A rejection requires a reason", code="reason_required")
    release.status = ReleaseStatus.REJECTED.value
    db.add(release)
    db.add(
        Approval(
            tenant_id=release.tenant_id,
            project_id=release.project_id,
            gate=Gate.NC_RELEASE.value,
            target_kind="release",
            target_id=release.id,
            actor_id=actor_id,
            decision="rejected",
            statement=reason,
            reason=reason,
        )
    )
    events.publish(
        db,
        tenant_id=release.tenant_id,
        topic=events.Topic.RELEASE_STATUS_CHANGED,
        project_id=release.project_id,
        actor_id=actor_id,
        payload={"release_id": release.id, "new_state": ReleaseStatus.REJECTED.value, "reason": reason},
    )
    return release


def package_bytes(db: Session, release: Release) -> tuple[bytes, str, bool]:
    """Return the package, its filename and whether it is a controlled copy.

    A package that is not in the Released state is still downloadable for
    review, but it is watermarked as an uncontrolled copy so it cannot be
    mistaken for production truth (PRD 9.1, GET release package).
    """
    store = get_store()
    controlled = release.status == ReleaseStatus.RELEASED.value
    project = db.get(PartProject, release.project_id)
    name = f"{project.part_number}-rev{project.revision}-release{release.revision}"

    if controlled and release.package_storage_key:
        return store.get_bytes(release.package_storage_key), f"{name}.zip", True

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "UNCONTROLLED-COPY.txt",
            "UNCONTROLLED COPY - NOT FOR PRODUCTION\n"
            f"Release status: {release.status}\n"
            "This package has not passed the release gates and carries no signature.\n"
            "Do not load these programs on a machine.\n",
        )
        archive.writestr("manifest.json", canonical_json(release.manifest or {}).decode())
        archive.writestr("gates.json", canonical_json(release.gate_results or []).decode())
        for program_id in release.nc_program_ids or []:
            program = db.get(NCProgram, program_id)
            if not program:
                continue
            body = store.get_bytes(program.storage_key).decode()
            archive.writestr(
                f"nc/{program.program_number}.nc.txt",
                "(UNCONTROLLED COPY - NOT FOR PRODUCTION)\n" + body,
            )
    return buffer.getvalue(), f"{name}-UNCONTROLLED.zip", False


def _record_gate(db: Session, project: PartProject, evaluation: policy.GateEvaluation, actor_id: str | None) -> None:
    db.add(
        GateEvaluationRecord(
            tenant_id=project.tenant_id,
            project_id=project.id,
            gate=evaluation.gate.value,
            target_kind=evaluation.target_kind,
            target_id=evaluation.target_id,
            passed=evaluation.passed,
            blocks=evaluation.blocks,
            findings=[f.to_dict() for f in evaluation.findings],
            max_severity=evaluation.max_severity,
            evaluated_by_id=actor_id,
        )
    )


def release_checklist(db: Session, release: Release) -> list[dict[str, Any]]:
    """The items an approver must sign, derived from the current gate state."""
    plan = db.get(ManufacturingPlan, release.plan_id)
    programs = [db.get(NCProgram, pid) for pid in release.nc_program_ids or []]
    simulation = db.get(SimulationRun, programs[0].simulation_run_id) if programs and programs[0] else None
    geometry = db.get(GeometryVersion, plan.geometry_version_id) if plan else None

    return [
        {
            "key": "geometry_reviewed",
            "label": "Geometry and critical dimensions have been reviewed",
            "auto_state": bool(geometry and geometry.status == LifecycleStatus.APPROVED.value),
            "evidence": f"Geometry revision {geometry.revision}" if geometry else None,
        },
        {
            "key": "simulation_reviewed",
            "label": "Simulation result and all warnings have been reviewed",
            "auto_state": bool(simulation and simulation.passed and not simulation.stale),
            "evidence": f"{(simulation.event_counts if simulation else {}) or 'no events'}",
        },
        {
            "key": "workholding_confirmed",
            "label": "Workholding and fixture model match the physical setup",
            "auto_state": False,
            "evidence": "Requires physical confirmation by the approver",
        },
        {
            "key": "tool_list_confirmed",
            "label": "Tool list, gauge lengths and offsets are loaded on the machine",
            "auto_state": False,
            "evidence": "Requires physical confirmation by the approver",
        },
        {
            "key": "post_certified",
            "label": "Machine and postprocessor pair is certified and current",
            "auto_state": all(p and p.validation_passed for p in programs),
            "evidence": f"{len(programs)} program(s) validated",
        },
        {
            "key": "proof_out_planned",
            "label": "First proof-out will run single block with feed override and first-piece inspection",
            "auto_state": False,
            "evidence": "Factory proof-out procedure applies",
        },
    ]
