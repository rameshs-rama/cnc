"""Geometry versions, reconstruction and feature recognition.

Reconstruction turns evidence into a *provisional* part model. It is labelled
provisional until an engineer approves it, and it refuses to produce scaled
manufacturing geometry without a traceable dimension (FR-REC-004).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import audit, events, staleness
from app.core.enums import (
    ArtifactKind,
    AuthorityRank,
    Criticality,
    FeatureType,
    Gate,
    LifecycleStatus,
    VerificationStatus,
)
from app.core.errors import GateBlocked, ValidationFailed
from app.core.hashing import sha256_json
from app.engines.partmodel import Feature, PartModel
from app.models.base import audit_entry
from app.models.geometry import GeometryVersion, ManufacturingFeature
from app.models.project import EvidenceObservation, PartProject, SourceArtifact
from app.parsers import step as step_parser
from app.services import policy

RECONSTRUCTION_VERSION = "1.0.0"


def stable_key(feature_type: str, params: dict[str, Any], access: tuple[float, float, float]) -> str:
    """Identity that survives unrelated geometry edits (FR-FTR-002).

    The key is a hash of the feature type, its access direction and its
    position-invariant parameters rounded to 0.01 mm. Two revisions of the same
    physical feature therefore keep one identity, and a moved feature honestly
    gets a new one.
    """
    signature: dict[str, Any] = {"type": feature_type, "access": [round(a, 4) for a in access]}
    for key in ("center", "start", "end", "size", "diameter", "width", "depth", "corner_radius", "top_z"):
        if key not in params:
            continue
        value = params[key]
        if isinstance(value, (list, tuple)):
            signature[key] = [round(float(v), 2) for v in value]
        else:
            signature[key] = round(float(value), 2)
    return sha256_json(signature)[:16]


def create_version(
    db: Session,
    *,
    project: PartProject,
    part_model: PartModel,
    scale_established: bool,
    scale_source: str | None,
    scale_uncertainty_mm: float | None,
    quality_report: dict[str, Any],
    source_artifact_hashes: list[str],
    parent_id: str | None = None,
    provisional: bool = True,
    actor_id: str | None = None,
) -> GeometryVersion:
    next_revision = (
        db.execute(
            select(func.coalesce(func.max(GeometryVersion.revision), 0)).where(
                GeometryVersion.project_id == project.id
            )
        ).scalar_one()
        + 1
    )
    payload = part_model.to_dict()
    geometry = GeometryVersion(
        tenant_id=project.tenant_id,
        project_id=project.id,
        revision=next_revision,
        parent_id=parent_id,
        part_model=payload,
        units=part_model.units,
        scale_established=scale_established,
        scale_source=scale_source,
        scale_uncertainty_mm=scale_uncertainty_mm,
        quality_report=quality_report,
        provisional=provisional,
        status=LifecycleStatus.CURRENT.value,
        content_hash=sha256_json(payload),
        source_artifact_hashes=source_artifact_hashes,
        audit=[audit_entry(actor_id or "system", "created", f"revision {next_revision}")],
    )
    db.add(geometry)
    db.flush()

    recognise_features(db, geometry, part_model, actor_id=actor_id)

    if parent_id:
        affected = staleness.invalidate(
            db,
            upstream_kind="geometry",
            upstream_id=parent_id,
            reason=f"Geometry revision {next_revision} superseded the version these artifacts were built from",
        )
        geometry.audit = [*geometry.audit, audit_entry(actor_id or "system", "invalidated_downstream", detail=affected)]

    events.publish(
        db,
        tenant_id=project.tenant_id,
        topic=events.Topic.GEOMETRY_VERSION_CREATED,
        project_id=project.id,
        actor_id=actor_id,
        payload={
            "geometry_version_id": geometry.id,
            "revision": next_revision,
            "content_hash": geometry.content_hash,
            "source_hashes": source_artifact_hashes,
            "provisional": provisional,
            "scale_established": scale_established,
        },
    )
    return geometry


def recognise_features(
    db: Session, geometry: GeometryVersion, part_model: PartModel, *, actor_id: str | None = None
) -> list[ManufacturingFeature]:
    """Persist the recognised features of a geometry version (FR-FTR-001/003)."""
    previous_keys: set[str] = set()
    if geometry.parent_id:
        previous_keys = {
            row
            for row in db.execute(
                select(ManufacturingFeature.stable_key).where(
                    ManufacturingFeature.geometry_version_id == geometry.parent_id
                )
            ).scalars()
        }

    created: list[ManufacturingFeature] = []
    for feature in part_model.features:
        key = feature.key or stable_key(feature.feature_type, feature.params, feature.access)
        record = ManufacturingFeature(
            tenant_id=geometry.tenant_id,
            geometry_version_id=geometry.id,
            project_id=geometry.project_id,
            stable_key=key,
            feature_type=feature.feature_type,
            label=feature.label or key,
            parameters=feature.params,
            access_direction=list(feature.access),
            support=feature.support,
            support_reason=_support_reason(feature),
            criticality=feature.criticality,
            status=feature.status,
            confidence=feature.confidence,
            tolerance=feature.tolerance,
            surface_finish_ra=feature.surface_finish_ra,
            thread_spec=feature.thread_spec,
            predecessor_key=key if key in previous_keys else None,
        )
        db.add(record)
        created.append(record)
    db.flush()
    return created


def _support_reason(feature: Feature) -> str | None:
    support = feature.support
    if support == "Supported":
        return None
    if feature.feature_type == FeatureType.THREAD_CANDIDATE:
        return "Thread designation and pitch must be confirmed before tapping can be selected"
    if feature.feature_type in (FeatureType.BOSS, FeatureType.ISLAND):
        return "Island avoidance is generated but wall finishing needs engineer review"
    if feature.feature_type == FeatureType.FILLET:
        return "Fillet finishing depends on the chosen finishing strategy"
    return "Feature type is outside the automatic planning scope in this release"


def reconstruct(
    db: Session,
    *,
    project: PartProject,
    artifacts: list[SourceArtifact],
    known_dimensions: dict[str, float] | None = None,
    parent: GeometryVersion | None = None,
    actor_id: str | None = None,
    progress: Any = None,
) -> GeometryVersion:
    """Build a provisional part model from the available evidence.

    The reconstruction prefers the highest-authority geometric source present:
    CAD first, then a 2D drawing, then measurements. Photographs alone establish
    shape but not scale, and the result is refused rather than guessed
    (FR-REC-004).
    """
    known_dimensions = known_dimensions or {}
    notes: list[str] = []
    quality: dict[str, Any] = {
        "engine_version": RECONSTRUCTION_VERSION,
        "sources": [{"id": a.id, "kind": a.kind, "authority": a.authority, "hash": a.content_hash} for a in artifacts],
    }

    step_artifacts = [a for a in artifacts if a.kind == ArtifactKind.STEP]
    stl_artifacts = [a for a in artifacts if a.kind == ArtifactKind.STL]
    dxf_artifacts = [a for a in artifacts if a.kind == ArtifactKind.DXF]
    images = [a for a in artifacts if a.kind in (ArtifactKind.IMAGE, ArtifactKind.VIDEO)]

    if progress:
        progress("collecting evidence", 15, f"{len(artifacts)} artifacts available")

    model: PartModel | None = None
    scale_source: str | None = None
    scale_uncertainty: float | None = None
    scale_established = False

    if step_artifacts:
        model, scale_source, scale_uncertainty = _model_from_step(step_artifacts[0], dxf_artifacts, notes)
        scale_established = True
    elif dxf_artifacts:
        model, scale_source, scale_uncertainty = _model_from_dxf(dxf_artifacts[0], known_dimensions, notes)
        scale_established = True
    elif stl_artifacts:
        model, scale_source, scale_uncertainty = _model_from_stl(stl_artifacts[0], notes)
        scale_established = True
    elif images:
        model, scale_established, scale_source, scale_uncertainty = _model_from_images(
            images, known_dimensions, notes, quality
        )
    else:
        raise ValidationFailed(
            "No artifact in this project carries geometry. Upload CAD, a drawing, a mesh or calibrated images.",
            code="no_geometry_evidence",
        )

    if progress:
        progress("fitting geometry", 60, f"{len(model.features)} features proposed")

    quality.update(
        {
            "feature_count": len(model.features),
            "scale_established": scale_established,
            "scale_source": scale_source,
            "scale_uncertainty_mm": scale_uncertainty,
            "notes": notes,
            "coverage": _coverage_report(images, model),
        }
    )
    model.notes = notes

    geometry = create_version(
        db,
        project=project,
        part_model=model,
        scale_established=scale_established,
        scale_source=scale_source,
        scale_uncertainty_mm=scale_uncertainty,
        quality_report=quality,
        source_artifact_hashes=[a.content_hash for a in artifacts],
        parent_id=parent.id if parent else None,
        provisional=not bool(step_artifacts),
        actor_id=actor_id,
    )

    _attach_observations(db, project, geometry, artifacts, model, actor_id)
    if progress:
        progress("stored geometry", 95, f"revision {geometry.revision} created")
    return geometry


def _model_from_step(
    artifact: SourceArtifact, dxf_artifacts: list[SourceArtifact], notes: list[str]
) -> tuple[PartModel, str, float]:
    extracted = artifact.extracted or {}
    lo = extracted.get("bbox_min_mm") or [0, 0, 0]
    hi = extracted.get("bbox_max_mm") or [0, 0, 0]
    notes.append(f"Envelope taken from STEP {extracted.get('schema', '')} point cloud ({extracted.get('point_count')} points)")

    model = PartModel(
        units="mm",
        outline={
            "shape": "rect",
            "center": [(lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0],
            "size": [hi[0] - lo[0], hi[1] - lo[1]],
        },
        z_top=hi[2],
        z_bottom=lo[2],
    )

    for index, candidate in enumerate(step_parser.hole_candidates(_as_extract(extracted), top_z=hi[2]), start=1):
        params = {
            "center": candidate["center"],
            "diameter": candidate["diameter"],
            "through": True,
            "top_z": hi[2],
        }
        model.features.append(
            Feature(
                key=stable_key(FeatureType.HOLE, params, (0.0, 0.0, 1.0)),
                feature_type=FeatureType.HOLE,
                params=params,
                label=f"H{index}",
                criticality=Criticality.FUNCTION,
                confidence=candidate["confidence"],
                status=VerificationStatus.VERIFICATION_REQUIRED,
            )
        )
    if extracted.get("entity_counts", {}).get("B_SPLINE_SURFACE"):
        notes.append("Free-form surfaces present; 3D finishing strategy must be reviewed by an engineer")
    if dxf_artifacts:
        notes.append("A drawing is also present and outranks this CAD envelope for dimensions under conflict")
    return model, f"STEP file {artifact.filename} declared in {extracted.get('units', 'mm')}", 0.01


def _as_extract(payload: dict[str, Any]) -> step_parser.StepExtract:
    extract = step_parser.StepExtract()
    extract.cylinders = payload.get("cylinders", [])
    extract.circles = payload.get("circles", [])
    return extract


def _model_from_dxf(
    artifact: SourceArtifact, known: dict[str, float], notes: list[str]
) -> tuple[PartModel, str, float]:
    extracted = artifact.extracted or {}
    outline = extracted.get("outline_candidate")
    if not outline:
        raise ValidationFailed("The drawing contains no closed profile or usable extent", code="no_outline")

    height = known.get("height") or known.get("thickness")
    if height is None:
        height = 20.0
        notes.append("Part height is not derivable from a 2D drawing; 20 mm assumed and flagged for verification")
    model = PartModel(units=extracted.get("units", "mm"), outline=outline, z_top=0.0, z_bottom=-float(height))

    for index, candidate in enumerate(extracted.get("hole_candidates", []), start=1):
        params = {"center": candidate["center"], "diameter": candidate["diameter"], "through": True, "top_z": 0.0}
        model.features.append(
            Feature(
                key=stable_key(FeatureType.HOLE, params, (0.0, 0.0, 1.0)),
                feature_type=FeatureType.HOLE,
                params=params,
                label=f"H{index}",
                criticality=Criticality.FUNCTION,
                confidence=candidate["confidence"],
                status=VerificationStatus.VERIFICATION_REQUIRED,
            )
        )
    notes.append(f"Outline from {outline.get('source')}")
    return model, f"Drawing {artifact.filename} in {extracted.get('units', 'mm')}", 0.05


def _model_from_stl(artifact: SourceArtifact, notes: list[str]) -> tuple[PartModel, str, float]:
    extracted = artifact.extracted or {}
    lo = extracted.get("bbox_min_mm") or [0, 0, 0]
    hi = extracted.get("bbox_max_mm") or [0, 0, 0]
    if not extracted.get("closed"):
        notes.append("Mesh is not closed; the envelope is usable but internal features cannot be trusted")
    notes.append("Mesh source: only the envelope is derived. Features must be added by an engineer.")
    model = PartModel(
        units="mm",
        outline={
            "shape": "rect",
            "center": [(lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0],
            "size": [hi[0] - lo[0], hi[1] - lo[1]],
        },
        z_top=hi[2],
        z_bottom=lo[2],
    )
    return model, f"Mesh {artifact.filename}", 0.1


def _model_from_images(
    images: list[SourceArtifact], known: dict[str, float], notes: list[str], quality: dict[str, Any]
) -> tuple[PartModel, bool, str | None, float | None]:
    """Photograph-derived geometry.

    Shape can be proposed from images; absolute size cannot. Without a
    traceable dimension or a detected calibration target the geometry is
    returned unscaled and the geometry gate will refuse it.
    """
    calibrated = [a for a in images if (a.capture_metrics or {}).get("calibration_target_detected")]
    length = known.get("length")
    width = known.get("width")
    height = known.get("height")

    scale_established = bool(length or calibrated)
    if not scale_established:
        notes.append(
            "No traceable dimension and no calibration target: geometry is unscaled and cannot be approved for planning"
        )
        quality["blocking"] = "scale_not_established"

    model = PartModel(
        units="mm",
        outline={"shape": "rect", "center": [0.0, 0.0], "size": [float(length or 100.0), float(width or 60.0)]},
        z_top=0.0,
        z_bottom=-float(height or 20.0),
    )
    notes.append(f"{len(images)} images contributed; {len(calibrated)} contain a calibration target")

    source = None
    uncertainty = None
    if length:
        source = "Engineer-supplied known dimension"
        uncertainty = 0.15
    elif calibrated:
        source = f"Calibration target in {calibrated[0].filename}"
        uncertainty = 0.25
    return model, scale_established, source, uncertainty


def _coverage_report(images: list[SourceArtifact], model: PartModel) -> dict[str, Any]:
    regions = {"top", "bottom", "front", "back", "left", "right"}
    covered = {
        (a.capture_metrics or {}).get("region")
        for a in images
        if (a.capture_metrics or {}).get("region")
    }
    missing = sorted(regions - covered)
    return {
        "required_regions": sorted(regions),
        "covered_regions": sorted(r for r in covered if r),
        "missing_regions": missing,
        "completeness": round(len(regions - set(missing)) / len(regions), 3) if images else 0.0,
        "note": "Unseen regions may hide features; nothing is inferred for them" if missing else "All regions covered",
    }


def _attach_observations(
    db: Session,
    project: PartProject,
    geometry: GeometryVersion,
    artifacts: list[SourceArtifact],
    model: PartModel,
    actor_id: str | None,
) -> None:
    """Bind the geometry's own dimensions to the evidence that produced them."""
    from app.services import evidence as evidence_service

    primary = artifacts[0] if artifacts else None
    authority = AuthorityRank(primary.authority) if primary else AuthorityRank.AI_INFERENCE
    lo, hi = model.bbox()
    for attribute, value in (("length", hi[0] - lo[0]), ("width", hi[1] - lo[1]), ("height", model.part_height)):
        evidence_service.record_observation(
            db,
            project=project,
            attribute=attribute,
            value=round(float(value), 4),
            geometry_version_id=geometry.id,
            source_artifact_id=primary.id if primary else None,
            authority=authority,
            method=f"reconstruction {RECONSTRUCTION_VERSION}",
            confidence=0.9 if geometry.scale_established else 0.3,
            uncertainty=geometry.scale_uncertainty_mm,
            status=VerificationStatus.VERIFICATION_REQUIRED
            if geometry.scale_established
            else VerificationStatus.UNKNOWN,
            criticality=Criticality.FUNCTION,
            actor_id=actor_id,
            detect_conflicts=False,
        )
    for feature in model.features:
        if "diameter" in feature.params:
            evidence_service.record_observation(
                db,
                project=project,
                attribute="diameter",
                feature_key=feature.key,
                value=float(feature.params["diameter"]),
                geometry_version_id=geometry.id,
                source_artifact_id=primary.id if primary else None,
                authority=authority,
                method=f"reconstruction {RECONSTRUCTION_VERSION}",
                confidence=feature.confidence,
                uncertainty=geometry.scale_uncertainty_mm,
                status=VerificationStatus(feature.status),
                criticality=Criticality(feature.criticality),
                actor_id=actor_id,
                detect_conflicts=False,
            )

    for attribute in ("length", "width", "height", "diameter"):
        evidence_service.detect_for(db, project, attribute=attribute, feature_key=None)


