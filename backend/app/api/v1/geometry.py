"""Reconstruction jobs, geometry versions and engineering review."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, expect_version, require, scoped, tenant_project
from app.core.enums import JobKind
from app.core.errors import ValidationFailed
from app.core.rbac import Permission
from app.engines.partmodel import Feature, PartModel
from app.jobs import queue
from app.models.geometry import GeometryVersion, ManufacturingFeature
from app.schemas.api import ApprovalRequest, GeometryJobRequest, GeometryOut, JobOut
from app.services import geometry as geometry_service

router = APIRouter(tags=["geometry"])


@router.post("/geometry-jobs", response_model=JobOut, status_code=202, summary="Request a reconstruction")
def create_geometry_job(
    payload: GeometryJobRequest,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.GEOMETRY_EDIT))],
) -> Any:
    """Queue reconstruction against immutable artifact ids.

    Engineering calculations are asynchronous; the job exposes stage, percent
    and diagnostics while it runs (PRD 9.1).
    """
    project = tenant_project(db, user, payload.project_id)
    job = queue.enqueue(
        db,
        tenant_id=user.tenant_id,
        kind=JobKind.RECONSTRUCTION.value,
        project_id=project.id,
        payload={
            "artifact_ids": payload.artifact_ids,
            "known_dimensions": payload.known_dimensions,
            "parent_id": payload.parent_geometry_id,
        },
        requested_by_id=user.id,
    )
    db.commit()
    db.refresh(job)
    return job


@router.get("/projects/{project_id}/geometry", response_model=list[GeometryOut], summary="List geometry revisions")
def list_geometry(project_id: str, db: DbSession, user: CurrentUser) -> list[GeometryVersion]:
    tenant_project(db, user, project_id)
    return list(
        db.execute(
            select(GeometryVersion)
            .where(GeometryVersion.project_id == project_id)
            .order_by(GeometryVersion.revision.desc())
        ).scalars()
    )


@router.get("/geometry/{geometry_id}", response_model=GeometryOut, summary="Read one geometry revision")
def get_geometry(geometry_id: str, db: DbSession, user: CurrentUser) -> GeometryVersion:
    return scoped(db, user, GeometryVersion, geometry_id, "Geometry version")


@router.get("/geometry/{geometry_id}/confidence-map", summary="Confidence map for engineering review")
def confidence_map(geometry_id: str, db: DbSession, user: CurrentUser) -> dict[str, Any]:
    """Every value with its source, method, confidence, residual and verifier."""
    geometry = scoped(db, user, GeometryVersion, geometry_id, "Geometry version")
    return geometry_service.confidence_map(db, geometry)


@router.get("/geometry/{geometry_id}/surface", summary="Finished-part surface for the 3D viewer")
def geometry_surface(
    geometry_id: str, db: DbSession, user: CurrentUser, pitch: float = 1.0, downsample: int = 2
) -> dict[str, Any]:
    """The finished surface as a height field, sampled on the engine's own grid.

    The viewer draws exactly what the verification engine compares against,
    rather than a separate cosmetic model that could drift from it.
    """
    from app.engines.partmodel import PartModel, grid_for, target_heightfield

    geometry = scoped(db, user, GeometryVersion, geometry_id, "Geometry version")
    model = PartModel.from_dict(geometry.part_model)
    grid = grid_for(model, max(0.25, min(pitch, 5.0)))
    field = target_heightfield(model, grid)
    step = max(1, int(downsample))
    sampled = field[::step, ::step]

    return {
        "geometry_version_id": geometry.id,
        "revision": geometry.revision,
        "units": geometry.units,
        "grid": {**grid.to_dict(), "downsample": step},
        "bounds": {"min": model.bbox()[0], "max": model.bbox()[1]},
        "z_top": model.z_top,
        "z_bottom": model.z_bottom,
        "height": [[round(float(v), 3) for v in row] for row in sampled],
        "outline": [[round(p[0], 3), round(p[1], 3)] for p in model.outline_polygon()],
        "stock": model.stock_block(),
    }


@router.get("/geometry/{geometry_id}/features", summary="Recognised features")
def list_features(geometry_id: str, db: DbSession, user: CurrentUser) -> list[dict[str, Any]]:
    geometry = scoped(db, user, GeometryVersion, geometry_id, "Geometry version")
    rows = db.execute(
        select(ManufacturingFeature).where(ManufacturingFeature.geometry_version_id == geometry.id)
    ).scalars()
    return [
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
        }
        for f in rows
    ]


@router.post("/geometry/{geometry_id}/features", response_model=GeometryOut, summary="Edit features into a new revision")
def edit_features(
    geometry_id: str,
    payload: dict[str, Any],
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.GEOMETRY_EDIT))],
) -> GeometryVersion:
    """Apply feature edits.

    The edit never overwrites: it produces revision N+1 and marks everything
    built from N as stale (PRD 8.2).
    """
    geometry = scoped(db, user, GeometryVersion, geometry_id, "Geometry version")
    reason = str(payload.get("reason", "")).strip()
    if len(reason) < 5:
        raise ValidationFailed("A geometry edit requires a reason of at least 5 characters", code="reason_required")

    model = PartModel.from_dict(geometry.part_model)
    for update in payload.get("features", []):
        key = update.get("key")
        existing = model.feature(key) if key else None
        if existing is None:
            model.features.append(Feature.from_dict(update))
            continue
        for field in ("params", "label", "criticality", "tolerance", "thread_spec", "surface_finish_ra", "status", "confidence"):
            if field in update:
                setattr(existing, field if field != "params" else "params", update[field])
    for key in payload.get("remove_feature_keys", []):
        model.features = [f for f in model.features if f.key != key]
    if "outline" in payload:
        model.outline = payload["outline"]
    for field in ("z_top", "z_bottom"):
        if field in payload:
            setattr(model, field, float(payload[field]))

    project = tenant_project(db, user, geometry.project_id)
    new_version = geometry_service.edit_features(
        db, project=project, geometry=geometry, part_model=model, actor_id=user.id, reason=reason
    )
    db.commit()
    db.refresh(new_version)
    return new_version


@router.post("/geometry/{geometry_id}:approve", response_model=GeometryOut, summary="Approve geometry for planning")
def approve_geometry(
    geometry_id: str,
    payload: ApprovalRequest,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.GEOMETRY_APPROVE))],
) -> GeometryVersion:
    """Runs the confidence policy. A critical unknown blocks the approval."""
    geometry = scoped(db, user, GeometryVersion, geometry_id, "Geometry version")
    expect_version(geometry, payload.expected_version)
    geometry_service.approve(db, geometry, actor_id=user.id, note=payload.note)
    db.commit()
    db.refresh(geometry)
    return geometry


@router.get("/geometry/{geometry_id}/gate", summary="Evaluate the geometry approval gate without signing")
def geometry_gate(geometry_id: str, db: DbSession, user: CurrentUser) -> dict[str, Any]:
    from app.services import policy

    geometry = scoped(db, user, GeometryVersion, geometry_id, "Geometry version")
    return policy.evaluate_geometry(db, geometry).to_dict()
