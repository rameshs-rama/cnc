"""Evidence intake, observation management and conflict detection.

The rule the whole product rests on: a value exists only with its source,
method, confidence and verification status attached (PRD 1.3).
"""

from __future__ import annotations

from typing import Any, BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import audit, events
from app.core.enums import (
    AUTHORITY_RANKS,
    ArtifactKind,
    ArtifactStatus,
    AuthorityRank,
    Criticality,
    Disposition,
    Severity,
    VerificationStatus,
)
from app.core.errors import NotFound, ValidationFailed
from app.core.storage import get_store
from app.models.base import audit_entry
from app.models.identity import Tenant
from app.models.project import EvidenceConflict, EvidenceObservation, PartProject, SourceArtifact
from app.parsers import classify, parse_artifact


def ingest_artifact(
    db: Session,
    *,
    project: PartProject,
    filename: str,
    stream: BinaryIO,
    media_type: str = "application/octet-stream",
    uploader_id: str | None = None,
    provenance: dict[str, Any] | None = None,
    capture_session_id: str | None = None,
    capture_metrics: dict[str, Any] | None = None,
) -> SourceArtifact:
    """Store the original bytes, hash them, classify and parse.

    The bytes are never modified. Correcting an artifact means uploading a new
    one that supersedes it (PRD 8.2).
    """
    store = get_store()
    key, digest, size = store.put_stream(project.tenant_id, f"artifacts/{project.id}", stream)
    data = store.get_bytes(key)
    kind, authority = classify(filename, data[:4096])

    artifact = SourceArtifact(
        tenant_id=project.tenant_id,
        project_id=project.id,
        filename=filename,
        media_type=media_type,
        kind=kind.value,
        byte_size=size,
        content_hash=digest,
        storage_key=key,
        authority=authority.value,
        status=ArtifactStatus.PARSING.value,
        uploader_id=uploader_id,
        provenance_declaration=provenance or {},
        capture_session_id=capture_session_id,
        capture_metrics=capture_metrics or {},
    )
    db.add(artifact)
    db.flush()

    result = parse_artifact(kind, data, filename)
    artifact.status = result.status.value
    artifact.parser = result.parser
    artifact.extracted = result.extracted
    artifact.parse_warnings = list(result.warnings) + ([result.error] if result.error else [])

    audit.record(
        db,
        tenant_id=project.tenant_id,
        object_kind="artifact",
        object_id=artifact.id,
        action="ingest",
        actor_id=uploader_id,
        after={"hash": digest, "kind": kind.value, "authority": authority.value, "status": artifact.status},
    )
    events.publish(
        db,
        tenant_id=project.tenant_id,
        topic=events.Topic.ARTIFACT_PROCESSED,
        project_id=project.id,
        actor_id=uploader_id,
        payload={
            "artifact_id": artifact.id,
            "parser": result.parser,
            "status": artifact.status,
            "warnings": artifact.parse_warnings,
        },
    )
    return artifact


def override_authority(
    db: Session, artifact: SourceArtifact, authority: AuthorityRank, *, reason: str, actor_id: str
) -> SourceArtifact:
    """Reclassify evidence authority. A reason is mandatory (FR-INT-004)."""
    if not reason.strip():
        raise ValidationFailed("An authority override requires a reason", code="reason_required")
    before = artifact.authority
    artifact.authority = authority.value
    artifact.authority_overridden = True
    artifact.authority_override_reason = reason
    db.add(artifact)
    audit.record(
        db,
        tenant_id=artifact.tenant_id,
        object_kind="artifact",
        object_id=artifact.id,
        action="authority_override",
        actor_id=actor_id,
        before={"authority": before},
        after={"authority": authority.value},
        reason=reason,
    )
    return artifact