def edit_features(
    db: Session,
    *,
    project: PartProject,
    geometry: GeometryVersion,
    part_model: PartModel,
    actor_id: str,
    reason: str,
) -> GeometryVersion:
    """Any geometry edit creates revision N+1; the previous stays intact (PRD 8.2)."""
    if geometry.status == LifecycleStatus.APPROVED:
        db.add(geometry)
    return create_version(
        db,
        project=project,
        part_model=part_model,
        scale_established=geometry.scale_established,
        scale_source=geometry.scale_source,
        scale_uncertainty_mm=geometry.scale_uncertainty_mm,
        quality_report={**(geometry.quality_report or {}), "edited_from": geometry.id, "edit_reason": reason},
        source_artifact_hashes=list(geometry.source_artifact_hashes or []),
        parent_id=geometry.id,
        provisional=geometry.provisional,
        actor_id=actor_id,
    )


def approve(db: Session, geometry: GeometryVersion, *, actor_id: str, note: str | None = None) -> GeometryVersion:
    """Approve geometry for planning, or refuse with the exact reasons (FR-ENG-004)."""
    evaluation = policy.evaluate_geometry(db, geometry)
    if not evaluation.passed:
        raise GateBlocked(
            f"{Gate.GEOMETRY_APPROVAL.value} did not pass; {evaluation.blocks} remains blocked",
            detail=evaluation.to_dict(),
        )

    geometry.status = LifecycleStatus.APPROVED.value
    geometry.approved_by_id = actor_id
    geometry.approval_note = note
    geometry.version += 1
    geometry.audit = [*(geometry.audit or []), audit_entry(actor_id, "approved", note)]
    db.add(geometry)
    audit.record(
        db,
        tenant_id=geometry.tenant_id,
        object_kind="geometry",
        object_id=geometry.id,
        action="approve",
        actor_id=actor_id,
        after={"status": geometry.status, "gate": evaluation.to_dict()},
        reason=note,
    )
    return geometry


