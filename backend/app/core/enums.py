"""Controlled vocabularies.

These enumerations are the contract between the safety policy engine, the
engineering services and the user interface. Values are stored as strings so an
audit record remains readable without the application.
"""

from __future__ import annotations

from enum import StrEnum


class ProjectState(StrEnum):
    """Project state machine (PRD 3.1)."""

    DRAFT = "Draft"
    EVIDENCE_COLLECTION = "Evidence collection"
    RECONSTRUCTION = "Reconstruction"
    ENGINEERING_REVIEW = "Engineering review"
    PLANNING = "Planning"
    CAM_GENERATION = "CAM generation"
    SIMULATION = "Simulation"
    OPTIMIZATION = "Optimization"
    POSTPROCESSING = "Postprocessing"
    RELEASE_REVIEW = "Release review"
    RELEASED = "Released"
    IN_PRODUCTION = "In production"
    COMPLETED = "Completed"
    ARCHIVED = "Archived"
    SUPERSEDED = "Superseded"
    QUARANTINED = "Quarantined"
    CANCELLED = "Cancelled"


#: Allowed transitions. A transition not listed here is rejected by the workflow
#: service; the project state can only move along this graph (PRD 3.1).
PROJECT_TRANSITIONS: dict[ProjectState, tuple[ProjectState, ...]] = {
    ProjectState.DRAFT: (ProjectState.EVIDENCE_COLLECTION, ProjectState.CANCELLED),
    ProjectState.EVIDENCE_COLLECTION: (ProjectState.RECONSTRUCTION, ProjectState.DRAFT),
    ProjectState.RECONSTRUCTION: (ProjectState.ENGINEERING_REVIEW, ProjectState.EVIDENCE_COLLECTION),
    ProjectState.ENGINEERING_REVIEW: (ProjectState.PLANNING, ProjectState.RECONSTRUCTION),
    ProjectState.PLANNING: (ProjectState.CAM_GENERATION, ProjectState.ENGINEERING_REVIEW),
    ProjectState.CAM_GENERATION: (ProjectState.SIMULATION, ProjectState.PLANNING),
    ProjectState.SIMULATION: (ProjectState.OPTIMIZATION, ProjectState.CAM_GENERATION),
    ProjectState.OPTIMIZATION: (ProjectState.POSTPROCESSING, ProjectState.CAM_GENERATION),
    ProjectState.POSTPROCESSING: (ProjectState.RELEASE_REVIEW, ProjectState.OPTIMIZATION),
    ProjectState.RELEASE_REVIEW: (ProjectState.RELEASED, ProjectState.POSTPROCESSING),
    ProjectState.RELEASED: (ProjectState.IN_PRODUCTION, ProjectState.SUPERSEDED),
    ProjectState.IN_PRODUCTION: (ProjectState.COMPLETED, ProjectState.QUARANTINED),
    ProjectState.COMPLETED: (ProjectState.ARCHIVED, ProjectState.DRAFT),
    ProjectState.QUARANTINED: (ProjectState.IN_PRODUCTION, ProjectState.ARCHIVED),
    ProjectState.SUPERSEDED: (ProjectState.ARCHIVED,),
    ProjectState.ARCHIVED: (),
    ProjectState.CANCELLED: (),
}


class AuthorityRank(StrEnum):
    """Evidence authority hierarchy (PRD 1.3 / 5.1).

    Lower ``rank`` wins a conflict. Verified CAD with PMI outranks drawings,
    which outrank measurements, scans, calibrated photographs, ordinary
    photographs and finally AI inference.
    """

    VERIFIED_CAD_PMI = "Verified CAD and PMI"
    DRAWING = "Drawing"
    MEASUREMENT = "Measurement"
    SCAN = "Scan"
    CALIBRATED_PHOTO = "Calibrated photograph"
    PHOTO = "Ordinary photograph"
    AI_INFERENCE = "AI inference"

    @property
    def rank(self) -> int:
        return AUTHORITY_RANKS[self]


AUTHORITY_RANKS: dict[AuthorityRank, int] = {
    AuthorityRank.VERIFIED_CAD_PMI: 1,
    AuthorityRank.DRAWING: 2,
    AuthorityRank.MEASUREMENT: 3,
    AuthorityRank.SCAN: 4,
    AuthorityRank.CALIBRATED_PHOTO: 5,
    AuthorityRank.PHOTO: 6,
    AuthorityRank.AI_INFERENCE: 7,
}


class VerificationStatus(StrEnum):
    """Status field of the confidence object (PRD 5.1)."""

    CONFIRMED = "Confirmed"
    MEASURED = "Measured"
    INFERRED = "Inferred"
    VERIFICATION_REQUIRED = "Verification Required"
    UNKNOWN = "Unknown"


#: Statuses that satisfy a release-critical attribute without a waiver.
VERIFIED_STATUSES = frozenset({VerificationStatus.CONFIRMED, VerificationStatus.MEASURED})


class Criticality(StrEnum):
    SAFETY = "Safety critical"
    FUNCTION = "Function critical"
    QUALITY = "Quality critical"
    NONCRITICAL = "Noncritical"


