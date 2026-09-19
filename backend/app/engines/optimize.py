"""Multi-objective optimisation over feasible plans (FR-OPT-001, PRD 10.3).

The objective is a weighted, normalised combination of cycle time, setup effort,
machine cost, tool consumption, non-cutting travel, an energy proxy, quality
risk and collision margin. Weights change the ranking; they never relax a hard
constraint.

The search is a seeded simulated annealing over the decision variables the
planner leaves open. Every candidate is reproducible from its seed and the input
versions, which is what makes an optimisation result auditable.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: Objective presets exposed in the optimisation studio.
PRESETS: dict[str, dict[str, float]] = {
    "min_cycle_time": {
        "cycle_time": 0.55, "setup_effort": 0.10, "machine_cost": 0.10, "tool_consumption": 0.05,
        "non_cutting_travel": 0.10, "energy": 0.02, "quality_risk": 0.05, "collision_margin": 0.03,
    },
    "min_cost": {
        "cycle_time": 0.20, "setup_effort": 0.15, "machine_cost": 0.30, "tool_consumption": 0.20,
        "non_cutting_travel": 0.05, "energy": 0.05, "quality_risk": 0.03, "collision_margin": 0.02,
    },
    "min_tool_changes": {
        "cycle_time": 0.20, "setup_effort": 0.20, "machine_cost": 0.10, "tool_consumption": 0.35,
        "non_cutting_travel": 0.05, "energy": 0.02, "quality_risk": 0.05, "collision_margin": 0.03,
    },
    "balanced": {
        "cycle_time": 0.30, "setup_effort": 0.12, "machine_cost": 0.15, "tool_consumption": 0.12,
        "non_cutting_travel": 0.10, "energy": 0.05, "quality_risk": 0.10, "collision_margin": 0.06,
    },
    "max_quality": {
        "cycle_time": 0.10, "setup_effort": 0.08, "machine_cost": 0.07, "tool_consumption": 0.10,
        "non_cutting_travel": 0.05, "energy": 0.02, "quality_risk": 0.38, "collision_margin": 0.20,
    },
}

OBJECTIVE_TERMS = list(PRESETS["balanced"].keys())


@dataclass
class Metrics:
    """Raw, unnormalised measurements of one candidate."""

    cycle_time_s: float = 0.0
    setup_count: int = 1
    setup_minutes: float = 0.0
    machine_cost: float = 0.0
    tool_changes: int = 0
    tool_wear_minutes: float = 0.0
    non_cutting_mm: float = 0.0
    energy_kj: float = 0.0
    quality_risk: float = 0.0  # 0 best, 1 worst
    collision_margin_mm: float = 5.0  # larger is safer
    feasible: bool = True
    infeasible_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_time_s": round(self.cycle_time_s, 2),
            "setup_count": self.setup_count,
            "setup_minutes": round(self.setup_minutes, 2),
            "machine_cost": round(self.machine_cost, 4),
            "tool_changes": self.tool_changes,
            "tool_wear_minutes": round(self.tool_wear_minutes, 2),
            "non_cutting_mm": round(self.non_cutting_mm, 1),
            "energy_kj": round(self.energy_kj, 2),
            "quality_risk": round(self.quality_risk, 4),
            "collision_margin_mm": round(self.collision_margin_mm, 3),
            "feasible": self.feasible,
            "infeasible_reason": self.infeasible_reason,
        }


@dataclass
class Normaliser:
    """Maps each raw term onto 0..1 where 0 is best.

    Baselines come from the reference candidate, so a score is always relative
    to something a user can see rather than to an absolute scale nobody agreed.
    """

    cycle_time_s: float = 1.0
    setup_minutes: float = 1.0
    machine_cost: float = 1.0
    tool_wear_minutes: float = 1.0
    non_cutting_mm: float = 1.0
    energy_kj: float = 1.0
    collision_margin_reference_mm: float = 5.0

    @staticmethod
    def from_baseline(metrics: Metrics) -> Normaliser:
        return Normaliser(
            cycle_time_s=max(metrics.cycle_time_s, 1e-6),
            setup_minutes=max(metrics.setup_minutes, 1e-6),
            machine_cost=max(metrics.machine_cost, 1e-6),
            tool_wear_minutes=max(metrics.tool_wear_minutes, 1e-6),
            non_cutting_mm=max(metrics.non_cutting_mm, 1e-6),
            energy_kj=max(metrics.energy_kj, 1e-6),
            collision_margin_reference_mm=max(metrics.collision_margin_mm, 1e-6),
        )

    def terms(self, metrics: Metrics) -> dict[str, float]:
        margin_ratio = metrics.collision_margin_mm / self.collision_margin_reference_mm
        return {
            "cycle_time": metrics.cycle_time_s / self.cycle_time_s,
            "setup_effort": metrics.setup_minutes / self.setup_minutes,
            "machine_cost": metrics.machine_cost / self.machine_cost,
            "tool_consumption": metrics.tool_wear_minutes / self.tool_wear_minutes,
            "non_cutting_travel": metrics.non_cutting_mm / self.non_cutting_mm,
            "energy": metrics.energy_kj / self.energy_kj,
            "quality_risk": metrics.quality_risk,
            # Less margin than the baseline is worse; more is better, with
            # diminishing return so the optimiser does not chase huge clearances.
            "collision_margin": max(0.0, 1.0 - math.tanh(margin_ratio)),
        }


def score(metrics: Metrics, weights: dict[str, float], normaliser: Normaliser) -> tuple[float, dict[str, Any]]:
    if not metrics.feasible:
        return math.inf, {"infeasible": metrics.infeasible_reason}
    terms = normaliser.terms(metrics)
    total = 0.0
    contributions: dict[str, float] = {}
    for term, weight in weights.items():
        value = terms.get(term, 0.0)
        contribution = weight * value
        contributions[term] = round(contribution, 6)
        total += contribution
    return total, {
        "normalised_terms": {k: round(v, 6) for k, v in terms.items()},
        "weighted_contributions": contributions,
        "weights": weights,
        "total": round(total, 6),
    }


@dataclass
class Variable:
    """One decision the optimiser may change, with its hard bounds."""

    name: str
    values: list[Any]
    locked: bool = False
    description: str = ""


@dataclass
class Candidate:
    assignment: dict[str, Any]
    metrics: Metrics
    objective: float
    breakdown: dict[str, Any]
    iteration: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "assignment": self.assignment,
            "metrics": self.metrics.to_dict(),
            "objective": round(self.objective, 6) if math.isfinite(self.objective) else None,
            "breakdown": self.breakdown,
            "iteration": self.iteration,
        }


@dataclass
class SearchResult:
    baseline: Candidate
    best: Candidate
    candidates: list[Candidate] = field(default_factory=list)
    evaluations: int = 0
    seed: int = 0
    timed_out: bool = False
    weights: dict[str, float] = field(default_factory=dict)

    @property
    def improvement(self) -> dict[str, Any]:
        base, best = self.baseline.metrics, self.best.metrics

        def delta(a: float, b: float) -> float:
            return 0.0 if a <= 1e-9 else round((a - b) / a * 100.0, 2)

        return {
            "objective_percent": (
                round((self.baseline.objective - self.best.objective) / self.baseline.objective * 100.0, 2)
                if math.isfinite(self.baseline.objective) and self.baseline.objective > 0
                else 0.0
            ),
            "cycle_time_percent": delta(base.cycle_time_s, best.cycle_time_s),
            "non_cutting_percent": delta(base.non_cutting_mm, best.non_cutting_mm),
            "tool_changes_delta": best.tool_changes - base.tool_changes,
            "setup_delta": best.setup_count - base.setup_count,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "evaluations": self.evaluations,
            "timed_out": self.timed_out,
            "weights": self.weights,
            "baseline": self.baseline.to_dict(),
            "best": self.best.to_dict(),
            "improvement": self.improvement,
            "candidates": [c.to_dict() for c in self.candidates],
        }


def search(
    *,
    variables: list[Variable],
    evaluate: Callable[[dict[str, Any]], Metrics],
    weights: dict[str, float] | str = "balanced",
    seed: int = 20260919,
    budget: int = 120,
    keep_best: int = 8,
) -> SearchResult:
    """Seeded simulated annealing over the open decision variables.

    The same seed, variables and evaluator always produce the same result, which
    is the reproducibility requirement in PRD 11.
    """
    resolved = PRESETS[weights] if isinstance(weights, str) else _normalise_weights(weights)
    rng = random.Random(seed)

    baseline_assignment = {v.name: v.values[0] for v in variables}
    baseline_metrics = evaluate(baseline_assignment)
    normaliser = Normaliser.from_baseline(baseline_metrics)
    baseline_objective, baseline_breakdown = score(baseline_metrics, resolved, normaliser)
    baseline = Candidate(baseline_assignment, baseline_metrics, baseline_objective, baseline_breakdown, 0)

    mutable = [v for v in variables if not v.locked and len(v.values) > 1]
    if not mutable:
        return SearchResult(baseline, baseline, [baseline], 1, seed, False, resolved)

    current = baseline
    best = baseline
    seen: dict[tuple, Candidate] = {_key(baseline.assignment): baseline}
    evaluations = 1

    for iteration in range(1, budget + 1):
        temperature = max(0.02, 1.0 - iteration / budget)
        proposal = dict(current.assignment)
        for variable in rng.sample(mutable, k=min(len(mutable), rng.choice([1, 1, 2]))):
            proposal[variable.name] = rng.choice(variable.values)

        key = _key(proposal)
        if key in seen:
            continue

        metrics = evaluate(proposal)
        evaluations += 1
        objective, breakdown = score(metrics, resolved, normaliser)
        candidate = Candidate(proposal, metrics, objective, breakdown, iteration)
        seen[key] = candidate

        if not math.isfinite(objective):
            continue
        delta = objective - current.objective
        if delta < 0 or rng.random() < math.exp(-delta / max(temperature, 1e-6)):
            current = candidate
        if objective < best.objective:
            best = candidate

    ranked = sorted(
        (c for c in seen.values() if math.isfinite(c.objective)), key=lambda c: c.objective
    )[:keep_best]
    return SearchResult(baseline, best, ranked, evaluations, seed, False, resolved)


def _normalise_weights(weights: dict[str, float]) -> dict[str, float]:
    filtered = {k: max(0.0, float(v)) for k, v in weights.items() if k in OBJECTIVE_TERMS}
    for term in OBJECTIVE_TERMS:
        filtered.setdefault(term, 0.0)
    total = sum(filtered.values())
    if total <= 0:
        return dict(PRESETS["balanced"])
    return {k: round(v / total, 6) for k, v in filtered.items()}


def _key(assignment: dict[str, Any]) -> tuple:
    return tuple(sorted((k, _hashable(v)) for k, v in assignment.items()))


def _hashable(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_hashable(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    return value
