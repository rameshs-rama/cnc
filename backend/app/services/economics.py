"""Cost estimation bound to a specific plan and simulation (FR-CST-001/002)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import staleness
from app.core.errors import ValidationFailed
from app.engines import cost as cost_engine
from app.models.factory import MachineVersion, Material, ToolAssemblyVersion
from app.models.identity import Tenant
from app.models.planning import ManufacturingPlan, ToolpathVersion
from app.models.project import PartProject
from app.models.verification import CostEstimate, SimulationRun


def _tenant_currency(db: Session, tenant_id: str) -> str:
    tenant = db.get(Tenant, tenant_id)
    return tenant.currency if tenant else "EUR"


def estimate(
    db: Session,
    *,
    plan: ManufacturingPlan,
    simulation: SimulationRun,
    quantity: int | None = None,
    rate_overrides: dict[str, Any] | None = None,
    actor_id: str | None = None,
) -> CostEstimate:
    if simulation.plan_id != plan.id:
        raise ValidationFailed("The simulation does not belong to this plan", code="simulation_mismatch")

    project = db.get(PartProject, plan.project_id)
    machine = db.get(MachineVersion, plan.machine_version_id)
    material = db.get(Material, plan.material_id) if plan.material_id else None

    stock = plan.stock or {}
    volume = 1.0
    if stock.get("min") and stock.get("max"):
        volume = abs(
            (stock["max"][0] - stock["min"][0]) * (stock["max"][1] - stock["min"][1]) * (stock["max"][2] - stock["min"][2])
        )

    setup_minutes = sum(s.setup_minutes for s in plan.setups)
    cutting_minutes = float((simulation.time_breakdown or {}).get("cutting_s", 0.0)) / 60.0
    tool_ids = {o.tool_assembly_id for s in plan.setups for o in s.operations}
    tool_changes = sum(1 for _ in tool_ids)

    tools = [db.get(ToolAssemblyVersion, tid) for tid in tool_ids]
    tools = [t for t in tools if t]
    average_edge_cost = sum(t.cost_per_edge for t in tools) / len(tools) if tools else 18.0
    average_life = sum(t.expected_life_minutes for t in tools) / len(tools) if tools else 90.0

    rates = cost_engine.Rates(
        machine_hourly=machine.hourly_rate,
        setup_hourly=machine.setup_rate,
        material_price_per_kg=material.price_per_kg if material else 6.5,
        material_density_g_cm3=material.density_g_cm3 if material else 2.7,
        tool_cost_per_edge=average_edge_cost,
        tool_life_minutes=average_life,
        currency=_tenant_currency(db, plan.tenant_id),
    )
    for key, value in (rate_overrides or {}).items():
        if hasattr(rates, key) and isinstance(value, (int, float)):
            setattr(rates, key, float(value))

    inputs = cost_engine.CostInputs(
        cycle_time_seconds=simulation.cycle_time_seconds,
        setup_minutes=setup_minutes,
        stock_volume_mm3=volume,
        cutting_minutes=cutting_minutes,
        tool_changes=tool_changes,
        quantity=quantity or project.quantity,
        distinct_tools=len(tool_ids),
    )
    result = cost_engine.estimate(inputs, rates)

    record = CostEstimate(
        tenant_id=plan.tenant_id,
        project_id=plan.project_id,
        plan_id=plan.id,
        simulation_run_id=simulation.id,
        currency=rates.currency,
        quantity=inputs.quantity,
        rates=rates.to_dict(),
        assumptions=result.assumptions,
        breakdown=result.breakdown,
        unit_cost=result.unit_cost,
        unit_price=result.unit_price,
        quantity_breaks=result.quantity_breaks,
        sensitivity=result.sensitivity,
    )
    db.add(record)
    db.flush()

    for kind, object_id, object_hash in (
        ("plan", plan.id, plan.content_hash),
        ("simulation", simulation.id, simulation.content_hash),
    ):
        staleness.link(
            db,
            tenant_id=plan.tenant_id,
            downstream_kind="cost",
            downstream_id=record.id,
            upstream_kind=kind,
            upstream_id=object_id,
            upstream_hash=object_hash,
        )
    return record


def latest(db: Session, plan_id: str) -> CostEstimate | None:
    return db.execute(
        select(CostEstimate).where(CostEstimate.plan_id == plan_id).order_by(CostEstimate.created_at.desc()).limit(1)
    ).scalar_one_or_none()


def cutting_length(db: Session, plan_id: str) -> float:
    return float(
        sum(
            t.cutting_length_mm
            for t in db.execute(select(ToolpathVersion).where(ToolpathVersion.plan_id == plan_id)).scalars()
        )
    )
