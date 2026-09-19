"""Cost and quotation model (FR-CST-001, FR-CST-002).

Every figure is traceable to a rate, a quantity and a measured time. Nothing is
a lump sum, because an estimator cannot defend a number they cannot decompose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Rates:
    machine_hourly: float = 65.0
    setup_hourly: float = 55.0
    labour_hourly: float = 42.0
    inspection_hourly: float = 48.0
    overhead_percent: float = 12.0
    margin_percent: float = 22.0
    scrap_percent: float = 2.5
    material_price_per_kg: float = 6.5
    material_density_g_cm3: float = 2.70
    tool_cost_per_edge: float = 18.0
    tool_life_minutes: float = 90.0
    programming_hours: float = 0.0
    programming_hourly: float = 68.0
    currency: str = "EUR"

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class CostInputs:
    cycle_time_seconds: float
    setup_minutes: float
    stock_volume_mm3: float
    cutting_minutes: float
    tool_changes: int
    quantity: int = 1
    inspection_minutes_per_part: float = 2.0
    load_unload_minutes: float = 1.5
    distinct_tools: int = 1


@dataclass
class CostResult:
    unit_cost: float
    unit_price: float
    breakdown: dict[str, Any]
    assumptions: dict[str, Any]
    quantity_breaks: list[dict[str, Any]] = field(default_factory=list)
    sensitivity: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_cost": round(self.unit_cost, 4),
            "unit_price": round(self.unit_price, 4),
            "breakdown": self.breakdown,
            "assumptions": self.assumptions,
            "quantity_breaks": self.quantity_breaks,
            "sensitivity": self.sensitivity,
        }


def estimate(inputs: CostInputs, rates: Rates, *, with_sensitivity: bool = True) -> CostResult:
    """Full unit cost and price.

    ``with_sensitivity`` is cleared for the internal sweep runs so a sensitivity
    row does not recompute its own sensitivity table.
    """
    quantity = max(1, inputs.quantity)

    stock_kg = inputs.stock_volume_mm3 / 1000.0 * rates.material_density_g_cm3 / 1000.0
    material = stock_kg * rates.material_price_per_kg

    cycle_hours = inputs.cycle_time_seconds / 3600.0
    machining = cycle_hours * rates.machine_hourly

    # Setup is paid once for the batch and spread across the parts.
    setup_total = inputs.setup_minutes / 60.0 * rates.setup_hourly
    setup_per_part = setup_total / quantity

    programming_total = rates.programming_hours * rates.programming_hourly
    programming_per_part = programming_total / quantity

    tooling = (inputs.cutting_minutes / max(rates.tool_life_minutes, 1e-6)) * rates.tool_cost_per_edge
    labour = (inputs.load_unload_minutes / 60.0) * rates.labour_hourly
    inspection = (inputs.inspection_minutes_per_part / 60.0) * rates.inspection_hourly

    direct = material + machining + setup_per_part + programming_per_part + tooling + labour + inspection
    scrap = direct * rates.scrap_percent / 100.0
    overhead = (direct + scrap) * rates.overhead_percent / 100.0
    unit_cost = direct + scrap + overhead
    unit_price = unit_cost * (1.0 + rates.margin_percent / 100.0)

    breakdown = {
        "material": round(material, 4),
        "machining": round(machining, 4),
        "setup": round(setup_per_part, 4),
        "programming": round(programming_per_part, 4),
        "tooling": round(tooling, 4),
        "labour": round(labour, 4),
        "inspection": round(inspection, 4),
        "scrap": round(scrap, 4),
        "overhead": round(overhead, 4),
        "unit_cost": round(unit_cost, 4),
        "margin": round(unit_price - unit_cost, 4),
        "unit_price": round(unit_price, 4),
        "batch_total": round(unit_price * quantity, 2),
    }

    assumptions = {
        "currency": rates.currency,
        "quantity": quantity,
        "cycle_time_seconds": round(inputs.cycle_time_seconds, 2),
        "setup_minutes": round(inputs.setup_minutes, 2),
        "setup_amortisation": f"Setup and programming divided across {quantity} parts",
        "stock_mass_kg": round(stock_kg, 4),
        "stock_volume_mm3": round(inputs.stock_volume_mm3, 1),
        "material_note": "Stock mass is the raw block; recovered swarf value is not credited",
        "tool_life_basis": f"{rates.tool_life_minutes:.0f} minutes of cutting per edge",
        "cutting_minutes": round(inputs.cutting_minutes, 2),
        "distinct_tools": inputs.distinct_tools,
        "rates": rates.to_dict(),
    }

    breaks = []
    for candidate_qty in sorted({1, 5, 10, 25, 50, 100, quantity}):
        spread = (setup_total + programming_total) / candidate_qty
        variable = material + machining + tooling + labour + inspection
        direct_q = variable + spread
        scrap_q = direct_q * rates.scrap_percent / 100.0
        overhead_q = (direct_q + scrap_q) * rates.overhead_percent / 100.0
        cost_q = direct_q + scrap_q + overhead_q
        breaks.append(
            {
                "quantity": candidate_qty,
                "unit_cost": round(cost_q, 4),
                "unit_price": round(cost_q * (1 + rates.margin_percent / 100.0), 4),
                "batch_total": round(cost_q * (1 + rates.margin_percent / 100.0) * candidate_qty, 2),
            }
        )

    sensitivity: dict[str, Any] = {}
    if with_sensitivity:
        sensitivity = {
            "cycle_time": _sweep(inputs, rates, "cycle", (-0.20, -0.10, 0.10, 0.20, 0.30)),
            "scrap_rate": _sweep(inputs, rates, "scrap", (-0.5, 0.5, 2.0, 5.0)),
            "machine_rate": _sweep(inputs, rates, "machine_rate", (-0.15, 0.15, 0.30)),
            "basis": "Each row re-runs the full model with one input changed",
        }

    return CostResult(unit_cost, unit_price, breakdown, assumptions, breaks, sensitivity)


def _sweep(inputs: CostInputs, rates: Rates, dimension: str, deltas: tuple[float, ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for delta in deltas:
        shifted_inputs = CostInputs(**inputs.__dict__)
        shifted_rates = Rates(**rates.to_dict())
        if dimension == "cycle":
            shifted_inputs.cycle_time_seconds *= 1 + delta
            shifted_inputs.cutting_minutes *= 1 + delta
            label = f"{delta:+.0%} cycle time"
        elif dimension == "scrap":
            shifted_rates.scrap_percent = max(0.0, rates.scrap_percent + delta)
            label = f"scrap {shifted_rates.scrap_percent:.1f}%"
        else:
            shifted_rates.machine_hourly *= 1 + delta
            label = f"{delta:+.0%} machine rate"
        result = estimate(shifted_inputs, shifted_rates, with_sensitivity=False)
        rows.append({"scenario": label, "unit_cost": round(result.unit_cost, 4), "unit_price": round(result.unit_price, 4)})
    return rows
