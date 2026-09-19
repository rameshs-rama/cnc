"""Projects, artifacts, capture sessions and evidence."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession, expect_version, require, scoped, tenant_project
from app.config import get_settings
from app.core import audit
from app.core.enums import (
    ArtifactKind,
    AuthorityRank,
    Criticality,
    Disposition,
    JobKind,
    ProjectState,
    VerificationStatus,
)
from app.core.errors import ValidationFailed
from app.core.rbac import Permission
from app.core.storage import get_store
from app.jobs import queue
from app.models.base import audit_entry
from app.models.project import (
    CaptureSession,
    EvidenceConflict,
    EvidenceObservation,
    PartProject,
    SourceArtifact,
    Waiver,
)
from app.schemas.api import (
    ArtifactOut,
    AuthorityOverride,
    CaptureSessionCreate,
    CaptureSessionOut,
    ConflictResolution,
    DispositionRequest,
    ObservationCreate,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    TransitionRequest,
    UploadInitiateRequest,
    UploadInitiateResponse,
    WaiverRequest,
)
from app.schemas.common import ActionResult, Page
from app.services import evidence as evidence_service
from app.services import workflow

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=ProjectOut, status_code=201, summary="Create a part project")
def create_project(
    payload: ProjectCreate,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.PROJECT_CREATE))],
) -> PartProject:
    project = PartProject(
        tenant_id=user.tenant_id,
        part_number=payload.part_number.strip(),
        revision=payload.revision.strip() or "A",
        name=payload.name.strip(),
        quantity=payload.quantity,
        unit_system=payload.unit_system,
        intended_use=payload.intended_use,
        target_material_code=payload.target_material_code,
        due_date=payload.due_date,
        provenance_declaration=payload.provenance_declaration,
        owner_id=user.id,
        state=ProjectState.DRAFT.value,
        audit=[audit_entry(user.id, "created")],
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.get("", response_model=Page[ProjectOut], summary="List projects in the tenant")
def list_projects(
    db: DbSession,
    user: CurrentUser,
    state: str | None = None,
    search: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[ProjectOut]:
    query = select(PartProject).where(PartProject.tenant_id == user.tenant_id)
    if state:
        query = query.where(PartProject.state == state)
    if search:
        pattern = f"%{search.lower()}%"
        query = query.where(
            func.lower(PartProject.part_number).like(pattern) | func.lower(PartProject.name).like(pattern)
        )
    total = db.execute(select(func.count()).select_from(query.subquery())).scalar_one()
    rows = db.execute(query.order_by(PartProject.updated_at.desc()).limit(limit).offset(offset)).scalars().all()
    return Page(items=[ProjectOut.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/{project_id}", response_model=ProjectOut, summary="Read one project")
def get_project(project_id: str, db: DbSession, user: CurrentUser) -> PartProject:
    return tenant_project(db, user, project_id)


@router.patch("/{project_id}", response_model=ProjectOut, summary="Update project metadata")
def update_project(
    project_id: str,
    payload: ProjectUpdate,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.PROJECT_UPDATE))],
) -> PartProject:
    project = tenant_project(db, user, project_id)
    expect_version(project, payload.expected_version)
    for field in ("name", "quantity", "intended_use", "target_material_code", "due_date", "provenance_declaration"):
        value = getattr(payload, field)
        if value is not None:
            setattr(project, field, value)
    project.version += 1
    project.audit = [*(project.audit or []), audit_entry(user.id, "updated")]
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.post("/{project_id}/transitions", response_model=ProjectOut, summary="Move the project along its state machine")
def transition(
    project_id: str,
    payload: TransitionRequest,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.PROJECT_UPDATE))],
) -> PartProject:
    project = tenant_project(db, user, project_id)
    workflow.transition(db, project, ProjectState(payload.target_state), actor_id=user.id, reason=payload.reason)
    db.commit()
    db.refresh(project)
    return project


# ------------------------------------------------------------------ artifacts
@router.post(
    "/{project_id}/artifacts:initiate",
    response_model=UploadInitiateResponse,
    summary="Start a resumable upload session",
)
def initiate_upload(
    project_id: str,
    payload: UploadInitiateRequest,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.ARTIFACT_UPLOAD))],
) -> UploadInitiateResponse:
    tenant_project(db, user, project_id)
    settings = get_settings()
    if payload.byte_size > settings.max_upload_bytes:
        raise ValidationFailed(
            f"{payload.filename} is {payload.byte_size} bytes, above the {settings.max_upload_bytes} byte limit",
            code="upload_too_large",
        )
    return UploadInitiateResponse(
        upload_url=f"{settings.api_prefix}/projects/{project_id}/artifacts",
        max_bytes=settings.max_upload_bytes,
        accepted_kinds=[k.value for k in ArtifactKind if k is not ArtifactKind.UNKNOWN],
        part_size_bytes=8 * 1024 * 1024,
        note="Upload the file to upload_url as multipart/form-data. Unsupported files are quarantined, not rejected silently.",
    )


@router.post("/{project_id}/artifacts", response_model=ArtifactOut, status_code=201, summary="Upload a source artifact")
def upload_artifact(
    project_id: str,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.ARTIFACT_UPLOAD))],
    file: Annotated[UploadFile, File()],
    provenance: Annotated[str | None, Form()] = None,
    capture_session_id: Annotated[str | None, Form()] = None,
    capture_region: Annotated[str | None, Form()] = None,
    calibration_target_detected: Annotated[bool, Form()] = False,
) -> SourceArtifact:
    import json

    project = tenant_project(db, user, project_id)
    declaration: dict[str, Any] = {}
    if provenance:
        try:
            declaration = json.loads(provenance)
        except json.JSONDecodeError as exc:
            raise ValidationFailed("provenance must be a JSON object", code="bad_provenance") from exc

    metrics: dict[str, Any] = {}
    if capture_region:
        metrics["region"] = capture_region
    if calibration_target_detected:
        metrics["calibration_target_detected"] = True

    artifact = evidence_service.ingest_artifact(
        db,
        project=project,
        filename=file.filename or "upload.bin",
        stream=file.file,
        media_type=file.content_type or "application/octet-stream",
        uploader_id=user.id,
        provenance=declaration,
        capture_session_id=capture_session_id,
        capture_metrics=metrics,
    )
    queue.enqueue(
        db,
        tenant_id=user.tenant_id,
        kind=JobKind.ARTIFACT_PARSE.value,
        project_id=project.id,
        payload={"artifact_id": artifact.id},
        requested_by_id=user.id,
    )
    if capture_session_id:
        session = db.get(CaptureSession, capture_session_id)
        if session and capture_region:
            covered = dict(session.covered_regions or {})
            covered[capture_region] = covered.get(capture_region, 0) + 1
            session.covered_regions = covered
            required = set(session.required_regions or [])
            session.completeness = round(len(required & set(covered)) / max(len(required), 1), 3)
            db.add(session)
    db.commit()
    db.refresh(artifact)
    return artifact


@router.get("/{project_id}/artifacts", response_model=list[ArtifactOut], summary="List artifacts")
def list_artifacts(project_id: str, db: DbSession, user: CurrentUser) -> list[SourceArtifact]:
    tenant_project(db, user, project_id)
    return list(
        db.execute(
            select(SourceArtifact)
            .where(SourceArtifact.project_id == project_id)
            .order_by(SourceArtifact.created_at)
        ).scalars()
    )


@router.get("/{project_id}/artifacts/{artifact_id}/content", summary="Download the original artifact bytes")
def download_artifact(project_id: str, artifact_id: str, db: DbSession, user: CurrentUser):
    from fastapi.responses import Response

    tenant_project(db, user, project_id)
    artifact = scoped(db, user, SourceArtifact, artifact_id, "Artifact")
    data = get_store().get_bytes(artifact.storage_key)
    return Response(
        content=data,
        media_type=artifact.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{artifact.filename}"',
            "X-Content-Hash": artifact.content_hash,
        },
    )


@router.patch(
    "/{project_id}/artifacts/{artifact_id}/authority",
    response_model=ArtifactOut,
    summary="Override the evidence authority classification",
)
def override_authority(
    project_id: str,
    artifact_id: str,
    payload: AuthorityOverride,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.ARTIFACT_CLASSIFY))],
) -> SourceArtifact:
    tenant_project(db, user, project_id)
    artifact = scoped(db, user, SourceArtifact, artifact_id, "Artifact")
    evidence_service.override_authority(
        db, artifact, AuthorityRank(payload.authority), reason=payload.reason, actor_id=user.id
    )
    db.commit()
    db.refresh(artifact)
    return artifact


# ------------------------------------------------------------ capture sessions
@router.post(
    "/{project_id}/capture-sessions",
    response_model=CaptureSessionOut,
    status_code=201,
    summary="Start a guided capture session",
)
def start_capture(
    project_id: str,
    payload: CaptureSessionCreate,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.ARTIFACT_UPLOAD))],
) -> CaptureSession:
    project = tenant_project(db, user, project_id)
    session = CaptureSession(
        tenant_id=user.tenant_id,
        project_id=project.id,
        calibration_target=payload.calibration_target,
        required_regions=payload.required_regions,
        threshold=payload.threshold,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


@router.get("/{project_id}/capture-sessions", response_model=list[CaptureSessionOut], summary="List capture sessions")
def list_capture(project_id: str, db: DbSession, user: CurrentUser) -> list[CaptureSession]:
    tenant_project(db, user, project_id)
    return list(db.execute(select(CaptureSession).where(CaptureSession.project_id == project_id)).scalars())


# -------------------------------------------------------------------- evidence
@router.get("/{project_id}/observations", summary="List evidence observations")
def list_observations(project_id: str, db: DbSession, user: CurrentUser) -> list[dict[str, Any]]:
    tenant_project(db, user, project_id)
    rows = db.execute(
        select(EvidenceObservation)
        .where(EvidenceObservation.project_id == project_id)
        .order_by(EvidenceObservation.created_at)
    ).scalars()
    return [
        {
            "id": o.id,
            "attribute": o.attribute,
            "feature_key": o.feature_key,
            "value": o.value,
            "value_text": o.value_text,
            "unit": o.unit,
            "original_representation": o.original_representation,
            "authority": o.authority,
            "authority_rank": AuthorityRank(o.authority).rank,
            "method": o.method,
            "confidence": o.confidence,
            "uncertainty": o.uncertainty,
            "status": o.status,
            "criticality": o.criticality,
            "disposition": o.disposition,
            "disposition_reason": o.disposition_reason,
            "source_artifact_id": o.source_artifact_id,
            "source_region": o.source_region,
            "superseded_by_id": o.superseded_by_id,
            "version": o.version,
            "audit": o.audit,
        }
        for o in rows
    ]


@router.post("/{project_id}/observations", status_code=201, summary="Record an engineer-entered value")
def create_observation(
    project_id: str,
    payload: ObservationCreate,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.GEOMETRY_EDIT))],
) -> dict[str, Any]:
    project = tenant_project(db, user, project_id)
    observation = evidence_service.record_observation(
        db,
        project=project,
        attribute=payload.attribute,
        value=payload.value,
        value_text=payload.value_text,
        unit=payload.unit,
        feature_key=payload.feature_key,
        source_artifact_id=payload.source_artifact_id,
        authority=AuthorityRank(payload.authority),
        method=payload.method,
        confidence=payload.confidence,
        uncertainty=payload.uncertainty,
        status=VerificationStatus(payload.status),
        criticality=Criticality(payload.criticality),
        actor_id=user.id,
    )
    db.commit()
    return {"id": observation.id, "status": observation.status, "confidence": observation.confidence}


@router.post("/{project_id}/observations/{observation_id}/disposition", summary="Accept, edit, reject or waive a value")
def disposition(
    project_id: str,
    observation_id: str,
    payload: DispositionRequest,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.GEOMETRY_EDIT))],
) -> dict[str, Any]:
    tenant_project(db, user, project_id)
    observation = scoped(db, user, EvidenceObservation, observation_id, "Observation")
    evidence_service.disposition_observation(
        db,
        observation,
        disposition=Disposition(payload.disposition),
        actor_id=user.id,
        reason=payload.reason,
        new_value=payload.new_value,
        new_status=VerificationStatus(payload.new_status) if payload.new_status else None,
    )
    db.commit()
    db.refresh(observation)
    return {
        "id": observation.id,
        "value": observation.value,
        "status": observation.status,
        "disposition": observation.disposition,
        "version": observation.version,
    }


@router.get("/{project_id}/conflicts", summary="List evidence conflicts")
def list_conflicts(project_id: str, db: DbSession, user: CurrentUser, include_resolved: bool = False) -> list[dict[str, Any]]:
    tenant_project(db, user, project_id)
    query = select(EvidenceConflict).where(EvidenceConflict.project_id == project_id)
    if not include_resolved:
        query = query.where(EvidenceConflict.resolved.is_(False))
    return [
        {
            "id": c.id,
            "attribute": c.attribute,
            "feature_key": c.feature_key,
            "observation_ids": c.observation_ids,
            "authoritative_observation_id": c.authoritative_observation_id,
            "delta": c.delta,
            "severity": c.severity,
            "summary": c.summary,
            "resolved": c.resolved,
            "resolution": c.resolution,
        }
        for c in db.execute(query).scalars()
    ]


@router.post("/{project_id}/conflicts/{conflict_id}/resolve", summary="Resolve an evidence conflict")
def resolve_conflict(
    project_id: str,
    conflict_id: str,
    payload: ConflictResolution,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.GEOMETRY_EDIT))],
) -> ActionResult:
    tenant_project(db, user, project_id)
    conflict = scoped(db, user, EvidenceConflict, conflict_id, "Conflict")
    evidence_service.resolve_conflict(
        db, conflict, chosen_observation_id=payload.chosen_observation_id, actor_id=user.id, reason=payload.reason
    )
    db.commit()
    return ActionResult(message="Conflict resolved", detail={"conflict_id": conflict.id})


@router.post("/{project_id}/waivers", status_code=201, summary="Grant an authorised waiver against a gate finding")
def grant_waiver(
    project_id: str,
    payload: WaiverRequest,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.WAIVER_GRANT))],
) -> dict[str, Any]:
    project = tenant_project(db, user, project_id)
    waiver = Waiver(
        tenant_id=user.tenant_id,
        project_id=project.id,
        gate=payload.gate,
        finding_code=payload.finding_code,
        object_ref=payload.object_ref,
        rationale=payload.rationale,
        granted_by_id=user.id,
    )
    db.add(waiver)
    audit.record(
        db,
        tenant_id=user.tenant_id,
        object_kind="waiver",
        object_id=waiver.id,
        action="grant",
        actor_id=user.id,
        after={"gate": payload.gate, "finding": payload.finding_code, "object": payload.object_ref},
        reason=payload.rationale,
    )
    db.commit()
    return {
        "id": waiver.id,
        "gate": waiver.gate,
        "finding_code": waiver.finding_code,
        "note": "S1 stop conditions are never waivable; this waiver applies only to S2 and below.",
    }


@router.get("/{project_id}/waivers", summary="List waivers")
def list_waivers(project_id: str, db: DbSession, user: CurrentUser) -> list[dict[str, Any]]:
    tenant_project(db, user, project_id)
    return [
        {
            "id": w.id,
            "gate": w.gate,
            "finding_code": w.finding_code,
            "object_ref": w.object_ref,
            "rationale": w.rationale,
            "granted_by_id": w.granted_by_id,
            "revoked": w.revoked,
            "created_at": w.created_at,
        }
        for w in db.execute(select(Waiver).where(Waiver.project_id == project_id)).scalars()
    ]
