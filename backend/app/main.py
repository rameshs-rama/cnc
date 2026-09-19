"""Application entry point.

Modular monolith for the transactional workflow, with the engineering engines
kept behind the job queue so compute can be scaled or isolated without
restructuring the API (PRD 7.2).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import auth, factory, geometry, planning, platform, production, projects, verification
from app.config import get_settings
from app.core.errors import DomainError
from app.db import create_all
from app.jobs.worker import Worker
from app.models.base import new_id

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("mip")

DESCRIPTION = """
AI CNC Manufacturing Intelligence Platform.

An engineering copilot, not a machine controller. Photo-derived geometry is
provisional, deterministic services perform every engineering calculation, and a
qualified engineer remains accountable for dimensions, workholding, tooling,
postprocessing and final release.

**Safety model**

* Every manufacturing attribute stores value, unit, source, confidence and verification status.
* Verified CAD and PMI outrank drawings, measurements, scans, calibrated photographs, ordinary photographs and AI inference.
* Geometry, kinematics, collision, cutting parameters and postprocessing are deterministic.
* Only a user with release authority may approve a validated machine and post pair.
* Missing critical data produces a stop or a verification task, never a silent invention.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    create_all()

    if settings.seed_demo:
        from app.seed.demo import seed_if_empty

        seed_if_empty()

    workers = [Worker(name=f"mip-worker-{i}") for i in range(max(1, settings.worker_threads))]
    for worker in workers:
        worker.start()
    logger.info("started %d compute worker(s)", len(workers))

    try:
        yield
    finally:
        for worker in workers:
            worker.stop()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="AI CNC Manufacturing Intelligence Platform",
        version="1.0.0",
        description=DESCRIPTION,
        lifespan=lifespan,
        openapi_tags=[
            {"name": "auth", "description": "Identity, roles and second factor"},
            {"name": "projects", "description": "Projects, evidence, capture and observations"},
            {"name": "geometry", "description": "Reconstruction, geometry versions and engineering review"},
            {"name": "factory", "description": "Machines, tools, fixtures, materials and postprocessors"},
            {"name": "planning", "description": "Process plans, toolpaths and optimisation"},
            {"name": "verification", "description": "Simulation, costing, postprocessing and release"},
            {"name": "production", "description": "Actual runs, inspection and governed learning"},
            {"name": "platform", "description": "Jobs, events and audit"},
        ],
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Trace-Id", "X-Package-Hash", "X-Program-Hash", "X-Controlled", "X-Content-Hash"],
    )

    @app.middleware("http")
    async def trace(request: Request, call_next):
        trace_id = request.headers.get("X-Trace-Id") or new_id()[:16]
        request.state.trace_id = trace_id
        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        return response

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError):
        payload = exc.to_payload()
        payload["trace_id"] = getattr(request.state, "trace_id", "")
        return JSONResponse(status_code=exc.status_code, content=payload)

    for module in (auth, projects, geometry, factory, planning, verification, production, platform):
        app.include_router(module.router, prefix=settings.api_prefix)

    @app.get("/health", tags=["platform"], summary="Liveness and readiness")
    def health() -> dict:
        return {"status": "ok", "version": app.version, "api_prefix": settings.api_prefix}

    @app.get("/", include_in_schema=False)
    def root() -> dict:
        return {
            "name": app.title,
            "version": app.version,
            "docs": "/docs",
            "openapi": "/openapi.json",
            "health": "/health",
        }

    return app


app = create_app()