def record_observation(
    db: Session,
    *,
    project: PartProject,
    attribute: str,
    value: float | None,
    unit: str = "mm",
    geometry_version_id: str | None = None,
    feature_key: str | None = None,
    source_artifact_id: str | None = None,
    source_region: dict[str, Any] | None = None,
    authority: AuthorityRank = AuthorityRank.AI_INFERENCE,
    method: str = "unspecified",
    confidence: float = 0.0,
    uncertainty: float | None = None,
    residual: float | None = None,
    status: VerificationStatus = VerificationStatus.INFERRED,
    criticality: Criticality = Criticality.NONCRITICAL,
    value_text: str | None = None,
    original_representation: str | None = None,
    actor_id: str | None = None,
    detect_conflicts: bool = True,
) -> EvidenceObservation:
    observation = EvidenceObservation(
        tenant_id=project.tenant_id,
        project_id=project.id,
        geometry_version_id=geometry_version_id,
        feature_key=feature_key,
        attribute=attribute,
        value=value,
        value_text=value_text,
        unit=unit,
        original_representation=original_representation,
        source_artifact_id=source_artifact_id,
        source_region=source_region or {},
        authority=authority.value,
        method=method,
        confidence=confidence,
        uncertainty=uncertainty,
        residual=residual,
        status=status.value,
        criticality=criticality.value,
        audit=[audit_entry(actor_id or "system", "created")],
    )
    db.add(observation)
    db.flush()
    if detect_conflicts:
        detect_for(db, project, attribute=attribute, feature_key=feature_key)
    return observation


def disposition_observation(
    db: Session,
    observation: EvidenceObservation,
    *,
    disposition: Disposition,
    actor_id: str,
    reason: str | None = None,
    new_value: float | None = None,
    new_status: VerificationStatus | None = None,
) -> EvidenceObservation:
    """Accept, edit, reject or waive a value, keeping the prior state (FR-ENG-003)."""
    before = {"value": observation.value, "status": observation.status, "disposition": observation.disposition}

    if disposition is Disposition.EDIT:
        if new_value is None:
            raise ValidationFailed("An edit disposition requires the new value", code="value_required")
        observation.value = new_value
        observation.status = (new_status or VerificationStatus.CONFIRMED).value
        observation.authority = AuthorityRank.MEASUREMENT.value
        observation.method = "engineer edit"
        observation.confidence = max(observation.confidence, 0.95)
    elif disposition is Disposition.ACCEPT:
        observation.status = (new_status or VerificationStatus.CONFIRMED).value
        observation.confidence = max(observation.confidence, 0.9)
    elif disposition is Disposition.REJECT:
        observation.status = VerificationStatus.UNKNOWN.value
        observation.confidence = 0.0
    elif disposition is Disposition.WAIVE and not (reason or "").strip():
        raise ValidationFailed("A waiver requires a rationale", code="reason_required")

    observation.disposition = disposition.value
    observation.disposition_reason = reason
    observation.verified_by_id = actor_id
    observation.version += 1
    observation.audit = [*(observation.audit or []), audit_entry(actor_id, f"disposition:{disposition}", reason, before=before)]
    db.add(observation)

    audit.record(
        db,
        tenant_id=observation.tenant_id,
        object_kind="observation",
        object_id=observation.id,
        action=f"disposition:{disposition}",
        actor_id=actor_id,
        before=before,
        after={"value": observation.value, "status": observation.status},
        reason=reason,
    )
    project = db.get(PartProject, observation.project_id)
    if project:
        detect_for(db, project, attribute=observation.attribute, feature_key=observation.feature_key)
    return observation


