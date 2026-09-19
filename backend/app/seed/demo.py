"""Demonstration tenant, users and benchmark project.

The seed creates the pilot factory from ``factory_data`` plus one benchmark
part with real evidence files, so a fresh deployment can be exercised end to
end without hand-assembling master data.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.enums import LifecycleStatus
from app.core.hashing import sha256_json
from app.core.rbac import Role
from app.core.security import generate_totp_secret, hash_password
from app.db import session_scope
from app.engines.post import build_post
from app.models.factory import (
    FixtureVersion,
    MachineVersion,
    Material,
    PostProcessorVersion,
    ToolAssemblyVersion,
)
from app.models.identity import Tenant, User
from app.models.project import PartProject
from app.seed import factory_data

logger = logging.getLogger("mip.seed")

TENANT_SLUG = "rama-pilot"
DEMO_PASSWORD = "Pilot2026!"

#: One user per persona in PRD section 2, so the four-eyes rule is testable.
USERS: list[dict[str, Any]] = [
    ("engineer@example.com", "Priya Raman", [Role.PROJECT_ENGINEER, Role.REVERSE_ENGINEER]),
    ("manufacturing@example.com", "Tomas Weber", [Role.MANUFACTURING_ENGINEER, Role.CAM_PROGRAMMER]),
    ("cam@example.com", "Ana Oliveira", [Role.CAM_PROGRAMMER]),
    ("estimator@example.com", "Lena Fischer", [Role.ESTIMATOR]),
    ("quality@example.com", "Sam Okafor", [Role.QUALITY_ENGINEER]),
    ("approver@example.com", "Marta Kovacs", [Role.NC_RELEASE_APPROVER]),
    ("admin@example.com", "Devan Rao", [Role.ADMINISTRATOR]),
]


def seed_if_empty() -> str | None:
    """Create the demo tenant on first boot. Idempotent."""
    with session_scope() as db:
        existing = db.execute(select(Tenant).where(Tenant.slug == TENANT_SLUG)).scalar_one_or_none()
        if existing:
            return existing.id
    return seed()


def seed() -> str:
    with session_scope() as db:
        tenant = db.execute(select(Tenant).where(Tenant.slug == TENANT_SLUG)).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(
                name="Rama Manufacturing - pilot cell",
                slug=TENANT_SLUG,
                unit_system="metric",
                currency="EUR",
                allow_self_approval=False,
                critical_confidence_threshold=0.90,
                conflict_tolerance_mm=0.05,
                security_profile={"mfa_required_for": ["nc_release"], "data_residency": "eu-west"},
                policy={
                    "prohibited_codes": ["M99", "M98", "G65", "G66", "G10"],
                    "proof_out": "single block, feed override 25 percent, dry run, first-piece inspection",
                },
            )
            db.add(tenant)
            db.flush()

        _seed_users(db, tenant)
        _seed_factory(db, tenant)
        _seed_project(db, tenant)
        logger.info("demo tenant %s seeded", tenant.slug)
        return tenant.id


def _seed_users(db, tenant: Tenant) -> None:
    for email, name, roles in USERS:
        if db.execute(select(User).where(User.email == email)).scalar_one_or_none():
            continue
        role_values = sorted({r.value if hasattr(r, "value") else str(r) for r in roles})
        user = User(
            tenant_id=tenant.id,
            email=email,
            full_name=name,
            password_hash=hash_password(DEMO_PASSWORD),
            roles=role_values,
            is_active=True,
        )
        # The release approver is enrolled for MFA because a release cannot be
        # signed without a second factor.
        if Role.NC_RELEASE_APPROVER.value in role_values:
            user.totp_secret = generate_totp_secret()
            user.mfa_enabled = True
        db.add(user)
    db.flush()


def _seed_factory(db, tenant: Tenant) -> None:
    for spec in factory_data.MACHINES:
        if db.execute(
            select(MachineVersion).where(MachineVersion.tenant_id == tenant.id, MachineVersion.code == spec["code"])
        ).scalar_one_or_none():
            continue
        record = MachineVersion(tenant_id=tenant.id, revision=1, status=LifecycleStatus.CURRENT.value, **spec)
        record.content_hash = sha256_json(spec)
        db.add(record)

    for spec in factory_data.TOOLS:
        if db.execute(
            select(ToolAssemblyVersion).where(
                ToolAssemblyVersion.tenant_id == tenant.id, ToolAssemblyVersion.code == spec["code"]
            )
        ).scalar_one_or_none():
            continue
        data = dict(spec)
        available = data.pop("available", True)
        record = ToolAssemblyVersion(
            tenant_id=tenant.id,
            revision=1,
            status=LifecycleStatus.CURRENT.value,
            available=available,
            has_collision_model=bool(data.get("collision_profile")),
            **data,
        )
        record.content_hash = sha256_json(spec)
        db.add(record)

    for spec in factory_data.FIXTURES:
        if db.execute(
            select(FixtureVersion).where(FixtureVersion.tenant_id == tenant.id, FixtureVersion.code == spec["code"])
        ).scalar_one_or_none():
            continue
        record = FixtureVersion(tenant_id=tenant.id, revision=1, status=LifecycleStatus.CURRENT.value, **spec)
        record.content_hash = sha256_json(spec)
        db.add(record)

    for spec in factory_data.MATERIALS:
        if db.execute(
            select(Material).where(Material.tenant_id == tenant.id, Material.code == spec["code"])
        ).scalar_one_or_none():
            continue
        db.add(Material(tenant_id=tenant.id, **spec))

    for spec in factory_data.POSTS:
        if db.execute(
            select(PostProcessorVersion).where(
                PostProcessorVersion.tenant_id == tenant.id, PostProcessorVersion.code == spec["code"]
            )
        ).scalar_one_or_none():
            continue
        record = PostProcessorVersion(
            tenant_id=tenant.id, revision=1, status=LifecycleStatus.DRAFT.value, enabled=False, certified=False, **spec
        )
        record.runtime_hash = build_post(record.definition).runtime_hash
        db.add(record)
    db.flush()


def _seed_project(db, tenant: Tenant) -> None:
    if db.execute(
        select(PartProject).where(PartProject.tenant_id == tenant.id, PartProject.part_number == "BRK-1042")
    ).scalar_one_or_none():
        return
    owner = db.execute(select(User).where(User.email == "engineer@example.com")).scalar_one_or_none()
    db.add(
        PartProject(
            tenant_id=tenant.id,
            part_number="BRK-1042",
            revision="C",
            name="Mounting bracket, pilot family A",
            quantity=25,
            unit_system="metric",
            intended_use="Structural mounting bracket for a conveyor drive unit",
            target_material_code="AL6082-T6",
            owner_id=owner.id if owner else None,
            provenance_declaration={
                "source": "Customer supplied drawing and sample part",
                "ip_owner": "Customer",
                "permitted_use": "Manufacture for the supplying customer only",
                "export_control": "None declared",
            },
        )
    )
    db.flush()


def sample_files() -> dict[str, Path]:
    """Benchmark artifacts shipped with the repository."""
    root = Path(__file__).resolve().parents[3] / "samples"
    return {
        "step": root / "cad" / "BRK-1042.step",
        "stl": root / "cad" / "BRK-1042-envelope.stl",
        "dxf": root / "drawings" / "BRK-1042.dxf",
        "pdf": root / "drawings" / "BRK-1042.pdf",
        "csv": root / "measurements" / "BRK-1042-firstpiece.csv",
    }
