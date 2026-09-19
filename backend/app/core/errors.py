"""Domain errors mapped to stable HTTP responses.

Every error carries a machine-readable code so the web application can render a
specific recovery action rather than a generic failure (PRD 6.1).
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    status_code = 400
    code = "domain_error"

    def __init__(self, message: str, *, detail: Any = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail
        if code:
            self.code = code

    def to_payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class NotFound(DomainError):
    status_code = 404
    code = "not_found"


class PermissionDenied(DomainError):
    status_code = 403
    code = "permission_denied"


class Unauthorized(DomainError):
    status_code = 401
    code = "unauthorized"


class ConflictError(DomainError):
    status_code = 409
    code = "conflict"


class VersionConflict(ConflictError):
    code = "version_conflict"


class ValidationFailed(DomainError):
    status_code = 422
    code = "validation_failed"


class GateBlocked(DomainError):
    """A safety gate refused the action. Never downgrade this to a warning."""

    status_code = 409
    code = "gate_blocked"


class UnsupportedArtifact(DomainError):
    status_code = 415
    code = "unsupported_artifact"
