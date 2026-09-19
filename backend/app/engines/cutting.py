"""Deterministic cutting parameter engine (FR-CAM-002).

Speeds and feeds are derived from the material rule set, the tool geometry and
the machine limits - never proposed by a model. Every returned value carries the
chain of rules and clamps that produced it so the CAM studio can explain it
(PRD 10.4).

Method
------
1.  Surface speed and feed per tooth come from the material rule for the
    cutter material and operation class.
2.  Spindle speed follows ``n = 1000 Vc / (pi D)``.
3.  Feed per tooth is corrected for radial chip thinning when the radial
    engagement is below half the cutter diameter.
4.  Cutting power is estimated with the Kienzle specific force model and
    checked against the interpolated spindle curve; depth of cut is reduced
    deterministically until the cut fits inside the available power.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

#: Operation classes the material rules are keyed on.
CLASS_ROUGH = "rough"
CLASS_FINISH = "finish"
CLASS_DRILL = "drill"
CLASS_SLOT = "slot"
CLASS_REAM = "ream"
CLASS_TAP = "tap"
CLASS_BORE = "bore"
CLASS_CHAMFER = "chamfer"


@dataclass(slots=True)
class ToolGeometry:
    diameter: float
    flutes: int
    flute_length: float
    corner_radius: float = 0.0
    material: str = "carbide"
    coating: str = "TiAlN"
    max_rpm: float = 12000.0
    tool_type: str = "endmill"
    point_angle: float = 140.0


@dataclass(slots=True)
class MachineLimits:
    max_rpm: float
    min_rpm: float
    max_feed_mm_min: float
    spindle_curve: list[list[float]] = field(default_factory=list)

    def power_at(self, rpm: float) -> float:
        """Interpolate available spindle power in kW at ``rpm``."""
        if not self.spindle_curve:
            return 15.0
        points = sorted(self.spindle_curve, key=lambda p: p[0])
        if rpm <= points[0][0]:
            return float(points[0][1])
        if rpm >= points[-1][0]:
            return float(points[-1][1])
        for (r0, p0, *_), (r1, p1, *_) in zip(points, points[1:], strict=False):
            if r0 <= rpm <= r1:
                span = r1 - r0
                t = 0.0 if span == 0 else (rpm - r0) / span
                return float(p0 + (p1 - p0) * t)
        return float(points[-1][1])


@dataclass(slots=True)
class MaterialRule:
    vc_m_min: float
    fz_mm: float
    ap_factor: float = 1.0
    ae_factor: float = 0.5
    kc11: float = 700.0
    mc: float = 0.25
    coolant: str = "flood"
    source: str = "tenant rule set"
    validated: bool = False


@dataclass(slots=True)
class CuttingParameters:
    rpm: float
    feed_mm_min: float
    fz_mm: float
    vc_m_min: float
    ap_mm: float
    ae_mm: float
    mrr_mm3_min: float
    power_kw: float
    coolant: str
    plunge_feed_mm_min: float
    rationale: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rpm": round(self.rpm, 1),
            "feed_mm_min": round(self.feed_mm_min, 1),
            "fz_mm": round(self.fz_mm, 5),
            "vc_m_min": round(self.vc_m_min, 2),
            "ap_mm": round(self.ap_mm, 4),
            "ae_mm": round(self.ae_mm, 4),
            "mrr_mm3_min": round(self.mrr_mm3_min, 2),
            "power_kw": round(self.power_kw, 3),
            "coolant": self.coolant,
            "plunge_feed_mm_min": round(self.plunge_feed_mm_min, 1),
        }


def rule_for(material_rules: dict[str, Any], tool: ToolGeometry, operation_class: str) -> MaterialRule:
    """Select the material rule, falling back from specific to generic keys."""
    keys = [
        f"{tool.material}/{operation_class}",
        f"{tool.material}/default",
        f"default/{operation_class}",
        "default/default",
    ]
    for key in keys:
        raw = material_rules.get(key)
        if raw:
            return MaterialRule(
                vc_m_min=float(raw["vc_m_min"]),
                fz_mm=float(raw["fz_mm"]),
                ap_factor=float(raw.get("ap_factor", 1.0)),
                ae_factor=float(raw.get("ae_factor", 0.5)),
                kc11=float(raw.get("kc11", 700.0)),
                mc=float(raw.get("mc", 0.25)),
                coolant=raw.get("coolant", "flood"),
                source=raw.get("source", f"material rule {key}"),
                validated=bool(raw.get("validated", False)),
            )
    raise KeyError(
        f"No cutting rule for {tool.material}/{operation_class}. "
        "Material rules must be entered and validated by an engineer before planning."
    )


def chip_thinning_factor(diameter: float, ae: float) -> float:
    """Radial chip thinning correction.

    Below half-diameter engagement the average chip is thinner than the
    programmed feed per tooth, so feed is increased by the inverse of the
    engagement ratio to keep the real chip load constant.
    """
    if ae <= 0 or diameter <= 0:
        return 1.0
    ratio = min(ae / diameter, 0.5)
    if ratio >= 0.5:
        return 1.0
    sin_engagement = math.sqrt(max(1e-9, 1.0 - (1.0 - 2.0 * ratio) ** 2))
    return min(2.0, 1.0 / sin_engagement)


def specific_cutting_force(kc11: float, mc: float, chip_thickness: float) -> float:
    """Kienzle model: kc = kc1.1 * h^-mc, in N/mm^2."""
    h = max(chip_thickness, 0.01)
    return kc11 * (h ** (-mc))


def milling_power_kw(mrr_mm3_min: float, kc: float, efficiency: float = 0.85) -> float:
    """Spindle power for a given removal rate, in kW."""
    return (mrr_mm3_min * kc) / (60_000_000.0 * efficiency)


def compute(
    *,
    tool: ToolGeometry,
    machine: MachineLimits,
    material_rules: dict[str, Any],
    operation_class: str,
    ap_mm: float | None = None,
    ae_mm: float | None = None,
    depth_available_mm: float | None = None,
) -> CuttingParameters:
    """Derive a safe, explainable parameter set for one operation."""
    rule = rule_for(material_rules, tool, operation_class)
    notes: list[str] = [f"Base rule: {rule.source} (Vc {rule.vc_m_min} m/min, fz {rule.fz_mm} mm/tooth)"]
    clamps: list[str] = []

    diameter = tool.diameter
    ae = ae_mm if ae_mm is not None else rule.ae_factor * diameter
    ap = ap_mm if ap_mm is not None else rule.ap_factor * diameter
    if depth_available_mm is not None:
        ap = min(ap, depth_available_mm)
    ap = min(ap, tool.flute_length)
    if ap < (ap_mm or ap):
        clamps.append("Depth of cut limited by usable flute length")
    ae = min(ae, diameter)

    # --- spindle speed ----------------------------------------------------
    rpm_ideal = (1000.0 * rule.vc_m_min) / (math.pi * diameter) if diameter > 0 else machine.max_rpm
    rpm = rpm_ideal
    rpm_ceiling = min(machine.max_rpm, tool.max_rpm)
    if rpm > rpm_ceiling:
        rpm = rpm_ceiling
        clamps.append(
            f"Spindle speed clamped to {rpm:.0f} rpm by "
            f"{'tool' if tool.max_rpm < machine.max_rpm else 'machine'} maximum"
        )
    if rpm < machine.min_rpm:
        rpm = machine.min_rpm
        clamps.append(f"Spindle speed raised to machine minimum {rpm:.0f} rpm")
    vc_actual = math.pi * diameter * rpm / 1000.0

    # --- feed -------------------------------------------------------------
    if operation_class in (CLASS_DRILL, CLASS_REAM, CLASS_BORE, CLASS_TAP):
        fz = rule.fz_mm
        thinning = 1.0
        ae = diameter
    else:
        thinning = chip_thinning_factor(diameter, ae)
        fz = rule.fz_mm * thinning
        if thinning > 1.0:
            notes.append(f"Chip thinning correction x{thinning:.2f} at {ae:.2f} mm radial engagement")

    feed = rpm * tool.flutes * fz
    if feed > machine.max_feed_mm_min:
        feed = machine.max_feed_mm_min
        fz = feed / max(1e-6, rpm * tool.flutes)
        clamps.append(f"Feed clamped to machine maximum {machine.max_feed_mm_min:.0f} mm/min")

    # --- power check, with deterministic depth back-off -------------------
    available_kw = machine.power_at(rpm)
    for _ in range(24):
        mrr = ae * ap * feed
        chip = fz * math.sqrt(max(1e-9, min(1.0, ae / max(diameter, 1e-9))))
        kc = specific_cutting_force(rule.kc11, rule.mc, chip)
        power = milling_power_kw(mrr, kc)
        if power <= available_kw or ap <= 0.2:
            break
        ap *= 0.85
        clamps.append(f"Depth of cut reduced to {ap:.2f} mm to stay inside {available_kw:.1f} kW spindle power")
    mrr = ae * ap * feed
    chip = fz * math.sqrt(max(1e-9, min(1.0, ae / max(diameter, 1e-9))))
    power = milling_power_kw(mrr, specific_cutting_force(rule.kc11, rule.mc, chip), 0.85)

    plunge = feed * (0.3 if operation_class not in (CLASS_DRILL, CLASS_REAM, CLASS_BORE, CLASS_TAP) else 1.0)

    return CuttingParameters(
        rpm=rpm,
        feed_mm_min=feed,
        fz_mm=fz,
        vc_m_min=vc_actual,
        ap_mm=ap,
        ae_mm=ae,
        mrr_mm3_min=mrr,
        power_kw=power,
        coolant=rule.coolant,
        plunge_feed_mm_min=plunge,
        rationale={
            "operation_class": operation_class,
            "rule_source": rule.source,
            "rule_validated": rule.validated,
            "target_vc_m_min": rule.vc_m_min,
            "achieved_vc_m_min": round(vc_actual, 2),
            "ideal_rpm": round(rpm_ideal, 1),
            "available_power_kw": round(available_kw, 2),
            "chip_thinning_factor": round(thinning, 3),
            "notes": notes,
            "clamps": clamps,
            "model": "Kienzle specific cutting force, kc = kc1.1 * h^-mc",
        },
    )


def tapping_parameters(tool: ToolGeometry, machine: MachineLimits, pitch_mm: float, vc_m_min: float = 12.0) -> CuttingParameters:
    """Synchronous tapping: feed is locked to pitch times spindle speed."""
    rpm = min(machine.max_rpm, tool.max_rpm, (1000.0 * vc_m_min) / (math.pi * max(tool.diameter, 1e-6)))
    rpm = max(rpm, machine.min_rpm)
    feed = rpm * pitch_mm
    clamps = []
    if feed > machine.max_feed_mm_min:
        feed = machine.max_feed_mm_min
        rpm = feed / pitch_mm
        clamps.append("Tapping speed reduced so feed stays inside the machine maximum")
    return CuttingParameters(
        rpm=rpm,
        feed_mm_min=feed,
        fz_mm=pitch_mm,
        vc_m_min=math.pi * tool.diameter * rpm / 1000.0,
        ap_mm=0.0,
        ae_mm=tool.diameter,
        mrr_mm3_min=0.0,
        power_kw=0.0,
        coolant="flood",
        plunge_feed_mm_min=feed,
        rationale={
            "operation_class": CLASS_TAP,
            "rule_source": "synchronous tapping, feed = pitch x rpm",
            "pitch_mm": pitch_mm,
            "clamps": clamps,
            "notes": ["Feed is rigidly bound to pitch; it is never adjusted independently"],
        },
    )
