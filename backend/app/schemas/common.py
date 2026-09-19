"""Shared response envelope.

Every response carries the tenant, the object version, the lifecycle status and
a trace identifier, as the API principles require (PRD 9.1).
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class Meta(BaseModel):
    tenant_id: str
    trace_id: str = ""
    version: int | None = None
    status: str | None = None


class Envelope(BaseModel, Generic[T]):
    data: T
    meta: Meta


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int = 50
    offset: int = 0


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ActionResult(BaseModel):
    ok: bool = True
    message: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