def confidence_map(db: Session, geometry: GeometryVersion) -> dict[str, Any]:
    """Payload for the engineering review 3D confidence view (FR-ENG-001)."""
    features = db.execute(
        select(ManufacturingFeature).where(ManufacturingFeature.geometry_version_id == geometry.id)
    ).scalars().all()
    observations = db.execute(
        select(EvidenceObservation).where(EvidenceObservation.geometry_version_id == geometry.id)
    ).scalars().all()

    by_feature: dict[str, list[dict[str, Any]]] = {}
    for observation in observations:
        by_feature.setdefault(observation.feature_key or "__part__", []).append(
            {
                "id": observation.id,
                "attribute": observation.attribute,
                "value": observation.value,
                "unit": observation.unit,
                "authority": observation.authority,
                "authority_rank": AuthorityRank(observation.authority).rank,
                "method": observation.method,
                "confidence": observation.confidence,
                "uncertainty": observation.uncertainty,
                "residual": observation.residual,
                "status": observation.status,
                "criticality": observation.criticality,
                "disposition": observation.disposition,
                "source_artifact_id": observation.source_artifact_id,
            }
        )

    buckets = {status.value: 0 for status in VerificationStatus}
    for feature in features:
        buckets[feature.status] = buckets.get(feature.status, 0) + 1

    return {
        "geometry_version_id": geometry.id,
        "revision": geometry.revision,
        "status": geometry.status,
        "provisional": geometry.provisional,
        "scale": {
            "established": geometry.scale_established,
            "source": geometry.scale_source,
            "uncertainty_mm": geometry.scale_uncertainty_mm,
        },
        "quality_report": geometry.quality_report,
        "part_model": geometry.part_model,
        "status_counts": buckets,
        "features": [
            {
                "stable_key": f.stable_key,
                "label": f.label,
                "type": f.feature_type,
                "parameters": f.parameters,
                "access": f.access_direction,
                "support": f.support,
                "support_reason": f.support_reason,
                "criticality": f.criticality,
                "status": f.status,
                "confidence": f.confidence,
                "tolerance": f.tolerance,
                "thread_spec": f.thread_spec,
                "surface_finish_ra": f.surface_finish_ra,
                "observations": by_feature.get(f.stable_key, []),
                "carried_from_previous_revision": bool(f.predecessor_key),
            }
            for f in features
        ],
        "part_observations": by_feature.get("__part__", []),
        "gate": policy.evaluate_geometry(db, geometry).to_dict(),
    }
