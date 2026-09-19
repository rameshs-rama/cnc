"""Request and response models for the public API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


# --------------------------------------------------------------------- auth
class TokenRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_minutes: int
    user_id: str
    tenant_id: str
    roles: list[str]
    permissions: list[str]
    mfa_enabled: bool


class MfaEnrolResponse(BaseModel):
    secret: str
    otpauth_uri: str
    note: str


# ------------------------------------------------------------------ projects
class ProjectCreate(BaseModel):
    part_number: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=200)
    revision: str = "A"
    quantity: int = Field(default=1, ge=1)
    unit_system: Literal["metric", "imperial"] = "metric"
    intended_use: str | None = None
    target_material_code: str | None = None
    due_date: datetime | None = None
    provenance_declaration: dict[str, Any] = Field(default_factory=dict)


class ProjectUpdate(BaseModel):
    name: str | None = None
    quantity: int | None = Field(default=None, ge=1)
    intended_use: str | None = None
    target_material_code: str | None = None
    due_date: datetime | None = None
    provenance_declaration: dict[str, Any] | None = None
    expected_version: int | None = None


class ProjectOut(ORMModel):
    id: str
    part_number: str
    revision: str
    name: str
    state: str
    quantity: int
    unit_system: str
    intended_use: str | None
    target_material_code: str | None
    due_date: datetime | None
    provenance_declaration: dict[str, Any]
    version: int
    created_at: datetime
    updated_at: datetime


class TransitionRequest(BaseModel):
    target_state: str
    reason: str | None = None


# ----------------------------------------------------------------- artifacts
class ArtifactOut(ORMModel):
    id: str
    filename: str
    kind: str
    media_type: str
    byte_size: int
    content_hash: str
    authority: str
    authority_overridden: bool
    authority_override_reason: str | None
    status: str
    parser: str | None
    parse_warnings: list[str]
    extracted: dict[str, Any]
    capture_metrics: dict[str, Any]
    created_at: datetime


class AuthorityOverride(BaseModel):
    authority: str
    reason: str = Field(min_length=3)


class UploadInitiateRequest(BaseModel):
    filename: str
    byte_size: int = Field(ge=0)
    media_type: str = "application/octet-stream"
    provenance_declaration: dict[str, Any] = Field(default_factory=dict)


class UploadInitiateResponse(BaseModel):
    upload_url: str
    method: str = "POST"
    max_bytes: int
    accepted_kinds: list[str]
    part_size_bytes: int
    note: str


class CaptureSessionCreate(BaseModel):
    calibration_target: str = "checkerboard-25mm"
    required_regions: list[str] = Field(default_factory=lambda: ["top", "bottom", "front", "back", "left", "right"])
    threshold: float = 0.85


class CaptureSessionOut(ORMModel):
    id: str
    calibration_target: str
    required_regions: list[str]
    covered_regions: dict[str, Any]
    completeness: float
    threshold: float
    waiver_reason: str | None
    closed: bool


# ------------------------------------------------------------------ geometry
class GeometryJobRequest(BaseModel):
    project_id: str
    artifact_ids: list[str] = Field(default_factory=list)
    known_dimensions: dict[str, float] = Field(default_factory=dict)
    parent_geometry_id: str | None = None


class GeometryOut(ORMModel):
    id: str
    project_id: str
    revision: int
    parent_id: str | None
    units: str
    scale_established: bool
    scale_source: str | None
    scale_uncertainty_mm: float | None
    provisional: bool
    status: str
    content_hash: str
    quality_report: dict[str, Any]
    part_model: dict[str, Any]
    created_at: datetime
    version: int


class ObservationCreate(BaseModel):
    attribute: str
    value: float | None = None
    value_text: str | None = None
    unit: str = "mm"
    feature_key: str | None = None
    source_artifact_id: str | None = None
    method: str = "engineer entry"
    authority: str = "Measurement"
    confidence: float = Field(default=0.95, ge=0.0, le=1.0)
    uncertainty: float | None = None
    status: str = "Confirmed"
    criticality: str = "Function critical"


class DispositionRequest(BaseModel):
    disposition: Literal["Accept", "Edit", "Reject", "Waive"]
    reason: str | None = None
    new_value: float | None = None
    new_status: str | None = None


class ConflictResolution(BaseModel):
    chosen_observation_id: str
    reason: str = Field(min_length=3)


class ApprovalRequest(BaseModel):
    note: str | None = None
    expected_version: int | None = None


class WaiverRequest(BaseModel):
    gate: str
    finding_code: str
    object_ref: str | None = None
    rationale: str = Field(min_length=10)


# ------------------------------------------------------------------- planning
class PlanGenerateRequest(BaseModel):
    project_id: str
    geometry_version_id: str
    material_id: str
    fixture_id: str | None = None
    machine_ids: list[str] = Field(default_factory=list)
    objective: str | dict[str, Any] = "balanced"


class PlanOut(ORMModel):
    id: str
    project_id: str
    label: str
    geometry_version_id: str
    machine_version_id: str
    fixture_version_id: str | None
    material_id: str | None
    objective: dict[str, Any]
    candidate_rank: int
    objective_score: float
    score_breakdown: dict[str, Any]
    stock: dict[str, Any]
    decision_record: dict[str, Any]
    status: str
    stale: bool
    stale_reason: str | None
    content_hash: str
    version: int
    created_at: datetime


class OperationOut(ORMModel):
    id: str
    sequence: int
    operation_type: str
    feature_keys: list[str]
    tool_assembly_id: str
    parameters: dict[str, Any]
    parameter_rationale: dict[str, Any]
    coolant: str
    suppressed: bool


class SetupOut(ORMModel):
    id: str
    sequence: int
    name: str
    work_offset: str
    orientation_deg: list[float]
    index_position: dict[str, Any]
    clearance_plane_mm: float
    setup_minutes: float
    datum_scheme: dict[str, Any]
    operations: list[OperationOut]


class FeasibilityOut(ORMModel):
    machine_version_id: str
    feasible: bool
    cause_code: str | None
    binding_constraint: str | None
    detail: dict[str, Any]


# --------------------------------------------------------------- verification
class SimulationRequest(BaseModel):
    plan_id: str
    voxel_pitch: float = Field(default=1.0, gt=0.1, le=5.0)
    tolerance_mm: float = Field(default=0.05, gt=0.0, le=1.0)


class SimulationOut(ORMModel):
    id: str
    plan_id: str
    engine_version: str
    identity_hash: str
    input_hashes: dict[str, Any]
    voxel_size_mm: float
    passed: bool
    events: list[dict[str, Any]]
    event_counts: dict[str, Any]
    max_severity: str | None
    cycle_time_seconds: float
    time_breakdown: dict[str, Any]
    stock_comparison: dict[str, Any]
    programmed_envelope: dict[str, Any]
    stale: bool
    dispositions: list[dict[str, Any]]
    created_at: datetime


class EventDisposition(BaseModel):
    event_code: str
    decision: Literal["acknowledged", "resolved", "accepted_risk"]
    reason: str = Field(min_length=3)


class OptimizationRequest(BaseModel):
    plan_id: str
    weights: str | dict[str, float] = "balanced"
    seed: int = 20260919
    budget: int = Field(default=60, ge=4, le=400)
    locked_variables: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------- costing
class CostRequest(BaseModel):
    plan_id: str
    simulation_id: str
    quantity: int | None = Field(default=None, ge=1)
    rate_overrides: dict[str, float] = Field(default_factory=dict)


class CostOut(ORMModel):
    id: str
    plan_id: str
    currency: str
    quantity: int
    rates: dict[str, Any]
    assumptions: dict[str, Any]
    breakdown: dict[str, Any]
    unit_cost: float
    unit_price: float
    quantity_breaks: list[dict[str, Any]]
    sensitivity: dict[str, Any]
    stale: bool
    created_at: datetime


# ----------------------------------------------------------------- NC/release
class PostprocessRequest(BaseModel):
    plan_id: str
    simulation_id: str
    post_id: str
    program_number: str | None = None


class NCProgramOut(ORMModel):
    id: str
    plan_id: str
    program_number: str
    line_count: int
    program_hash: str
    ir_hash: str
    validations: list[dict[str, Any]]
    validation_passed: bool
    max_severity: str | None
    programmed_envelope: dict[str, Any]
    status: str
    stale: bool
    created_at: datetime


class ReleasePrepareRequest(BaseModel):
    plan_id: str
    simulation_id: str
    nc_program_ids: list[str] = Field(min_length=1)


class ReleaseApproveRequest(BaseModel):
    totp_code: str = Field(min_length=6, max_length=8)
    statement: str = Field(min_length=10)
    checklist: dict[str, bool]


class ReleaseOut(ORMModel):
    id: str
    project_id: str
    plan_id: str
    revision: int
    nc_program_ids: list[str]
    manifest: dict[str, Any]
    package_hash: str
    package_signature: str | None
    gate_results: list[dict[str, Any]]
    status: str
    superseded_by_id: str | None
    released_at: datetime | None
    version: int
    created_at: datetime


class PostCertifyRequest(BaseModel):
    machine_version_id: str
    note: str = Field(min_length=5)
    test_program_hashes: list[str] = Field(min_length=1)


class PostRevokeRequest(BaseModel):
    reason: str = Field(min_length=5)


# -------------------------------------------------------------- production
class MachineRunCreate(BaseModel):
    release_id: str
    nc_program_id: str
    nc_program_hash: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    operator: str = ""
    actual_setup_seconds: float | None = None
    actual_cycle_seconds: float | None = None
    actual_tool_changes: int | None = None
    alarms: list[dict[str, Any]] = Field(default_factory=list)
    tool_outcomes: list[dict[str, Any]] = Field(default_factory=list)
    scrap_count: int = 0
    pieces: int = 1
    result: Literal["Pending", "Good", "Rework", "Scrap", "Aborted"] = "Pending"
    notes: str | None = None


class InspectionCreate(BaseModel):
    project_id: str
    machine_run_id: str | None = None
    feature_key: str
    characteristic: str = "diameter"
    nominal: float
    actual: float
    tolerance_plus: float = 0.05
    tolerance_minus: float = 0.05
    unit: str = "mm"
    instrument: dict[str, Any] = Field(default_factory=dict)
    disposition: str | None = None


class ProposalReview(BaseModel):
    approve: bool
    note: str = Field(min_length=5)


# ------------------------------------------------------------------- jobs
class JobOut(ORMModel):
    id: str
    kind: str
    project_id: str | None
    status: str
    stage: str
    percent: float
    payload: dict[str, Any]
    result: dict[str, Any]
    error: str | None
    attempts: int
    trace_id: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class JobLogsOut(BaseModel):
    job_id: str
    status: str
    stage: str
    percent: float
    logs: list[dict[str, Any]]
