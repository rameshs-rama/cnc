"""Canonical manufacturing model (PRD 8.1).

Importing this package registers every mapper with the declarative base.
"""

from app.models.base import audit_entry, new_id, utcnow
from app.models.factory import (
    FixtureVersion,
    MachineVersion,
    Material,
    PostProcessorVersion,
    ToolAssemblyVersion,
)
from app.models.geometry import GeometryVersion, ManufacturingFeature
from app.models.identity import ProjectAssignment, Tenant, User
from app.models.planning import (
    MachineFeasibility,
    ManufacturingPlan,
    Operation,
    Setup,
    ToolpathVersion,
)
from app.models.platform import (
    ArtifactDependency,
    AuditRecord,
    DomainEvent,
    IdempotencyRecord,
    Job,
)
from app.models.production import InspectionResult, MachineRun, RuleProposal
from app.models.project import (
    CaptureSession,
    EvidenceConflict,
    EvidenceObservation,
    PartProject,
    SourceArtifact,
    Waiver,
)
from app.models.verification import (
    Approval,
    CostEstimate,
    GateEvaluationRecord,
    NCProgram,
    Release,
    SimulationRun,
)

__all__ = [
    "Approval",
    "ArtifactDependency",
    "AuditRecord",
    "CaptureSession",
    "CostEstimate",
    "DomainEvent",
    "EvidenceConflict",
    "EvidenceObservation",
    "FixtureVersion",
    "GateEvaluationRecord",
    "GeometryVersion",
    "IdempotencyRecord",
    "InspectionResult",
    "Job",
    "MachineFeasibility",
    "MachineRun",
    "MachineVersion",
    "ManufacturingFeature",
    "ManufacturingPlan",
    "Material",
    "NCProgram",
    "Operation",
    "PartProject",
    "PostProcessorVersion",
    "ProjectAssignment",
    "Release",
    "RuleProposal",
    "Setup",
    "SimulationRun",
    "SourceArtifact",
    "Tenant",
    "ToolAssemblyVersion",
    "ToolpathVersion",
    "User",
    "Waiver",
    "audit_entry",
    "new_id",
    "utcnow",
]
