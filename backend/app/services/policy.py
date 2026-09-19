"""The gate and severity policy engine (PRD 5.2, 5.3).

This is the component that says no. Gates are evaluated from persisted state
rather than from whatever the caller claims, so a client cannot talk its way
past one. An S1 finding is never waivable; every other finding may be waived
only by an authorised engineer with a recorded rationale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import (
    GATE_BLOCKS,
    RELEASE_CRITICAL,
    VERIFIED_STATUSES,
    Disposition,
    Gate,
    LifecycleStatus,
    Severity,
    VerificationStatus,
)
from app.models.factory import MachineVersion, PostProcessorVersion
from app.models.geometry import GeometryVersion, ManufacturingFeature
from app.models.identity import Tenant
from app.models.planning import ManufacturingPlan
from app.models.project import EvidenceConflict, EvidenceObservation, PartProject, Waiver
from app.models.verification import Approval, NCProgram, SimulationRun


@dataclass
class Finding:
    """One reason a gate did or did not pass.

    Every finding names the affected object, the severity, the consequence and
    the recommended resolution, because a warning without those is not
    actionable (PRD 6.1).
    """

    code: str
    severity: Severity
    message: str
    consequence: str
    recommendation: str
    object_ref: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    waived_by: str | None = None

    @property
    def blocking(self) -> bool:
        """Whether this finding stops the gate.

        An S1 finding blocks regardless of any waiver on it. The invariant lives
        here rather than only in the waiver-application path, so a finding
        constructed or mutated anywhere else cannot open a hole in it.
        """
        if not self.severity.blocks_release:
            return False
        if not self.severity.waivable:
            return True
        return not self.waived_by

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "consequence": self.consequence,
            "recommendation": self.recommendation,
            "object_ref": self.object_ref,
            "detail": self.detail,
            "waived_by": self.waived_by,
            "waivable": self.severity.waivable,
            "blocking": self.blocking,
        }


@dataclass
class GateEvaluation:
    gate: Gate
    findings: list[Finding] = field(default_factory=list)
    target_kind: str = ""
    target_id: str | None = None

    @property
    def passed(self) -> bool:
        return not any(f.blocking for f in self.findings)

    @property
    def blocks(self) -> str:
        return GATE_BLOCKS[self.gate]

    @property
    def max_severity(self) -> str | None:
        for severity in (Severity.S1_STOP, Severity.S2_ENGINEER, Severity.S3_WARNING, Severity.S4_ADVISORY):
            if any(f.severity is severity for f in self.findings):
                return severity.value
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate.value,
            "passed": self.passed,
            "blocks": self.blocks,
            "max_severity": self.max_severity,
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "findings": [f.to_dict() for f in self.findings],
        }


def _waivers(db: Session, project_id: str, gate: Gate) -> dict[str, Waiver]:
    rows = db.execute(
        select(Waiver).where(Waiver.project_id == project_id, Waiver.gate == gate.value, Waiver.revoked.is_(False))
    ).scalars()
    return {f"{w.finding_code}:{w.object_ref or ''}": w for w in rows}


def _apply_waivers(findings: list[Finding], waivers: dict[str, Waiver]) -> list[Finding]:
    for finding in findings:
        if not finding.severity.waivable:
            continue  # S1 can never be waived (PRD 5.3)
        waiver = waivers.get(f"{finding.code}:{finding.object_ref or ''}") or waivers.get(f"{finding.code}:")
        if waiver:
            finding.waived_by = waiver.id
            finding.detail = {**finding.detail, "waiver_rationale": waiver.rationale}
    return findings


# --------------------------------------------------------------- geometry gate
def evaluate_geometry(db: Session, geometry: GeometryVersion) -> GateEvaluation:
    tenant = db.get(Tenant, geometry.tenant_id)
    threshold = tenant.critical_confidence_threshold if tenant else 0.9
    findings: list[Finding] = []

    if geometry.units not in ("mm", "inch"):
        findings.append(
            Finding(
                "units_unknown",
                Severity.S1_STOP,
                f"Geometry units are recorded as {geometry.units!r}",
                "Every dimension downstream would be scaled wrongly",
                "Set the geometry units explicitly; unit conversion is never inferred",
                geometry.id,
            )
        )

    if not geometry.scale_established:
        findings.append(
            Finding(
                "scale_not_established",
                Severity.S1_STOP,
                "No traceable dimension or calibration reference established the scale of this geometry",
                "Photo-derived geometry without a scale reference is not manufacturing geometry",
                "Add a known dimension or a calibration target measurement, then re-run reconstruction",
                geometry.id,
            )
        )

    stock = (geometry.part_model or {}).get("stock") or {}
    if not stock.get("min") or not stock.get("max"):
        findings.append(
            Finding(
                "stock_envelope_missing",
                Severity.S2_ENGINEER,
                "The geometry has no stock envelope",
                "Planning cannot choose stock or verify machine capacity",
                "Define the stock block on the geometry version",
                geometry.id,
            )
        )

    features = db.execute(
        select(ManufacturingFeature).where(ManufacturingFeature.geometry_version_id == geometry.id)
    ).scalars().all()

    for feature in features:
        if feature.criticality not in RELEASE_CRITICAL:
            continue
        if feature.status == VerificationStatus.UNKNOWN:
            findings.append(
                Finding(
                    "critical_attribute_unknown",
                    Severity.S2_ENGINEER,
                    f"{feature.criticality} feature {feature.label or feature.stable_key} is Unknown",
                    "Planning would proceed on a value nobody established",
                    "Verify the feature from evidence, or record an authorised waiver with rationale",
                    feature.stable_key,
                    {"feature_type": feature.feature_type},
                )
            )
        elif feature.status not in VERIFIED_STATUSES and feature.confidence < threshold:
            findings.append(
                Finding(
                    "critical_attribute_low_confidence",
                    Severity.S2_ENGINEER,
                    (
                        f"{feature.criticality} feature {feature.label or feature.stable_key} is "
                        f"{feature.status} at {feature.confidence:.2f} confidence, below the tenant "
                        f"threshold of {threshold:.2f}"
                    ),
                    "A release-critical dimension would rest on inference",
                    "Measure or confirm the feature, then set its status to Measured or Confirmed",
                    feature.stable_key,
                    {"confidence": feature.confidence, "threshold": threshold},
                )
            )
        if feature.feature_type == "Thread candidate" and not feature.thread_spec:
            findings.append(
                Finding(
                    "thread_unresolved",
                    Severity.S2_ENGINEER,
                    f"Thread specification for {feature.label or feature.stable_key} is not established",
                    "Tapping cannot be planned and the hole may be finished at the wrong size",
                    "Confirm the thread designation and pitch from the drawing or a gauge",
                    feature.stable_key,
                )
            )

    unresolved = db.execute(
        select(EvidenceConflict).where(
            EvidenceConflict.project_id == geometry.project_id, EvidenceConflict.resolved.is_(False)
        )
    ).scalars().all()
    for conflict in unresolved:
        findings.append(
            Finding(
                "evidence_conflict_open",
                Severity(conflict.severity),
                f"Unresolved evidence conflict on {conflict.attribute}"
                + (f" of {conflict.feature_key}" if conflict.feature_key else ""),
                "Two sources disagree and the system will not choose between them silently",
                "Review both observations and record a disposition for the authoritative one",
                conflict.feature_key or conflict.attribute,
                {"delta": conflict.delta, "summary": conflict.summary},
            )
        )

    pending = db.execute(
        select(EvidenceObservation).where(
            EvidenceObservation.geometry_version_id == geometry.id,
            EvidenceObservation.criticality.in_([c.value for c in RELEASE_CRITICAL]),
            EvidenceObservation.disposition == Disposition.PENDING,
            EvidenceObservation.status.in_(
                [VerificationStatus.INFERRED.value, VerificationStatus.VERIFICATION_REQUIRED.value]
            ),
        )
    ).scalars().all()
    for observation in pending:
        findings.append(
            Finding(
                "observation_awaiting_disposition",
                Severity.S3_WARNING,
                f"{observation.attribute} on {observation.feature_key or 'the part'} is awaiting engineer disposition",
                "The value is used for planning while still marked provisional",
                "Accept, edit or reject the observation in the engineering review workspace",
                observation.feature_key or observation.attribute,
            )
        )

    evaluation = GateEvaluation(Gate.GEOMETRY_APPROVAL, target_kind="geometry_version", target_id=geometry.id)
    evaluation.findings = _apply_waivers(findings, _waivers(db, geometry.project_id, Gate.GEOMETRY_APPROVAL))
    return evaluation


# ------------------------------------------------------------------- plan gate
def evaluate_plan(db: Session, plan: ManufacturingPlan) -> GateEvaluation:
    findings: list[Finding] = []

    if plan.stale:
        findings.append(
            Finding(
                "plan_stale",
                Severity.S1_STOP,
                f"The plan is stale: {plan.stale_reason or 'an upstream input changed'}",
                "Toolpaths would be generated against superseded inputs",
                "Regenerate the plan from the current geometry and configuration",
                plan.id,
            )
        )

    if not plan.material_id:
        findings.append(
            Finding(
                "material_not_set",
                Severity.S1_STOP,
                "No material is assigned to the plan",
                "Cutting parameters cannot be derived and material is never inferred from appearance",
                "Select the material, or have an engineer enter and validate a rule set for it",
                plan.id,
            )
        )
    if not plan.fixture_version_id:
        findings.append(
            Finding(
                "workholding_not_set",
                Severity.S1_STOP,
                "No workholding concept is assigned to the plan",
                "Collision checking has nothing to test the tool against",
                "Select a fixture version for the plan",
                plan.id,
            )
        )

    machine = db.get(MachineVersion, plan.machine_version_id)
    if machine is None:
        findings.append(
            Finding(
                "machine_missing",
                Severity.S1_STOP,
                "The plan references a machine version that no longer exists",
                "Nothing can be verified against an unknown machine",
                "Reassign the plan to a registered machine version",
                plan.id,
            )
        )
    elif not machine.geometry_qualified:
        findings.append(
            Finding(
                "machine_geometry_unqualified",
                Severity.S3_WARNING,
                f"Machine {machine.code} geometry has not been qualified against the manufacturer data",
                "Collision results carry lower confidence than a qualified twin",
                "Complete machine qualification before relying on simulation for proof-out",
                machine.code,
            )
        )

    if not plan.setups:
        findings.append(
            Finding(
                "no_setups",
                Severity.S1_STOP,
                "The plan contains no setups",
                "There is nothing to generate toolpaths from",
                "Regenerate the plan, or add setups manually",
                plan.id,
            )
        )

    for setup in plan.setups:
        if not setup.datum_scheme:
            findings.append(
                Finding(
                    "setup_datums_missing",
                    Severity.S2_ENGINEER,
                    f"Setup {setup.sequence} has no datum scheme",
                    "The operator has no defined way to locate the part, so positions are not repeatable",
                    "Record the datum scheme for the setup",
                    setup.id,
                    {"setup": setup.name},
                )
            )
        if not setup.operations:
            findings.append(
                Finding(
                    "setup_without_operations",
                    Severity.S3_WARNING,
                    f"Setup {setup.sequence} contains no operations",
                    "The setup costs time without removing material",
                    "Remove the setup or assign operations to it",
                    setup.id,
                )
            )

    unplanned = (plan.decision_record or {}).get("unplanned") or []
    for entry in unplanned:
        blocking = entry.get("blocking")
        findings.append(
            Finding(
                "feature_not_planned",
                Severity.S2_ENGINEER if blocking else Severity.S3_WARNING,
                f"{entry.get('type', 'Feature')} {entry.get('feature')} was not planned: {entry.get('reason')}",
                "The feature would be missing from the finished part",
                "Plan the feature manually, add suitable tooling, or confirm it is produced elsewhere",
                entry.get("feature"),
                entry,
            )
        )

    evaluation = GateEvaluation(Gate.PLAN_APPROVAL, target_kind="plan", target_id=plan.id)
    evaluation.findings = _apply_waivers(findings, _waivers(db, plan.project_id, Gate.PLAN_APPROVAL))
    return evaluation


# ------------------------------------------------------------- simulation gate
def evaluate_simulation(db: Session, simulation: SimulationRun) -> GateEvaluation:
    findings: list[Finding] = []
    dispositions = {d.get("event_code"): d for d in (simulation.dispositions or [])}

    if simulation.stale:
        findings.append(
            Finding(
                "simulation_stale",
                Severity.S1_STOP,
                "The simulation is stale because an input changed after it ran",
                "A passing result no longer describes what would be machined",
                "Re-run the simulation against the current inputs",
                simulation.id,
            )
        )

    for event in simulation.events or []:
        severity = Severity(event["severity"])
        if severity is Severity.S4_ADVISORY:
            continue
        disposition = dispositions.get(event["code"])
        resolved = bool(disposition and disposition.get("decision") == "resolved")
        if severity is Severity.S1_STOP or not resolved:
            findings.append(
                Finding(
                    f"sim_{event['code']}",
                    severity,
                    event["message"],
                    event.get("consequence", ""),
                    event.get("recommendation", ""),
                    event.get("operation_id") or simulation.id,
                    {
                        "time_s": event.get("time_s"),
                        "entities": event.get("entities"),
                        "position": event.get("position"),
                        "occurrences": event.get("occurrences", 1),
                        "setup": event.get("setup"),
                    },
                )
            )

    if not simulation.events and not simulation.passed:
        findings.append(
            Finding(
                "simulation_incomplete",
                Severity.S1_STOP,
                "The simulation did not complete",
                "Nothing has been verified",
                "Re-run the simulation and inspect the job diagnostics",
                simulation.id,
            )
        )

    evaluation = GateEvaluation(Gate.SIMULATION_PASS, target_kind="simulation", target_id=simulation.id)
    evaluation.findings = _apply_waivers(findings, _waivers(db, simulation.project_id, Gate.SIMULATION_PASS))
    return evaluation


# ------------------------------------------------------------------- post gate
def evaluate_post(db: Session, post: PostProcessorVersion, machine: MachineVersion) -> GateEvaluation:
    """Certification is a triple: machine model, controller version and post version."""
    findings: list[Finding] = []

    if post.revoked:
        findings.append(
            Finding(
                "post_revoked",
                Severity.S1_STOP,
                f"Post {post.code} r{post.revision} is revoked: {post.revocation_reason or 'no reason recorded'}",
                "Output from a revoked post must not reach a machine",
                "Certify a replacement post for this machine and controller pair",
                post.code,
            )
        )
    if not post.enabled:
        findings.append(
            Finding(
                "post_not_enabled",
                Severity.S1_STOP,
                f"Post {post.code} r{post.revision} is not enabled for production use",
                "An unvalidated post could emit motion nobody verified",
                "Enable the post after certification",
                post.code,
            )
        )
    if not post.certified:
        findings.append(
            Finding(
                "post_not_certified",
                Severity.S1_STOP,
                f"Post {post.code} r{post.revision} is not certified",
                "Machine-specific syntax and behaviour have not been proven",
                "Complete postprocessor certification with the approved test programs",
                post.code,
            )
        )
    if post.machine_code != machine.code:
        findings.append(
            Finding(
                "post_machine_mismatch",
                Severity.S1_STOP,
                f"Post {post.code} is certified for {post.machine_code} but the plan targets {machine.code}",
                "Syntax, offsets or kinematic assumptions could differ between machines",
                "Select a post certified for this exact machine, or certify this pair",
                post.code,
                {"post_machine": post.machine_code, "plan_machine": machine.code},
            )
        )
    if post.controller.upper() != machine.controller.upper():
        findings.append(
            Finding(
                "post_controller_mismatch",
                Severity.S1_STOP,
                f"Post targets controller {post.controller} but the machine runs {machine.controller}",
                "Controller dialects are not interchangeable",
                "Select a post for the correct controller family",
                post.code,
            )
        )
    if post.controller_version and machine.controller_version and post.controller_version != machine.controller_version:
        findings.append(
            Finding(
                "post_controller_version_mismatch",
                Severity.S2_ENGINEER,
                (
                    f"Post is certified against controller version {post.controller_version}; "
                    f"the machine reports {machine.controller_version}"
                ),
                "Behaviour differences between controller versions have not been proven for this pair",
                "Re-certify the post against the installed controller version",
                post.code,
            )
        )
    if not post.test_program_hashes:
        findings.append(
            Finding(
                "post_no_test_evidence",
                Severity.S2_ENGINEER,
                f"Post {post.code} has no recorded certification test programs",
                "There is no evidence behind the certification claim",
                "Attach the approved test programs used to certify this pair",
                post.code,
            )
        )

    evaluation = GateEvaluation(Gate.POST_VALIDATION, target_kind="post", target_id=post.id)
    evaluation.findings = findings  # post certification findings are never waivable
    return evaluation


# ---------------------------------------------------------------- release gate
def evaluate_release(
    db: Session,
    *,
    project: PartProject,
    plan: ManufacturingPlan,
    simulation: SimulationRun,
    nc_programs: list[NCProgram],
    approver_id: str | None = None,
) -> GateEvaluation:
    findings: list[Finding] = []

    for evaluation in (evaluate_plan(db, plan), evaluate_simulation(db, simulation)):
        for finding in evaluation.findings:
            if finding.blocking:
                findings.append(
                    Finding(
                        f"upstream_{finding.code}",
                        finding.severity,
                        f"{evaluation.gate.value} is not satisfied: {finding.message}",
                        finding.consequence,
                        finding.recommendation,
                        finding.object_ref,
                        finding.detail,
                    )
                )

    geometry = db.get(GeometryVersion, plan.geometry_version_id)
    if geometry is None or geometry.status != LifecycleStatus.APPROVED:
        findings.append(
            Finding(
                "geometry_not_approved",
                Severity.S1_STOP,
                "The geometry version behind this plan is not approved",
                "The release would rest on geometry nobody signed for",
                "Approve the geometry version, or regenerate the plan from an approved one",
                plan.geometry_version_id,
            )
        )

    if not nc_programs:
        findings.append(
            Finding(
                "no_nc_program",
                Severity.S1_STOP,
                "No NC program has been generated for this plan",
                "There is nothing to release",
                "Run postprocessing through a certified post",
                plan.id,
            )
        )

    for program in nc_programs:
        if program.stale:
            findings.append(
                Finding(
                    "nc_program_stale",
                    Severity.S1_STOP,
                    f"NC program {program.program_number} is stale",
                    "The program no longer matches the verified inputs",
                    "Regenerate the program and re-validate",
                    program.id,
                )
            )
        if not program.validation_passed:
            findings.append(
                Finding(
                    "nc_validation_failed",
                    Severity.S1_STOP,
                    f"NC program {program.program_number} did not pass validation",
                    "The program contains a condition the controller or the machine would reject",
                    "Resolve the validation findings and regenerate",
                    program.id,
                    {"max_severity": program.max_severity},
                )
            )
        if program.simulation_run_id != simulation.id:
            findings.append(
                Finding(
                    "nc_simulation_mismatch",
                    Severity.S1_STOP,
                    f"NC program {program.program_number} was produced from a different simulation run",
                    "The released program would not be the one that was verified",
                    "Regenerate the program from the current passing simulation",
                    program.id,
                )
            )
        post = db.get(PostProcessorVersion, program.post_version_id)
        machine = db.get(MachineVersion, program.machine_version_id)
        if post and machine:
            for finding in evaluate_post(db, post, machine).findings:
                if finding.blocking:
                    findings.append(finding)

    if approver_id:
        tenant = db.get(Tenant, project.tenant_id)
        if tenant and not tenant.allow_self_approval:
            prepared_by = {
                a.actor_id
                for a in db.execute(
                    select(Approval).where(
                        Approval.project_id == project.id,
                        Approval.gate.in_([Gate.GEOMETRY_APPROVAL.value, Gate.PLAN_APPROVAL.value]),
                    )
                ).scalars()
            }
            if approver_id in prepared_by:
                findings.append(
                    Finding(
                        "self_approval_blocked",
                        Severity.S1_STOP,
                        "The approver also signed an upstream gate on this project",
                        "Four-eyes separation would be lost on a production release",
                        "Have a second qualified approver sign the release",
                        approver_id,
                    )
                )

    evaluation = GateEvaluation(Gate.NC_RELEASE, target_kind="plan", target_id=plan.id)
    evaluation.findings = findings  # release findings are never waivable in the UI
    return evaluation
