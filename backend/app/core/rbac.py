"""Role based access control.

Authorization is evaluated at object and action level, not only at route level
(PRD 9.1). Postprocessor certification and NC release are deliberately separate
privileges so the four-eyes rule in PRD 2.1 is enforceable.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    PROJECT_ENGINEER = "project_engineer"
    REVERSE_ENGINEER = "reverse_engineer"
    MANUFACTURING_ENGINEER = "manufacturing_engineer"
    CAM_PROGRAMMER = "cam_programmer"
    ESTIMATOR = "estimator"
    QUALITY_ENGINEER = "quality_engineer"
    NC_RELEASE_APPROVER = "nc_release_approver"
    ADMINISTRATOR = "administrator"


class Permission(StrEnum):
    PROJECT_CREATE = "project:create"
    PROJECT_READ = "project:read"
    PROJECT_UPDATE = "project:update"
    ARTIFACT_UPLOAD = "artifact:upload"
    ARTIFACT_CLASSIFY = "artifact:classify"
    GEOMETRY_EDIT = "geometry:edit"
    GEOMETRY_APPROVE = "geometry:approve"
    PLAN_CREATE = "plan:create"
    PLAN_APPROVE = "plan:approve"
    TOOLPATH_EDIT = "toolpath:edit"
    SIMULATION_RUN = "simulation:run"
    SIMULATION_DISPOSITION = "simulation:disposition"
    COST_EDIT = "cost:edit"
    INSPECTION_APPROVE = "inspection:approve"
    POST_CERTIFY = "post:certify"
    NC_POSTPROCESS = "nc:postprocess"
    NC_RELEASE = "nc:release"
    RUN_RECORD = "run:record"
    RULE_PROMOTE = "rule:promote"
    MASTER_DATA_MANAGE = "masterdata:manage"
    TENANT_ADMIN = "tenant:admin"
    WAIVER_GRANT = "waiver:grant"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.PROJECT_ENGINEER: frozenset(
        {
            Permission.PROJECT_CREATE,
            Permission.PROJECT_READ,
            Permission.PROJECT_UPDATE,
            Permission.ARTIFACT_UPLOAD,
            Permission.ARTIFACT_CLASSIFY,
        }
    ),
    Role.REVERSE_ENGINEER: frozenset(
        {
            Permission.PROJECT_READ,
            Permission.ARTIFACT_UPLOAD,
            Permission.ARTIFACT_CLASSIFY,
            Permission.GEOMETRY_EDIT,
            Permission.GEOMETRY_APPROVE,
        }
    ),
    Role.MANUFACTURING_ENGINEER: frozenset(
        {
            Permission.PROJECT_READ,
            Permission.GEOMETRY_EDIT,
            Permission.PLAN_CREATE,
            Permission.PLAN_APPROVE,
            Permission.TOOLPATH_EDIT,
            Permission.SIMULATION_RUN,
            Permission.SIMULATION_DISPOSITION,
            Permission.MASTER_DATA_MANAGE,
            Permission.WAIVER_GRANT,
            Permission.RULE_PROMOTE,
        }
    ),
    Role.CAM_PROGRAMMER: frozenset(
        {
            Permission.PROJECT_READ,
            Permission.PLAN_CREATE,
            Permission.TOOLPATH_EDIT,
            Permission.SIMULATION_RUN,
            Permission.NC_POSTPROCESS,
        }
    ),
    Role.ESTIMATOR: frozenset({Permission.PROJECT_READ, Permission.COST_EDIT}),
    Role.QUALITY_ENGINEER: frozenset(
        {Permission.PROJECT_READ, Permission.INSPECTION_APPROVE, Permission.RUN_RECORD}
    ),
    Role.NC_RELEASE_APPROVER: frozenset(
        {Permission.PROJECT_READ, Permission.NC_RELEASE, Permission.SIMULATION_DISPOSITION}
    ),
    Role.ADMINISTRATOR: frozenset(
        {
            Permission.PROJECT_READ,
            Permission.MASTER_DATA_MANAGE,
            Permission.POST_CERTIFY,
            Permission.TENANT_ADMIN,
            Permission.RULE_PROMOTE,
        }
    ),
}


def permissions_for(roles: list[str] | tuple[str, ...]) -> frozenset[Permission]:
    granted: set[Permission] = set()
    for raw in roles:
        try:
            role = Role(raw)
        except ValueError:
            continue
        granted |= ROLE_PERMISSIONS[role]
    return frozenset(granted)


def has_permission(roles: list[str] | tuple[str, ...], permission: Permission) -> bool:
    return permission in permissions_for(roles)
