"""MVP acceptance scenarios (PRD 13.2).

Each test is one row of the acceptance table, phrased as the given, the when
and the acceptance the PRD states.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.enums import (
    AuthorityRank,
    Criticality,
    Disposition,
    FeatureType,
    LifecycleStatus,
    ReleaseStatus,
    Severity,
    VerificationStatus,
)
from app.core.errors import GateBlocked, PermissionDenied, ValidationFailed
from app.core.security import totp_now
from app.engines.partmodel import Feature, PartModel
from app.services import (
    evidence,
    learning,
    planning,
    policy,
    postprocess,
    verification,
)
from app.services import (
    geometry as geometry_service,
)
from app.services import (
    release as release_service,
)

SAMPLES = Path(__file__).resolve().parents[2] / "samples"


# ---------------------------------------------------- calibrated reconstruction
def test_photo_only_geometry_cannot_be_approved_without_scale(db, make_project, users):
    """Photographs alone establish shape, not size; the gate must refuse them."""
    project = make_project()
    model = PartModel(outline={"shape": "rect", "center": [0, 0], "size": [100, 60]}, z_top=0.0, z_bottom=-20.0)
    geometry = geometry_service.create_version(
        db,
        project=project,
        part_model=model,
        scale_established=False,
        scale_source=None,
        scale_uncertainty_mm=None,
        quality_report={"blocking": "scale_not_established"},
        source_artifact_hashes=["1" * 64],
        actor_id=users["engineer"].id,
    )
    db.commit()

    evaluation = policy.evaluate_geometry(db, geometry)
    assert evaluation.passed is False
    codes = {f.code for f in evaluation.findings}
    assert "scale_not_established" in codes
    stop = next(f for f in evaluation.findings if f.code == "scale_not_established")
    assert stop.severity is Severity.S1_STOP
    assert stop.severity.waivable is False

    with pytest.raises(GateBlocked):
        geometry_service.approve(db, geometry, actor_id=users["engineer"].id)


def test_scaled_reconstruction_records_its_scale_source(db, make_project, users):
    project = make_project()
    artifacts = []
    for path in (SAMPLES / "cad" / "BRK-1042.step", SAMPLES / "drawings" / "BRK-1042.pdf"):
        with path.open("rb") as handle:
            artifacts.append(
                evidence.ingest_artifact(
                    db, project=project, filename=path.name, stream=handle, uploader_id=users["engineer"].id
                )
            )
    db.commit()

    geometry = geometry_service.reconstruct(db, project=project, artifacts=artifacts, actor_id=users["engineer"].id)
    db.commit()

    assert geometry.scale_established is True
    assert "STEP" in (geometry.scale_source or "")
    assert geometry.quality_report["scale_uncertainty_mm"] is not None
    assert geometry.quality_report["coverage"]["completeness"] == 0.0  # no guided capture yet
    assert len(geometry.part_model["features"]) == 4  # four Z-axis cylinders found


# ------------------------------------------------------------- evidence conflict
def test_drawing_outranks_a_photo_estimate_and_the_conflict_stays_visible(db, make_project, users):
    """PRD 13.2: drawing says 12.00, image estimate says 11.6."""
    project = make_project()
    drawing = evidence.record_observation(
        db,
        project=project,
        attribute="diameter",
        feature_key="H9",
        value=12.00,
        authority=AuthorityRank.DRAWING,
        method="drawing callout",
        confidence=0.9,
        uncertainty=0.02,
        status=VerificationStatus.VERIFICATION_REQUIRED,
        criticality=Criticality.FUNCTION,
    )
    photo = evidence.record_observation(
        db,
        project=project,
        attribute="diameter",
        feature_key="H9",
        value=11.60,
        authority=AuthorityRank.CALIBRATED_PHOTO,
        method="cylinder fit",
        confidence=0.7,
        uncertainty=0.18,
        status=VerificationStatus.INFERRED,
        criticality=Criticality.FUNCTION,
    )
    db.commit()

    conflict = evidence.detect_for(db, project, attribute="diameter", feature_key="H9")
    assert conflict is not None
    assert conflict.resolved is False
    assert conflict.authoritative_observation_id == drawing.id  # drawing outranks the photo
    assert conflict.delta == pytest.approx(0.40, abs=1e-6)
    assert Severity(conflict.severity) is Severity.S2_ENGINEER
    assert "Drawing" in conflict.summary

    evidence.resolve_conflict(
        db, conflict, chosen_observation_id=drawing.id, actor_id=users["engineer"].id, reason="Drawing is controlled"
    )
    db.commit()
    db.refresh(photo)
    assert conflict.resolved is True
    assert photo.superseded_by_id == drawing.id
    assert photo.disposition == Disposition.REJECT


def test_agreeing_sources_within_uncertainty_are_not_a_conflict(db, make_project):
    project = make_project()
    evidence.record_observation(
        db, project=project, attribute="length", value=100.00, uncertainty=0.05,
        authority=AuthorityRank.DRAWING, status=VerificationStatus.VERIFICATION_REQUIRED,
    )
    evidence.record_observation(
        db, project=project, attribute="length", value=100.02, uncertainty=0.03,
        authority=AuthorityRank.MEASUREMENT, status=VerificationStatus.MEASURED,
    )
    db.commit()
    assert evidence.detect_for(db, project, attribute="length", feature_key=None) is None


# ---------------------------------------------------------------- unknown thread
def test_unresolved_thread_blocks_geometry_and_prevents_tapping(db, make_project, bracket_model, users):
    """PRD 13.2: hole images do not establish pitch, so tapping cannot be selected."""
    project = make_project()
    model = bracket_model
    model.features.append(
        Feature(
            key="T1",
            feature_type=FeatureType.THREAD_CANDIDATE,
            label="T1 thread candidate",
            params={"center": [0.0, 25.0], "diameter": 6.8, "depth": 15.0, "top_z": 0.0},
            criticality=Criticality.FUNCTION,
            status=VerificationStatus.VERIFICATION_REQUIRED,
            confidence=0.6,
            thread_spec=None,
        )
    )
    geometry = geometry_service.create_version(
        db, project=project, part_model=model, scale_established=True, scale_source="CAD",
        scale_uncertainty_mm=0.01, quality_report={}, source_artifact_hashes=[], actor_id=users["engineer"].id,
    )
    db.commit()

    evaluation = policy.evaluate_geometry(db, geometry)
    thread_findings = [f for f in evaluation.findings if f.code == "thread_unresolved"]
    assert thread_findings, "an unresolved thread must be reported"
    assert thread_findings[0].severity is Severity.S2_ENGINEER
    assert evaluation.passed is False

    # The feature is classified as needing confirmation, not as plannable.
    from sqlalchemy import select

    from app.models.geometry import ManufacturingFeature

    feature = db.execute(
        select(ManufacturingFeature).where(
            ManufacturingFeature.geometry_version_id == geometry.id, ManufacturingFeature.stable_key == "T1"
        )
    ).scalar_one()
    assert feature.support == "Partially supported"
    assert "pitch" in (feature.support_reason or "").lower()


# ------------------------------------------------------------ machine feasibility
def test_machine_without_travel_is_rejected_with_the_binding_constraint(db, approved_geometry, factory, users):
    """PRD 13.2: the machine is rejected with the exact binding constraint."""
    project, geometry = approved_geometry
    planning.generate_plans(
        db,
        project=project,
        geometry=geometry,
        material=factory["materials"]["AL6082-T6"],
        fixture=factory["fixtures"]["VISE-160"],
        machines=list(factory["machines"].values()),
        actor_id=users["manufacturing"].id,
    )
    db.commit()

    from sqlalchemy import select

    from app.models.planning import MachineFeasibility

    rows = {
        db.get(type(factory["machines"]["VMC-01"]), r.machine_version_id).code: r
        for r in db.execute(select(MachineFeasibility).where(MachineFeasibility.project_id == project.id)).scalars()
    }
    assert rows["VMC-01"].feasible is True
    assert rows["VMC-03"].feasible is False
    assert rows["VMC-03"].cause_code == "travel_x_exceeded"
    assert "X travel is 120 mm" in rows["VMC-03"].binding_constraint
    assert rows["VMC-03"].detail["required_mm"] > rows["VMC-03"].detail["available_mm"]


def test_no_feasible_machine_reports_every_cause_code(db, approved_geometry, factory, users):
    project, geometry = approved_geometry
    with pytest.raises(GateBlocked) as excinfo:
        planning.generate_plans(
            db,
            project=project,
            geometry=geometry,
            material=factory["materials"]["AL6082-T6"],
            fixture=factory["fixtures"]["VISE-160"],
            machines=[factory["machines"]["VMC-03"]],
            actor_id=users["manufacturing"].id,
        )
    detail = excinfo.value.detail
    assert excinfo.value.code == "no_feasible_machine"
    assert detail["machines"][0]["cause_code"] == "travel_x_exceeded"


# ------------------------------------------------------- stock aware rest machining
def test_rest_machining_uses_remaining_stock_and_does_not_air_cut():
    """PRD 13.2: the second operation must not re-cut the original volume."""
    import numpy as np

    from app.engines import toolpath
    from app.engines.cutting import CuttingParameters
    from app.engines.partmodel import Grid

    grid = Grid(x0=-50, y0=-30, nx=100, ny=60, pitch=1.0)
    target = np.full((100, 60), 0.0)
    target[20:80, 10:50] = -8.0  # a pocket floor

    parameters = CuttingParameters(
        rpm=10000, feed_mm_min=2000, fz_mm=0.05, vc_m_min=300, ap_mm=2.0, ae_mm=3.0,
        mrr_mm3_min=1000, power_kw=1.0, coolant="flood", plunge_feed_mm_min=600,
    )
    ctx = toolpath.PathContext(tool_diameter=6.0, clearance_z=25.0, stock_top_z=0.0)

    already_machined = target.copy()
    nothing_left = toolpath.rest_machining(
        stock_z=already_machined, target_z=target, grid=grid, ctx=ctx, params=parameters
    )
    assert nothing_left.moves == []
    assert any("suppressed" in w for w in nothing_left.warnings)

    partly_machined = target.copy()
    partly_machined[30:50, 20:40] = -6.0  # 2 mm of stock left in one region
    rest = toolpath.rest_machining(stock_z=partly_machined, target_z=target, grid=grid, ctx=ctx, params=parameters)
    assert rest.moves, "rest machining must cut where material remains"

    xs, ys = grid.centers()
    cutting_moves = [m for m in rest.moves if m["t"] in ("linear", "plunge")]
    assert cutting_moves
    # Every cut must fall inside the region that still holds material.
    for move in cutting_moves:
        assert xs[30] - 4 <= move["x"] <= xs[49] + 4
        assert ys[20] - 4 <= move["y"] <= ys[39] + 4


# -------------------------------------------------------------------- collision
def test_a_clamp_in_the_path_produces_an_unwaivable_stop(db, approved_geometry, factory, users):
    """PRD 13.2: a rapid into a clamp emits an S1 event and blocks postprocessing."""
    project, geometry = approved_geometry
    fixture = factory["fixtures"]["VISE-160"]

    # A clamp deliberately placed over the pocket.
    from app.core.hashing import sha256_json
    from app.models.factory import FixtureVersion

    hostile = FixtureVersion(
        tenant_id=fixture.tenant_id,
        code="TEST-CLAMP-OVER-POCKET",
        revision=1,
        name="Golden collision case: clamp over the pocket",
        fixture_type="clamp",
        jaw_opening_mm=200.0,
        max_part_height_mm=120.0,
        clamp_height_mm=8.0,
        safe_clearance_mm=2.0,
        verified=True,
        solids=[{"name": "strap clamp", "type": "box", "min": [-12, -12, -9], "max": [12, 12, 40]}],
        status=LifecycleStatus.CURRENT.value,
    )
    hostile.content_hash = sha256_json({"code": hostile.code})
    db.add(hostile)
    db.commit()

    plans = planning.generate_plans(
        db, project=project, geometry=geometry, material=factory["materials"]["AL6082-T6"],
        fixture=hostile, machines=[factory["machines"]["VMC-01"]], actor_id=users["manufacturing"].id,
    )
    db.commit()
    plan = plans[0]
    planning.generate_toolpaths(db, plan=plan, actor_id=users["manufacturing"].id)
    db.commit()
    run = verification.run_simulation(db, plan=plan, voxel_pitch=1.0, actor_id=users["manufacturing"].id)
    db.commit()

    assert run.passed is False
    collisions = [e for e in run.events if e["code"] == "fixture_collision"]
    assert collisions, "the clamp must be detected"
    first = collisions[0]
    assert first["severity"] == Severity.S1_STOP.value
    assert first["time_s"] >= 0
    assert "strap clamp" in first["entities"]
    assert first["position"] is not None
    assert first["detail"]["penetration_mm"] > 0

    # An S1 cannot be dispositioned away.
    with pytest.raises(ValidationFailed, match="cannot be dispositioned"):
        verification.disposition_event(
            db, run, event_code="fixture_collision", decision="resolved",
            actor_id=users["manufacturing"].id, reason="tried to wave it through",
        )

    gate = policy.evaluate_simulation(db, run)
    assert gate.passed is False
    assert gate.blocks == "Postprocessing"
    assert all(f.blocking for f in gate.findings if f.severity is Severity.S1_STOP)


# --------------------------------------------------------------- stale artifact
def test_editing_geometry_makes_everything_downstream_stale(db, simulated_plan, users, bracket_model):
    """PRD 13.2: approved geometry changes after simulation, so nothing may release."""
    project, geometry, plan, run = simulated_plan
    assert run.passed is True
    assert plan.stale is False

    model = PartModel.from_dict(geometry.part_model)
    pocket = next(f for f in model.features if f.key == "P1")
    pocket.params["depth"] = 9.0  # a real engineering change

    geometry_service.edit_features(
        db, project=project, geometry=geometry, part_model=model,
        actor_id=users["engineer"].id, reason="Pocket depth corrected against the drawing",
    )
    db.commit()
    db.refresh(plan)
    db.refresh(run)

    assert plan.stale is True
    assert "superseded" in (plan.stale_reason or "")
    assert run.stale is True

    plan_gate = policy.evaluate_plan(db, plan)
    assert plan_gate.passed is False
    assert "plan_stale" in {f.code for f in plan_gate.findings}

    simulation_gate = policy.evaluate_simulation(db, run)
    assert simulation_gate.passed is False
    assert "simulation_stale" in {f.code for f in simulation_gate.findings}


# ------------------------------------------------------------------ post mismatch
def test_post_certified_for_another_machine_is_refused(db, simulated_plan, factory, users):
    """PRD 13.2: the FANUC post for VMC-01 must not run for VMC-02."""
    project, geometry, plan, run = simulated_plan
    other_post = factory["posts"]["FANUC-VMC02"]
    postprocess.certify_post(
        db, other_post, machine=factory["machines"]["VMC-02"], actor_id=users["admin"].id,
        note="Certified for the trunnion cell", test_program_hashes=["d" * 64],
    )
    db.commit()

    with pytest.raises(GateBlocked) as excinfo:
        postprocess.postprocess(db, plan=plan, simulation=run, post=other_post, actor_id=users["cam"].id)
    codes = {f["code"] for f in excinfo.value.detail["findings"]}
    assert "post_machine_mismatch" in codes
    db.rollback()


def test_uncertified_post_is_refused(db, simulated_plan, factory, users):
    project, geometry, plan, run = simulated_plan
    from app.models.factory import PostProcessorVersion

    draft = PostProcessorVersion(
        tenant_id=plan.tenant_id, code="FANUC-DRAFT", revision=1, name="Draft post",
        machine_code="VMC-01", controller="FANUC", controller_version="31i-B5", definition={},
    )
    db.add(draft)
    db.commit()
    with pytest.raises(GateBlocked) as excinfo:
        postprocess.postprocess(db, plan=plan, simulation=run, post=draft, actor_id=users["cam"].id)
    codes = {f["code"] for f in excinfo.value.detail["findings"]}
    assert "post_not_certified" in codes and "post_not_enabled" in codes
    db.rollback()


# ---------------------------------------------------------------- release integrity
def test_release_requires_role_second_factor_and_a_signed_checklist(db, simulated_plan, certified_post, users):
    project, geometry, plan, run = simulated_plan
    program = postprocess.postprocess(db, plan=plan, simulation=run, post=certified_post, actor_id=users["cam"].id)
    db.commit()
    assert program.validation_passed is True

    release = release_service.prepare(
        db, project=project, plan=plan, simulation=run, nc_programs=[program], actor_id=users["cam"].id
    )
    db.commit()
    assert release.gate_results[-1]["passed"] is True
    checklist = {item["key"]: True for item in release_service.release_checklist(db, release)}

    # Wrong role.
    with pytest.raises(PermissionDenied, match="release approver role"):
        release_service.approve(
            db, release=release, approver=users["cam"], totp_code="000000",
            statement="Not my authority to give", checklist=checklist,
        )
    db.rollback()

    # Right role, bad second factor.
    with pytest.raises(PermissionDenied, match="second factor"):
        release_service.approve(
            db, release=release, approver=users["approver"], totp_code="000000",
            statement="Reviewed and approved for controlled proof-out", checklist=checklist,
        )
    db.rollback()

    # Right role, valid factor, incomplete checklist.
    partial = dict(checklist)
    partial["workholding_confirmed"] = False
    with pytest.raises(ValidationFailed, match="checklist"):
        release_service.approve(
            db, release=release, approver=users["approver"], totp_code=totp_now(users["approver"].totp_secret),
            statement="Reviewed and approved for controlled proof-out", checklist=partial,
        )
    db.rollback()

    # Everything in order.
    release_service.approve(
        db, release=release, approver=users["approver"], totp_code=totp_now(users["approver"].totp_secret),
        statement="Reviewed simulation, tool list and workholding. Approved for controlled proof-out.",
        checklist=checklist,
    )
    db.commit()

    assert release.status == ReleaseStatus.RELEASED
    assert release.package_signature
    assert release.package_hash
    manifest = release.manifest["artifacts"]
    for key in ("geometry", "plan", "simulation", "toolpaths", "tool_list", "nc_programs", "machine"):
        assert key in manifest and manifest[key]["hash"]

    data, filename, controlled = release_service.package_bytes(db, release)
    assert controlled is True
    assert filename.endswith(".zip")

    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        assert "manifest.json" in names and "SIGNATURE.txt" in names
        assert any(n.startswith("nc/") for n in names)
        assert any(n.startswith("ir/") for n in names)


def test_editing_after_release_creates_a_new_revision_and_supersedes_the_old(db, simulated_plan, certified_post, users):
    project, geometry, plan, run = simulated_plan
    program = postprocess.postprocess(db, plan=plan, simulation=run, post=certified_post, actor_id=users["cam"].id)
    db.commit()
    first = release_service.prepare(
        db, project=project, plan=plan, simulation=run, nc_programs=[program], actor_id=users["cam"].id
    )
    db.commit()
    release_service.approve(
        db, release=first, approver=users["approver"], totp_code=totp_now(users["approver"].totp_secret),
        statement="First release approved for controlled proof-out", checklist={
            item["key"]: True for item in release_service.release_checklist(db, first)
        },
    )
    db.commit()
    assert first.status == ReleaseStatus.RELEASED

    second = release_service.prepare(
        db, project=project, plan=plan, simulation=run, nc_programs=[program], actor_id=users["cam"].id
    )
    db.commit()
    release_service.approve(
        db, release=second, approver=users["approver"], totp_code=totp_now(users["approver"].totp_secret),
        statement="Second release after a controlled change", checklist={
            item["key"]: True for item in release_service.release_checklist(db, second)
        },
    )
    db.commit()
    db.refresh(first)

    assert second.revision == first.revision + 1
    assert first.status == ReleaseStatus.SUPERSEDED
    assert first.superseded_by_id == second.id
    assert first.package_hash  # the superseded package is preserved, not deleted


# --------------------------------------------------------------- actual feedback
def test_actuals_link_to_the_exact_program_and_change_nothing_automatically(
    db, simulated_plan, certified_post, users
):
    project, geometry, plan, run = simulated_plan
    program = postprocess.postprocess(db, plan=plan, simulation=run, post=certified_post, actor_id=users["cam"].id)
    db.commit()
    release = release_service.prepare(
        db, project=project, plan=plan, simulation=run, nc_programs=[program], actor_id=users["cam"].id
    )
    db.commit()
    release_service.approve(
        db, release=release, approver=users["approver"], totp_code=totp_now(users["approver"].totp_secret),
        statement="Approved for controlled proof-out", checklist={
            item["key"]: True for item in release_service.release_checklist(db, release)
        },
    )
    db.commit()

    # An unrelated program id is refused rather than approximately matched.
    with pytest.raises(ValidationFailed, match="not part of this release"):
        learning.record_run(
            db, release=release, nc_program_id="0" * 32, payload={"actual_cycle_seconds": 100.0},
            actor_id=users["quality"].id,
        )
    db.rollback()

    for _ in range(3):
        learning.record_run(
            db, release=release, nc_program_id=program.id,
            payload={
                "actual_cycle_seconds": run.cycle_time_seconds * 1.25,
                "result": "Good",
                "nc_program_hash": program.program_hash,
            },
            actor_id=users["quality"].id,
        )
        db.commit()

    report = learning.variance_report(db, project=project)
    assert report["summary"]["sample_size"] == 3
    assert report["summary"]["median_variance_percent"] == pytest.approx(25.0, abs=0.5)
    assert report["summary"]["within_calibration_band"] is False

    material_before = db.get(type(plan).__mro__[0], plan.id).material_id
    proposals = learning.propose_rules(db, project=project, actor_id=users["manufacturing"].id)
    db.commit()
    assert proposals, "a variance outside the band should produce a proposal"
    proposal = proposals[0]
    assert proposal.status == "Proposed"
    assert proposal.validation_state == "shadow"
    assert proposal.proposed_value["correction_factor"] == pytest.approx(1.25, abs=0.02)
    # Nothing about the plan changed just because a proposal exists.
    assert db.get(type(plan).__mro__[0], plan.id).material_id == material_before

    learning.review_proposal(
        db, proposal, approve=True, actor_id=users["manufacturing"].id,
        note="Accepted after review of three comparable runs",
    )
    db.commit()
    assert proposal.status == "Approved"
    assert proposal.validation_state == "promoted"


# -------------------------------------------------------------- telemetry hygiene
def test_a_program_hash_mismatch_is_flagged_as_suspect(db, simulated_plan, certified_post, users):
    project, geometry, plan, run = simulated_plan
    program = postprocess.postprocess(db, plan=plan, simulation=run, post=certified_post, actor_id=users["cam"].id)
    db.commit()
    release = release_service.prepare(
        db, project=project, plan=plan, simulation=run, nc_programs=[program], actor_id=users["cam"].id
    )
    db.commit()
    release_service.approve(
        db, release=release, approver=users["approver"], totp_code=totp_now(users["approver"].totp_secret),
        statement="Approved for controlled proof-out", checklist={
            item["key"]: True for item in release_service.release_checklist(db, release)
        },
    )
    db.commit()

    machine_run = learning.record_run(
        db, release=release, nc_program_id=program.id,
        payload={"actual_cycle_seconds": 120.0, "nc_program_hash": "9" * 64},
        actor_id=users["quality"].id,
    )
    db.commit()
    assert machine_run.telemetry_suspect is True
    report = learning.variance_report(db, project=project)
    assert report["summary"]["sample_size"] == 0  # suspect telemetry is excluded from calibration