def detect_for(db: Session, project: PartProject, *, attribute: str, feature_key: str | None) -> EvidenceConflict | None:
    """Find disagreement between live observations of one attribute (FR-INT-006).

    Two observations conflict when their uncertainty intervals do not overlap
    and the gap exceeds the tenant tolerance. The authoritative observation is
    identified but never applied automatically; a human dispositions it.
    """
    tenant = db.get(Tenant, project.tenant_id)
    tolerance = tenant.conflict_tolerance_mm if tenant else 0.05

    query = select(EvidenceObservation).where(
        EvidenceObservation.project_id == project.id,
        EvidenceObservation.attribute == attribute,
        EvidenceObservation.superseded_by_id.is_(None),
        EvidenceObservation.disposition != Disposition.REJECT.value,
        EvidenceObservation.status != VerificationStatus.UNKNOWN.value,
    )
    query = query.where(
        EvidenceObservation.feature_key == feature_key
        if feature_key is not None
        else EvidenceObservation.feature_key.is_(None)
    )
    observations = [o for o in db.execute(query).scalars().all() if o.value is not None]

    existing = db.execute(
        select(EvidenceConflict).where(
            EvidenceConflict.project_id == project.id,
            EvidenceConflict.attribute == attribute,
            EvidenceConflict.feature_key.is_(None) if feature_key is None else EvidenceConflict.feature_key == feature_key,
            EvidenceConflict.resolved.is_(False),
        )
    ).scalar_one_or_none()

    if len(observations) < 2:
        if existing:
            existing.resolved = True
            existing.resolution = {"reason": "Only one live observation remains"}
            db.add(existing)
        return None

    worst: tuple[float, EvidenceObservation, EvidenceObservation] | None = None
    for i, a in enumerate(observations):
        for b in observations[i + 1 :]:
            lo_a, hi_a = a.interval
            lo_b, hi_b = b.interval
            if hi_a >= lo_b and hi_b >= lo_a:
                continue  # intervals overlap; the sources agree within their stated uncertainty
            gap = lo_b - hi_a if lo_b > hi_a else lo_a - hi_b
            delta = abs(a.value - b.value)
            if gap <= tolerance:
                continue
            if worst is None or delta > worst[0]:
                worst = (delta, a, b)

    if worst is None:
        if existing:
            existing.resolved = True
            existing.resolution = {"reason": "Observations now agree within tolerance"}
            db.add(existing)
        return None

    delta, first, second = worst
    ranked = sorted(observations, key=lambda o: (AUTHORITY_RANKS[AuthorityRank(o.authority)], -o.confidence))
    authoritative = ranked[0]
    criticality = max(
        (o.criticality for o in observations),
        key=lambda c: [Criticality.NONCRITICAL, Criticality.QUALITY, Criticality.FUNCTION, Criticality.SAFETY].index(
            Criticality(c)
        ),
    )
    severity = Severity.S2_ENGINEER if Criticality(criticality) != Criticality.NONCRITICAL else Severity.S3_WARNING
    summary = (
        f"{first.method} reports {first.value:.3f} {first.unit} "
        f"({AuthorityRank(first.authority).value}); {second.method} reports {second.value:.3f} {second.unit} "
        f"({AuthorityRank(second.authority).value}). "
        f"{AuthorityRank(authoritative.authority).value} is authoritative and requires an engineer disposition."
    )

    conflict = existing or EvidenceConflict(
        tenant_id=project.tenant_id, project_id=project.id, attribute=attribute, feature_key=feature_key
    )
    conflict.observation_ids = [o.id for o in observations]
    conflict.authoritative_observation_id = authoritative.id
    conflict.delta = round(delta, 6)
    conflict.severity = severity.value
    conflict.summary = summary
    conflict.resolved = False
    db.add(conflict)
    db.flush()

    events.publish(
        db,
        tenant_id=project.tenant_id,
        topic=events.Topic.ENGINEERING_CONFLICT_DETECTED,
        project_id=project.id,
        payload={
            "conflict_id": conflict.id,
            "feature": feature_key,
            "attribute": attribute,
            "observations": conflict.observation_ids,
            "authoritative": authoritative.id,
            "delta": conflict.delta,
            "severity": severity.value,
        },
    )
    return conflict


def resolve_conflict(
    db: Session, conflict: EvidenceConflict, *, chosen_observation_id: str, actor_id: str, reason: str
) -> EvidenceConflict:
    if chosen_observation_id not in (conflict.observation_ids or []):
        raise ValidationFailed("The chosen observation is not part of this conflict", code="not_in_conflict")
    chosen = db.get(EvidenceObservation, chosen_observation_id)
    if chosen is None:
        raise NotFound("Observation not found")

    for observation_id in conflict.observation_ids:
        if observation_id == chosen_observation_id:
            continue
        other = db.get(EvidenceObservation, observation_id)
        if other is not None:
            other.superseded_by_id = chosen_observation_id
            other.disposition = Disposition.REJECT.value
            other.disposition_reason = f"Superseded by conflict resolution: {reason}"
            db.add(other)

    chosen.disposition = Disposition.ACCEPT.value
    chosen.status = VerificationStatus.CONFIRMED.value
    chosen.verified_by_id = actor_id
    db.add(chosen)

    conflict.resolved = True
    conflict.resolution = {"chosen": chosen_observation_id, "actor_id": actor_id, "reason": reason}
    db.add(conflict)
    audit.record(
        db,
        tenant_id=conflict.tenant_id,
        object_kind="conflict",
        object_id=conflict.id,
        action="resolve",
        actor_id=actor_id,
        after=conflict.resolution,
        reason=reason,
    )
    return conflict


