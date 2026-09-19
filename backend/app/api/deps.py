"""Request dependencies: identity, tenancy, authorization, idempotency.

Authorization is checked against the object being acted on, not only the route,
so a token for one tenant can never reach another tenant's project (PRD 9.1).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

import jwt
from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from app.core.errors import NotFound, PermissionDenied, Unauthorized
from app.core.hashing import sha256_json
from app.core.rbac import Permission, has_permission, permissions_for
from app.core.security import decode_access_token
from app.db import get_db
from app.models.identity import User
from app.models.platform import IdempotencyRecord
from app.models.project import PartProject

DbSession = Annotated[Session, Depends(get_db)]


def current_user(
    db: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("A bearer token is required")
    try:
        claims = decode_access_token(authorization.split(" ", 1)[1].strip())
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("The access token has expired", code="token_expired") from exc
    except jwt.PyJWTError as exc:
        raise Unauthorized("The access token could not be verified") from exc

    user = db.get(User, claims.get("sub", ""))
    if user is None or not user.is_active:
        raise Unauthorized("The token does not identify an active user")
    if claims.get("tenant") != user.tenant_id:
        raise Unauthorized("Token tenant does not match the user record", code="tenant_mismatch")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def require(*permissions: Permission) -> Callable[..., User]:
    """Route guard. Every listed permission must be held."""

    def dependency(user: CurrentUser) -> User:
        missing = [p.value for p in permissions if not has_permission(user.roles or [], p)]
        if missing:
            raise PermissionDenied(
                "Your roles do not grant this action",
                detail={"missing": missing, "held": sorted(p.value for p in permissions_for(user.roles or []))},
            )
        return user

    return dependency


def tenant_project(db: Session, user: User, project_id: str) -> PartProject:
    """Load a project inside the caller's tenant, or report it as absent.

    A project in another tenant is reported as not found rather than forbidden,
    so the API does not confirm that an identifier exists elsewhere.
    """
    project = db.get(PartProject, project_id)
    if project is None or project.tenant_id != user.tenant_id:
        raise NotFound(f"Project {project_id} was not found")
    return project


def scoped(db: Session, user: User, model: Any, object_id: str, label: str | None = None) -> Any:
    record = db.get(model, object_id)
    if record is None or getattr(record, "tenant_id", None) != user.tenant_id:
        raise NotFound(f"{label or model.__name__} {object_id} was not found")
    return record


def idempotent(
    db: Session,
    user: User,
    request: Request,
    key: str | None,
    payload: Any,
) -> tuple[IdempotencyRecord | None, str | None]:
    """Return a stored response for a repeated key, or the key to store under.

    A repeat with the same key and the same body replays the original response;
    a repeat with a different body is a conflict rather than a second mutation
    (PRD 9.1).
    """
    if not key:
        return None, None
    request_hash = sha256_json({"route": request.url.path, "payload": payload})
    existing = (
        db.query(IdempotencyRecord)
        .filter(IdempotencyRecord.tenant_id == user.tenant_id, IdempotencyRecord.key == key)
        .one_or_none()
    )
    if existing:
        if existing.request_hash != request_hash:
            raise PermissionDenied(
                "This idempotency key was used with a different request body",
                code="idempotency_key_reuse",
            )
        return existing, None
    return None, request_hash


def store_idempotent(
    db: Session, user: User, request: Request, key: str, request_hash: str, response: Any, status_code: int = 200
) -> None:
    db.add(
        IdempotencyRecord(
            tenant_id=user.tenant_id,
            key=key,
            route=request.url.path,
            request_hash=request_hash,
            response=response if isinstance(response, dict) else {"value": response},
            status_code=status_code,
        )
    )


def expect_version(record: Any, expected: int | None) -> None:
    """Optimistic concurrency check (PRD 9.1)."""
    from app.core.errors import VersionConflict

    if expected is None:
        return
    actual = getattr(record, "version", None)
    if actual is not None and actual != expected:
        raise VersionConflict(
            "The object changed since you loaded it",
            detail={"expected_version": expected, "current_version": actual},
        )
