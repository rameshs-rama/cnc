"""Factory digital twin master data: machines, tools, fixtures, materials, posts."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession, require, scoped
from app.core.enums import LifecycleStatus
from app.core.hashing import sha256_json
from app.core.rbac import Permission
from app.engines.post import build_post
from app.models.factory import (
    FixtureVersion,
    MachineVersion,
    Material,
    PostProcessorVersion,
    ToolAssemblyVersion,
)
from app.schemas.api import PostCertifyRequest, PostRevokeRequest
from app.schemas.common import ActionResult
from app.services import postprocess as postprocess_service

router = APIRouter(tags=["factory"])

#: Master-data models the generic version-bumping endpoints operate on.
_MODELS = {
    "machines": MachineVersion,
    "tools": ToolAssemblyVersion,
    "fixtures": FixtureVersion,
    "materials": Material,
    "posts": PostProcessorVersion,
}


def _serialise(record: Any) -> dict[str, Any]:
    payload = {
        column.name: getattr(record, column.name) for column in record.__table__.columns if column.name != "tenant_id"
    }
    return payload


@router.get("/factory/{collection}", summary="List master data")
def list_master_data(collection: str, db: DbSession, user: CurrentUser, include_superseded: bool = False) -> list[dict[str, Any]]:
    model = _MODELS.get(collection)
    if model is None:
        from app.core.errors import NotFound

        raise NotFound(f"Unknown master data collection {collection}")
    query = select(model).where(model.tenant_id == user.tenant_id)
    if not include_superseded and hasattr(model, "status"):
        query = query.where(model.status != LifecycleStatus.SUPERSEDED.value)
    return [_serialise(r) for r in db.execute(query).scalars()]


@router.get("/factory/{collection}/{record_id}", summary="Read one master data record")
def get_master_data(collection: str, record_id: str, db: DbSession, user: CurrentUser) -> dict[str, Any]:
    model = _MODELS.get(collection)
    if model is None:
        from app.core.errors import NotFound

        raise NotFound(f"Unknown master data collection {collection}")
    return _serialise(scoped(db, user, model, record_id, collection))


@router.post("/factory/{collection}", status_code=201, summary="Create a master data version")
def create_master_data(
    collection: str,
    payload: dict[str, Any],
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.MASTER_DATA_MANAGE))],
) -> dict[str, Any]:
    """Create a new version.

    Master data is versioned rather than edited in place so a historical plan
    keeps the exact configuration it was built against (FR-MCH-002).
    """
    from app.core.errors import NotFound

    model = _MODELS.get(collection)
    if model is None:
        raise NotFound(f"Unknown master data collection {collection}")

    fields = {c.name for c in model.__table__.columns}
    data = {k: v for k, v in payload.items() if k in fields and k not in ("id", "tenant_id", "created_at", "updated_at")}

    if "revision" in fields:
        code = data.get("code")
        latest = db.execute(
            select(func.coalesce(func.max(model.revision), 0)).where(model.tenant_id == user.tenant_id, model.code == code)
        ).scalar_one()
        data["revision"] = latest + 1
        if latest:
            for prior in db.execute(
                select(model).where(model.tenant_id == user.tenant_id, model.code == code, model.revision == latest)
            ).scalars():
                prior.status = LifecycleStatus.SUPERSEDED.value
                db.add(prior)

    record = model(tenant_id=user.tenant_id, **data)
    if hasattr(record, "content_hash"):
        record.content_hash = sha256_json(data)
    if isinstance(record, PostProcessorVersion):
        record.runtime_hash = build_post(record.definition).runtime_hash
    db.add(record)
    db.commit()
    db.refresh(record)
    return _serialise(record)


@router.post("/factory/posts/{post_id}:certify", summary="Certify a post for one machine and controller pair")
def certify_post(
    post_id: str,
    payload: PostCertifyRequest,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.POST_CERTIFY))],
) -> dict[str, Any]:
    post = scoped(db, user, PostProcessorVersion, post_id, "Post")
    machine = scoped(db, user, MachineVersion, payload.machine_version_id, "Machine")
    postprocess_service.certify_post(
        db,
        post,
        machine=machine,
        actor_id=user.id,
        note=payload.note,
        test_program_hashes=payload.test_program_hashes,
    )
    db.commit()
    db.refresh(post)
    return _serialise(post)


@router.post("/factory/posts/{post_id}:revoke", response_model=ActionResult, summary="Revoke a post")
def revoke_post(
    post_id: str,
    payload: PostRevokeRequest,
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.POST_CERTIFY))],
) -> ActionResult:
    """Block future releases; historic packages remain auditable (US-12)."""
    post = scoped(db, user, PostProcessorVersion, post_id, "Post")
    postprocess_service.revoke_post(db, post, actor_id=user.id, reason=payload.reason)
    db.commit()
    return ActionResult(
        message=f"Post {post.code} r{post.revision} revoked",
        detail={"reason": payload.reason, "historic_releases": "remain auditable and unchanged"},
    )


@router.post("/factory/machines/{machine_id}:qualify", summary="Mark machine geometry as qualified")
def qualify_machine(
    machine_id: str,
    payload: dict[str, Any],
    db: DbSession,
    user: Annotated[Any, Depends(require(Permission.MASTER_DATA_MANAGE))],
) -> dict[str, Any]:
    machine = scoped(db, user, MachineVersion, machine_id, "Machine")
    machine.geometry_qualified = True
    machine.machine_geometry = payload.get("machine_geometry", machine.machine_geometry)
    db.add(machine)
    db.commit()
    db.refresh(machine)
    return _serialise(machine)


@router.get("/factory/tools/{tool_id}/availability", summary="Tool availability and substitution penalty")
def tool_availability(tool_id: str, db: DbSession, user: CurrentUser) -> dict[str, Any]:
    """Quantify the penalty of substituting an unavailable tool (FR-TOL-003)."""
    tool = scoped(db, user, ToolAssemblyVersion, tool_id, "Tool")
    cutter = tool.cutter or {}
    diameter = float(cutter.get("diameter", 10.0))

    alternatives = []
    for other in db.execute(
        select(ToolAssemblyVersion).where(
            ToolAssemblyVersion.tenant_id == user.tenant_id,
            ToolAssemblyVersion.id != tool.id,
            ToolAssemblyVersion.status == LifecycleStatus.CURRENT.value,
            ToolAssemblyVersion.available.is_(True),
        )
    ).scalars():
        other_cutter = other.cutter or {}
        if other_cutter.get("type") != cutter.get("type"):
            continue
        other_diameter = float(other_cutter.get("diameter", 0.0))
        if other_diameter <= 0:
            continue
        # Removal rate scales with diameter for a fixed engagement ratio, so a
        # smaller substitute costs roughly the square of the diameter ratio in time.
        time_penalty = (diameter / other_diameter) ** 2 - 1.0
        alternatives.append(
            {
                "tool_id": other.id,
                "code": other.code,
                "diameter_mm": other_diameter,
                "flute_length_mm": other_cutter.get("flute_length"),
                "estimated_time_penalty_percent": round(max(time_penalty, 0.0) * 100, 1),
                "reaches_less_by_mm": round(
                    float(cutter.get("flute_length", 0)) - float(other_cutter.get("flute_length", 0)), 2
                ),
                "note": "Substituting a larger cutter may not fit the internal radius of every feature",
            }
        )
    alternatives.sort(key=lambda a: a["estimated_time_penalty_percent"])
    return {
        "tool_id": tool.id,
        "code": tool.code,
        "available": tool.available,
        "location": tool.location,
        "remaining_life_minutes": tool.remaining_life_minutes,
        "has_collision_model": tool.has_collision_model,
        "substitutes": alternatives[:6],
    }