#: Criticality levels that block geometry approval when unverified (FR-ENG-004).
RELEASE_CRITICAL = frozenset({Criticality.SAFETY, Criticality.FUNCTION, Criticality.QUALITY})


class Disposition(StrEnum):
    PENDING = "Pending"
    ACCEPT = "Accept"
    EDIT = "Edit"
    REJECT = "Reject"
    WAIVE = "Waive"


class Severity(StrEnum):
    """Severity model (PRD 5.3)."""

    S1_STOP = "S1"
    S2_ENGINEER = "S2"
    S3_WARNING = "S3"
    S4_ADVISORY = "S4"

    @property
    def waivable(self) -> bool:
        """S1 can never be waived in the UI; it must be corrected."""
        return self is not Severity.S1_STOP

    @property
    def blocks_release(self) -> bool:
        return self in (Severity.S1_STOP, Severity.S2_ENGINEER)


class Gate(StrEnum):
    """Default gate matrix (PRD 5.2)."""

    GEOMETRY_APPROVAL = "Geometry approval"
    PLAN_APPROVAL = "Plan approval"
    SIMULATION_PASS = "Simulation pass"
    POST_VALIDATION = "Post validation"
    NC_RELEASE = "NC release"
    PRODUCTION_COMPLETION = "Production completion"


#: What each gate blocks when it fails (PRD 5.2, "Blocks" column).
GATE_BLOCKS: dict[Gate, str] = {
    Gate.GEOMETRY_APPROVAL: "Process planning",
    Gate.PLAN_APPROVAL: "Toolpath generation",
    Gate.SIMULATION_PASS: "Postprocessing",
    Gate.POST_VALIDATION: "NC release",
    Gate.NC_RELEASE: "Download as Released",
    Gate.PRODUCTION_COMPLETION: "Learning eligibility",
}


class ArtifactKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    STEP = "step"
    STL = "stl"
    OBJ = "obj"
    DXF = "dxf"
    DRAWING_PDF = "drawing_pdf"
    MEASUREMENT_CSV = "measurement_csv"
    UNKNOWN = "unknown"


class ArtifactStatus(StrEnum):
    UPLOADED = "Uploaded"
    SCANNING = "Scanning"
    PARSING = "Parsing"
    PROCESSED = "Processed"
    UNSUPPORTED = "Unsupported"
    QUARANTINED = "Quarantined"
    FAILED = "Failed"


class LifecycleStatus(StrEnum):
    """Lifecycle of a versioned engineering artifact."""

    DRAFT = "Draft"
    CURRENT = "Current"
    APPROVED = "Approved"
    STALE = "Stale"
    SUPERSEDED = "Superseded"
    REJECTED = "Rejected"
    REVOKED = "Revoked"


class FeatureType(StrEnum):
    FACE = "Face"
    POCKET = "Pocket"
    SLOT = "Slot"
    HOLE = "Hole"
    COUNTERBORE = "Counterbore"
    COUNTERSINK = "Countersink"
    STEP = "Step"
    BOSS = "Boss"
    ISLAND = "Island"
    CHAMFER = "Chamfer"
    FILLET = "Fillet"
    THREAD_CANDIDATE = "Thread candidate"
    FREEFORM = "Freeform surface"


class FeatureSupport(StrEnum):
    """FR-FTR-003."""

    SUPPORTED = "Supported"
    PARTIAL = "Partially supported"
    MANUAL = "Manual planning required"


class OperationType(StrEnum):
    FACING = "Facing"
    CONTOUR = "Contouring"
    POCKET = "Pocketing"
    ADAPTIVE_ROUGH = "Adaptive roughing"
    REST_ROUGH = "Rest machining"
    DRILL = "Drilling"
    BORE = "Boring"
    REAM = "Reaming"
    TAP = "Tapping"
    CHAMFER = "Chamfering"
    FINISH_3D = "3D finishing"
    SLOT = "Slotting"


class JobKind(StrEnum):
    RECONSTRUCTION = "reconstruction"
    ARTIFACT_PARSE = "artifact_parse"
    PLAN_GENERATE = "plan_generate"
    TOOLPATH_GENERATE = "toolpath_generate"
    SIMULATION = "simulation"
    OPTIMIZATION = "optimization"
    POSTPROCESS = "postprocess"


class JobStatus(StrEnum):
    QUEUED = "Queued"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    CANCELLED = "Cancelled"


class ReleaseStatus(StrEnum):
    CANDIDATE = "Candidate"
    IN_REVIEW = "In review"
    RELEASED = "Released"
    REJECTED = "Rejected"
    SUPERSEDED = "Superseded"
    REVOKED = "Revoked"


class RunResult(StrEnum):
    PENDING = "Pending"
    GOOD = "Good"
    REWORK = "Rework"
    SCRAP = "Scrap"
    ABORTED = "Aborted"


class ProposalStatus(StrEnum):
    PROPOSED = "Proposed"
    UNDER_REVIEW = "Under review"
    APPROVED = "Approved"
    REJECTED = "Rejected"