def import_extracted_observations(
    db: Session, *, project: PartProject, artifact: SourceArtifact, actor_id: str | None = None
) -> list[EvidenceObservation]:
    """Turn parser output into candidate observations.

    Extracted values enter as Inferred or Verification Required, never as
    Confirmed. Extraction is not verification (FR-INT-005).
    """
    created: list[EvidenceObservation] = []
    kind = ArtifactKind(artifact.kind)
    extracted = artifact.extracted or {}
    authority = AuthorityRank(artifact.authority)

    if kind is ArtifactKind.DRAWING_PDF:
        for candidate in extracted.get("dimension_candidates", []):
            created.append(
                record_observation(
                    db,
                    project=project,
                    attribute=candidate["attribute"],
                    value=candidate.get("value"),
                    value_text=candidate.get("value_text"),
                    original_representation=candidate.get("raw"),
                    source_artifact_id=artifact.id,
                    source_region={"offset": candidate.get("source_offset"), "context": candidate.get("context")},
                    authority=authority,
                    method=f"drawing text extraction ({candidate['kind']})",
                    confidence=candidate.get("confidence", 0.5),
                    uncertainty=(candidate.get("tolerance") or {}).get("plus"),
                    status=VerificationStatus.VERIFICATION_REQUIRED,
                    criticality=Criticality.FUNCTION if candidate.get("tolerance") else Criticality.NONCRITICAL,
                    actor_id=actor_id,
                    detect_conflicts=False,
                )
            )
    elif kind is ArtifactKind.MEASUREMENT_CSV:
        for row in extracted.get("rows", []):
            created.append(
                record_observation(
                    db,
                    project=project,
                    attribute=row["attribute"],
                    feature_key=row.get("feature_key"),
                    value=row.get("value"),
                    unit="mm",
                    original_representation=f"{row.get('original_value')} {row.get('original_unit')}",
                    source_artifact_id=artifact.id,
                    source_region={"line": row.get("line")},
                    authority=AuthorityRank.MEASUREMENT,
                    method=f"measurement: {row.get('instrument') or 'unspecified instrument'}",
                    confidence=0.97 if row.get("instrument") else 0.85,
                    uncertainty=row.get("uncertainty"),
                    status=VerificationStatus.MEASURED,
                    criticality=Criticality(row.get("criticality", Criticality.NONCRITICAL)),
                    actor_id=actor_id,
                    detect_conflicts=False,
                )
            )
    elif kind in (ArtifactKind.STEP, ArtifactKind.STL, ArtifactKind.DXF):
        size = extracted.get("size_mm") or []
        labels = ["length", "width", "height"]
        for index, value in enumerate(size[:3]):
            if not value:
                continue
            created.append(
                record_observation(
                    db,
                    project=project,
                    attribute=labels[index],
                    value=float(value),
                    source_artifact_id=artifact.id,
                    authority=authority,
                    method=f"{artifact.parser} bounding box",
                    confidence=0.9 if kind is ArtifactKind.STEP else 0.85,
                    uncertainty=0.01,
                    status=VerificationStatus.VERIFICATION_REQUIRED,
                    criticality=Criticality.FUNCTION,
                    actor_id=actor_id,
                    detect_conflicts=False,
                )
            )

    # Conflicts are evaluated once, after the whole file is imported, so a
    # multi-row file does not raise the same conflict repeatedly.
    for attribute, feature_key in {(o.attribute, o.feature_key) for o in created}:
        detect_for(db, project, attribute=attribute, feature_key=feature_key)
    return created
