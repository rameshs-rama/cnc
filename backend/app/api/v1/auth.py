"""Authentication and second-factor enrolment."""

from __future__ import annotations

import urllib.parse

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.config import get_settings
from app.core.errors import Unauthorized
from app.core.rbac import permissions_for
from app.core.security import create_access_token, generate_totp_secret, verify_password
from app.models.identity import Tenant, User
from app.schemas.api import MfaEnrolResponse, TokenRequest, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/token", response_model=TokenResponse, summary="Exchange credentials for an access token")
def token(payload: TokenRequest, db: DbSession) -> TokenResponse:
    user = db.execute(select(User).where(User.email == payload.email.lower().strip())).scalars().first()
    # Constant work whether or not the user exists, so the endpoint does not
    # reveal which addresses are registered.
    stored = user.password_hash if user else "pbkdf2_sha256$240000$00$00"
    if not verify_password(payload.password, stored) or user is None or not user.is_active:
        raise Unauthorized("Email or password is incorrect")

    settings = get_settings()
    return TokenResponse(
        access_token=create_access_token(user.id, {"tenant": user.tenant_id, "roles": user.roles or []}),
        expires_in_minutes=settings.access_token_minutes,
        user_id=user.id,
        tenant_id=user.tenant_id,
        roles=list(user.roles or []),
        permissions=sorted(p.value for p in permissions_for(user.roles or [])),
        mfa_enabled=user.mfa_enabled,
    )


@router.get("/me", summary="Identity, roles and effective permissions")
def me(user: CurrentUser, db: DbSession) -> dict:
    tenant = db.get(Tenant, user.tenant_id)
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "roles": list(user.roles or []),
        "permissions": sorted(p.value for p in permissions_for(user.roles or [])),
        "mfa_enabled": user.mfa_enabled,
        "tenant": {
            "id": tenant.id,
            "name": tenant.name,
            "unit_system": tenant.unit_system,
            "currency": tenant.currency,
            "allow_self_approval": tenant.allow_self_approval,
            "critical_confidence_threshold": tenant.critical_confidence_threshold,
            "conflict_tolerance_mm": tenant.conflict_tolerance_mm,
        },
    }


@router.post("/mfa/enrol", response_model=MfaEnrolResponse, summary="Enrol a second factor for release approval")
def enrol_mfa(user: CurrentUser, db: DbSession) -> MfaEnrolResponse:
    secret = user.totp_secret or generate_totp_secret()
    user.totp_secret = secret
    user.mfa_enabled = True
    db.add(user)
    db.commit()
    label = urllib.parse.quote(f"MIP:{user.email}")
    return MfaEnrolResponse(
        secret=secret,
        otpauth_uri=f"otpauth://totp/{label}?secret={secret}&issuer=MIP&period=30&digits=6",
        note="Signing an NC release requires a current code from this factor.",
    )
