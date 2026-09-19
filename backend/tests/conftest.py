"""Test fixtures.

Each test module gets an isolated database and object store, so a test can
never see state another test left behind.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The database engine is created when ``app.db`` is imported, and pytest imports
# the test modules - and therefore the application - before any fixture runs.
# The workspace has to be pointed at a scratch directory here, at conftest import
# time, or the suite would silently run against the developer's real database.
_WORKSPACE = Path(tempfile.mkdtemp(prefix="mip-tests-"))
os.environ["MIP_DATABASE_URL"] = f"sqlite:///{_WORKSPACE}/test.db"
os.environ["MIP_OBJECT_STORE"] = str(_WORKSPACE / "store")
os.environ["MIP_SEED_DEMO"] = "false"
os.environ["MIP_JWT_SECRET"] = "test-secret-at-least-32-bytes-long-for-hs256"
os.environ["MIP_RELEASE_SIGNING_SECRET"] = "test-release-secret-at-least-32-bytes-long"


@pytest.fixture(scope="session", autouse=True)
def _environment():
    from app.config import get_settings

    settings = get_settings()
    assert str(_WORKSPACE) in settings.database_url, "tests must never run against a real database"

    from app.db import create_all

    create_all()
    yield _WORKSPACE
    shutil.rmtree(_WORKSPACE, ignore_errors=True)


@pytest.fixture(scope="session")
def tenant_id(_environment) -> str:
    from app.seed.demo import seed

    return seed()


@pytest.fixture()
def db(tenant_id):
    from app.db import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def users(db, tenant_id):
    from sqlalchemy import select

    from app.models.identity import User

    return {
        user.email.split("@")[0]: user
        for user in db.execute(select(User).where(User.tenant_id == tenant_id)).scalars()
    }


@pytest.fixture()
def factory(db, tenant_id):
    from sqlalchemy import select

    from app.models.factory import FixtureVersion, MachineVersion, Material, PostProcessorVersion

    return {
        "machines": {m.code: m for m in db.execute(select(MachineVersion).where(MachineVersion.tenant_id == tenant_id)).scalars()},
        "materials": {m.code: m for m in db.execute(select(Material).where(Material.tenant_id == tenant_id)).scalars()},
        "fixtures": {f.code: f for f in db.execute(select(FixtureVersion).where(FixtureVersion.tenant_id == tenant_id)).scalars()},
        "posts": {p.code: p for p in db.execute(select(PostProcessorVersion).where(PostProcessorVersion.tenant_id == tenant_id)).scalars()},
    }


@pytest.fixture()
def make_project(db, tenant_id, users):
    from app.models.base import new_id
    from app.models.project import PartProject

    def factory_fn(part_number: str | None = None, **overrides):
        project = PartProject(
            tenant_id=tenant_id,
            part_number=part_number or f"TEST-{new_id()[:8]}",
            revision="A",
            name=overrides.pop("name", "Test part"),
            quantity=overrides.pop("quantity", 10),
            target_material_code="AL6082-T6",
            owner_id=users["engineer"].id,
            **overrides,
        )
        db.add(project)
        db.commit()
        db.refresh(project)
        return project

    return factory_fn


@pytest.fixture()
def bracket_model():
    """The benchmark bracket: 100 x 60 x 20 with a pocket and four holes."""
    from app.core.enums import Criticality, FeatureType, VerificationStatus
    from app.engines.partmodel import Feature, PartModel

    model = PartModel(
        units="mm",
        outline={"shape": "rect", "center": [0.0, 0.0], "size": [100.0, 60.0]},
        z_top=0.0,
        z_bottom=-20.0,
    )
    model.features.append(
        Feature(
            key="P1",
            feature_type=FeatureType.POCKET,
            label="P1 central pocket",
            params={"shape": "rect", "center": [0.0, 0.0], "size": [60.0, 40.0], "corner_radius": 6.0, "depth": 8.0, "top_z": 0.0},
            criticality=Criticality.QUALITY,
            status=VerificationStatus.CONFIRMED,
            confidence=0.97,
        )
    )
    for index, (x, y) in enumerate([(40, 20), (-40, 20), (-40, -20), (40, -20)], start=1):
        model.features.append(
            Feature(
                key=f"H{index}",
                feature_type=FeatureType.HOLE,
                label=f"H{index}",
                params={"center": [float(x), float(y)], "diameter": 8.2, "through": True, "top_z": 0.0},
                criticality=Criticality.FUNCTION,
                status=VerificationStatus.CONFIRMED,
                confidence=0.96,
            )
        )
    return model


@pytest.fixture()
def approved_geometry(db, make_project, bracket_model, users):
    """A project whose geometry has passed the geometry approval gate."""
    from app.core.enums import LifecycleStatus
    from app.services import geometry as geometry_service

    project = make_project()
    geometry = geometry_service.create_version(
        db,
        project=project,
        part_model=bracket_model,
        scale_established=True,
        scale_source="Verified CAD",
        scale_uncertainty_mm=0.01,
        quality_report={"source": "test fixture"},
        source_artifact_hashes=["f" * 64],
        provisional=False,
        actor_id=users["engineer"].id,
    )
    db.commit()
    geometry_service.approve(db, geometry, actor_id=users["engineer"].id, note="test approval")
    db.commit()
    assert geometry.status == LifecycleStatus.APPROVED
    return project, geometry


@pytest.fixture()
def simulated_plan(db, approved_geometry, factory, users):
    """A plan with generated toolpaths and a completed simulation."""
    from app.services import planning, verification

    project, geometry = approved_geometry
    plans = planning.generate_plans(
        db,
        project=project,
        geometry=geometry,
        material=factory["materials"]["AL6082-T6"],
        fixture=factory["fixtures"]["VISE-160"],
        machines=[factory["machines"]["VMC-01"]],
        objective="balanced",
        actor_id=users["manufacturing"].id,
    )
    db.commit()
    plan = plans[0]
    planning.generate_toolpaths(db, plan=plan, actor_id=users["manufacturing"].id)
    db.commit()
    run = verification.run_simulation(db, plan=plan, voxel_pitch=1.0, actor_id=users["manufacturing"].id)
    db.commit()
    return project, geometry, plan, run


@pytest.fixture()
def certified_post(db, factory, users):
    from app.services import postprocess

    post = factory["posts"]["FANUC-VMC01"]
    if not post.certified:
        postprocess.certify_post(
            db,
            post,
            machine=factory["machines"]["VMC-01"],
            actor_id=users["admin"].id,
            note="Certified for tests against the approved test programs",
            test_program_hashes=["a" * 64],
        )
        db.commit()
    return post
