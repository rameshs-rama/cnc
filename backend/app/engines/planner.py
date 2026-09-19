"""Process planning and machine feasibility (FR-PLN-001 to FR-PLN-003).

Planning is a constrained rule graph. Candidates are produced by deterministic
rules, then ranked; ranking can reorder feasible candidates but can never
introduce one that violates a machine, tool, geometry or workholding constraint
(PRD 10.2).

Infeasibility always names the binding constraint. "No feasible machine" without
a cause code is not an acceptable answer to a manufacturing engineer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from app.core.enums import FeatureSupport, FeatureType, OperationType
from app.engines import geom2d
from app.engines.partmodel import Feature, PartModel, feature_floor_z, feature_polygon

#: Order in which operation classes run within one setup. Roughing before
#: finishing, holes before the profile that could otherwise distort, chamfers
#: last because they depend on the finished edges.
OPERATION_ORDER = [
    OperationType.FACING,
    OperationType.ADAPTIVE_ROUGH,
    OperationType.POCKET,
    OperationType.SLOT,
    OperationType.REST_ROUGH,
    OperationType.DRILL,
    OperationType.BORE,
    OperationType.REAM,
    OperationType.TAP,
    OperationType.CONTOUR,
    OperationType.FINISH_3D,
    OperationType.CHAMFER,
]

#: A machine that cannot reach this fraction of a cutter's rated surface speed
#: is treated as infeasible rather than merely slow. Below roughly this point the
#: cutter runs so far outside its validated window that tool life and finish
#: stop being predictable, so planning against it would be dishonest. Above it,
#: a shortfall is reported as a quality advisory and the cutting engine clamps
#: the speed to what the machine can actually deliver.
MIN_SURFACE_SPEED_FRACTION = 0.4

AXIS_LABELS = {
    (0.0, 0.0, 1.0): "+Z",
    (0.0, 0.0, -1.0): "-Z",
    (1.0, 0.0, 0.0): "+X",
    (-1.0, 0.0, 0.0): "-X",
    (0.0, 1.0, 0.0): "+Y",
    (0.0, -1.0, 0.0): "-Y",
}

#: Part rotation, in degrees about X/Y/Z, that brings each access direction up.
AXIS_ORIENTATION = {
    "+Z": [0.0, 0.0, 0.0],
    "-Z": [180.0, 0.0, 0.0],
    "+X": [0.0, -90.0, 0.0],
    "-X": [0.0, 90.0, 0.0],
    "+Y": [90.0, 0.0, 0.0],
    "-Y": [-90.0, 0.0, 0.0],
}


@dataclass(slots=True)
class ToolCandidate:
    """A tool assembly as the planner sees it."""

    id: str
    code: str
    diameter: float
    flutes: int
    flute_length: float
    corner_radius: float
    tool_type: str
    material: str
    max_rpm: float
    gauge_length: float
    available: bool
    has_collision_model: bool
    cost_per_edge: float
    expected_life_minutes: float
    holder_diameter: float = 0.0
    shank_diameter: float = 0.0

    @property
    def radius(self) -> float:
        return self.diameter / 2.0


@dataclass(slots=True)
class MachineCandidate:
    id: str
    code: str
    name: str
    kinematics: str
    travels_mm: dict[str, list[float]]
    max_rpm: float
    min_rpm: float
    max_feed_mm_min: float
    magazine_capacity: int
    hourly_rate: float
    setup_rate: float
    tool_change_seconds: float
    index_seconds: float
    controller: str
    geometry_qualified: bool

    def travel_span(self, axis: str) -> float:
        span = self.travels_mm.get(axis)
        return float(span[1] - span[0]) if span else math.inf


@dataclass(slots=True)
class FixtureCandidate:
    id: str
    code: str
    name: str
    jaw_opening_mm: float
    max_part_height_mm: float
    clamp_height_mm: float
    safe_clearance_mm: float
    verified: bool
    solids: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PlannedOperation:
    sequence: int
    operation_type: str
    feature_keys: list[str]
    tool: ToolCandidate
    label: str
    z_start: float
    z_target: float
    geometry: dict[str, Any] = field(default_factory=dict)
    operation_class: str = "rough"
    rationale: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlannedSetup:
    sequence: int
    name: str
    access: str
    orientation_deg: list[float]
    work_offset: str
    clearance_plane_mm: float
    operations: list[PlannedOperation] = field(default_factory=list)
    setup_minutes: float = 15.0


@dataclass
class PlanCandidate:
    label: str
    setups: list[PlannedSetup]
    machine: MachineCandidate
    fixture: FixtureCandidate | None
    stock: dict[str, Any]
    unplanned: list[dict[str, Any]] = field(default_factory=list)
    decision_record: dict[str, Any] = field(default_factory=dict)

    @property
    def tool_count(self) -> int:
        return len({op.tool.id for s in self.setups for op in s.operations})

    @property
    def operation_count(self) -> int:
        return sum(len(s.operations) for s in self.setups)


@dataclass
class Feasibility:
    machine: MachineCandidate
    feasible: bool
    cause_code: str | None = None
    binding_constraint: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- stock
def choose_stock(model: PartModel, radial_allowance: float = 2.0, top_allowance: float = 1.5) -> dict[str, Any]:
    """Smallest block that contains the part plus a machining allowance."""
    outline = model.outline_polygon()
    x0, y0, x1, y1 = geom2d.bounds(outline)
    return {
        "type": "block",
        "min": [x0 - radial_allowance, y0 - radial_allowance, model.z_bottom],
        "max": [x1 + radial_allowance, y1 + radial_allowance, model.z_top + top_allowance],
        "allowance": {"radial_mm": radial_allowance, "top_mm": top_allowance},
        "rationale": "Part envelope plus radial and facing allowance",
    }


# --------------------------------------------------------------------- feasibility
def assess_machine(
    machine: MachineCandidate,
    *,
    model: PartModel,
    stock: dict[str, Any],
    access_directions: list[str],
    tool_count: int,
    fixture: FixtureCandidate | None,
    required_rpm: float,
) -> Feasibility:
    size = [stock["max"][i] - stock["min"][i] for i in range(3)]
    for index, axis in enumerate(("X", "Y", "Z")):
        span = machine.travel_span(axis)
        needed = size[index] + (60.0 if axis != "Z" else 80.0)  # approach and tool clearance
        if needed > span:
            return Feasibility(
                machine,
                False,
                cause_code=f"travel_{axis.lower()}_exceeded",
                binding_constraint=(
                    f"{axis} travel is {span:.0f} mm; the stock plus approach clearance needs {needed:.0f} mm"
                ),
                detail={"axis": axis, "available_mm": span, "required_mm": needed},
            )

    indexed = [a for a in access_directions if a not in ("+Z", "-Z")]
    if indexed and machine.kinematics == "3axis":
        return Feasibility(
            machine,
            False,
            cause_code="kinematics_insufficient",
            binding_constraint=(
                f"Features require access from {', '.join(indexed)}, which a 3-axis machine cannot index to"
            ),
            detail={"required_access": indexed, "kinematics": machine.kinematics},
        )

    if tool_count > machine.magazine_capacity:
        return Feasibility(
            machine,
            False,
            cause_code="magazine_capacity",
            binding_constraint=f"Plan needs {tool_count} tools; the magazine holds {machine.magazine_capacity}",
            detail={"required": tool_count, "capacity": machine.magazine_capacity},
        )

    speed_advisory: dict[str, Any] | None = None
    if required_rpm > 0:
        fraction = machine.max_rpm / required_rpm
        if fraction < MIN_SURFACE_SPEED_FRACTION:
            return Feasibility(
                machine,
                False,
                cause_code="spindle_speed_insufficient",
                binding_constraint=(
                    f"The smallest planned cutter needs {required_rpm:.0f} rpm for its rated surface speed; "
                    f"the spindle reaches {machine.max_rpm:.0f} rpm, which is {fraction:.0%} of it"
                ),
                detail={
                    "required_rpm": round(required_rpm),
                    "available_rpm": machine.max_rpm,
                    "fraction": round(fraction, 3),
                    "threshold": MIN_SURFACE_SPEED_FRACTION,
                },
            )
        if fraction < 1.0:
            speed_advisory = {
                "required_rpm": round(required_rpm),
                "available_rpm": machine.max_rpm,
                "fraction_of_rated_surface_speed": round(fraction, 3),
                "consequence": "Small cutters run below their rated surface speed; expect reduced tool life and finish",
            }

    if fixture:
        width = min(size[0], size[1])
        if width > fixture.jaw_opening_mm:
            return Feasibility(
                machine,
                False,
                cause_code="fixture_capacity",
                binding_constraint=(
                    f"Stock is {width:.1f} mm across the jaws; {fixture.code} opens to {fixture.jaw_opening_mm:.1f} mm"
                ),
                detail={"required_mm": width, "available_mm": fixture.jaw_opening_mm},
            )
        if size[2] > fixture.max_part_height_mm:
            return Feasibility(
                machine,
                False,
                cause_code="fixture_capacity",
                binding_constraint=(
                    f"Stock is {size[2]:.1f} mm tall; {fixture.code} supports {fixture.max_part_height_mm:.1f} mm"
                ),
                detail={"required_mm": size[2], "available_mm": fixture.max_part_height_mm},
            )

    detail: dict[str, Any] = {"kinematics": machine.kinematics, "tool_count": tool_count}
    if speed_advisory:
        detail["surface_speed_shortfall"] = speed_advisory
    if not machine.geometry_qualified:
        detail["warning"] = "Machine geometry has not been qualified; collision results carry lower confidence"
    return Feasibility(machine, True, detail=detail)


# ------------------------------------------------------------------ tool selection
def access_label(feature: Feature) -> str:
    key = tuple(round(v, 6) for v in feature.access)
    return AXIS_LABELS.get(key, "custom")


def _narrowest_internal_radius(feature: Feature) -> float:
    params = feature.params
    ftype = feature.feature_type
    if ftype in (FeatureType.HOLE, FeatureType.THREAD_CANDIDATE, FeatureType.COUNTERBORE, FeatureType.COUNTERSINK):
        return float(params.get("diameter", 0.0)) / 2.0
    if ftype == FeatureType.SLOT:
        return float(params.get("width", 0.0)) / 2.0
    if ftype in (FeatureType.POCKET, FeatureType.STEP):
        if params.get("shape") == "circle":
            return float(params.get("diameter", 0.0)) / 2.0
        radius = float(params.get("corner_radius", 0.0))
        size = params.get("size", [0.0, 0.0])
        half_min = min(float(size[0]), float(size[1])) / 2.0
        return max(0.01, radius) if radius > 0 else half_min
    return math.inf


def select_tool(
    feature: Feature,
    tools: list[ToolCandidate],
    *,
    depth: float,
    operation_class: str,
    prefer_largest: bool = True,
    available_only: bool = True,
    wanted_type: str = "endmill",
) -> tuple[ToolCandidate | None, dict[str, Any]]:
    """Largest tool that fits the feature and reaches the depth.

    Returns the tool plus the rationale: how many candidates were considered and
    why the rejected ones were rejected (PRD 10.4).
    """
    limit = _narrowest_internal_radius(feature)
    considered: list[dict[str, Any]] = []
    viable: list[ToolCandidate] = []

    for tool in tools:
        if tool.tool_type != wanted_type:
            continue
        reasons: list[str] = []
        if available_only and not tool.available:
            reasons.append("not available in the crib")
        if wanted_type == "endmill" and tool.radius > limit + 1e-6:
            reasons.append(f"radius {tool.radius:.2f} mm exceeds the {limit:.2f} mm internal radius")
        if depth > tool.flute_length + 1e-6:
            reasons.append(f"flute length {tool.flute_length:.1f} mm cannot reach {depth:.1f} mm")
        if depth + 5.0 > tool.gauge_length:
            reasons.append(f"gauge length {tool.gauge_length:.1f} mm leaves no holder clearance at {depth:.1f} mm")
        considered.append({"tool": tool.code, "rejected_because": reasons or None})
        if not reasons:
            viable.append(tool)

    if not viable:
        return None, {"considered": considered, "internal_radius_limit_mm": round(limit, 3) if limit != math.inf else None}

    viable.sort(key=lambda t: t.diameter, reverse=prefer_largest)
    chosen = viable[0]
    return chosen, {
        "selected": chosen.code,
        "selection_rule": "largest cutter that respects the internal radius and reaches the depth",
        "alternatives": [t.code for t in viable[1:4]],
        "considered": considered,
        "internal_radius_limit_mm": round(limit, 3) if limit != math.inf else None,
    }


def select_drill(diameter: float, tools: list[ToolCandidate], depth: float) -> tuple[ToolCandidate | None, dict[str, Any]]:
    """Exact-diameter drill, or the nearest smaller one for a bored hole."""
    drills = [t for t in tools if t.tool_type == "drill" and t.available]
    exact = [t for t in drills if abs(t.diameter - diameter) < 0.02 and t.flute_length >= depth]
    if exact:
        return exact[0], {"selected": exact[0].code, "selection_rule": "drill matching the nominal hole diameter"}
    smaller = sorted([t for t in drills if t.diameter < diameter and t.flute_length >= depth], key=lambda t: -t.diameter)
    if smaller:
        return smaller[0], {
            "selected": smaller[0].code,
            "selection_rule": "largest drill below nominal; the hole is opened by interpolation",
            "note": f"No {diameter:.2f} mm drill in the crib",
        }
    return None, {"considered": [t.code for t in drills], "note": f"No drill reaches {depth:.1f} mm at {diameter:.2f} mm"}


# ------------------------------------------------------------------------- planning
def plan_candidate(
    *,
    model: PartModel,
    features: list[Feature],
    machine: MachineCandidate,
    tools: list[ToolCandidate],
    fixture: FixtureCandidate | None,
    label: str,
    stock: dict[str, Any] | None = None,
    strategy: str = "balanced",
) -> PlanCandidate:
    """Build one complete candidate plan for a machine."""
    stock = stock or choose_stock(model)
    unplanned: list[dict[str, Any]] = []
    by_access: dict[str, list[Feature]] = {}

    for feature in features:
        if feature.support == FeatureSupport.MANUAL:
            unplanned.append(
                {
                    "feature": feature.key,
                    "type": feature.feature_type,
                    "reason": "Feature type requires manual planning in this release",
                }
            )
            continue
        by_access.setdefault(access_label(feature), []).append(feature)

    # Machine the largest face first; a flip setup only if features need it.
    ordering = sorted(by_access, key=lambda a: (a != "+Z", a))
    setups: list[PlannedSetup] = []
    clearance = float(stock["max"][2]) + 25.0

    for index, access in enumerate(ordering, start=1):
        setup = PlannedSetup(
            sequence=index,
            name=f"Setup {index} from {access}",
            access=access,
            orientation_deg=AXIS_ORIENTATION.get(access, [0.0, 0.0, 0.0]),
            work_offset=f"G5{3 + index}",
            clearance_plane_mm=clearance,
            setup_minutes=15.0 if index == 1 else 12.0,
        )
        _plan_setup_operations(
            setup=setup,
            model=model,
            features=by_access[access],
            tools=tools,
            stock=stock,
            unplanned=unplanned,
            strategy=strategy,
            first_setup=index == 1,
            fixture=fixture,
        )
        if setup.operations:
            setups.append(setup)

    return PlanCandidate(
        label=label,
        setups=setups,
        machine=machine,
        fixture=fixture,
        stock=stock,
        unplanned=unplanned,
        decision_record={
            "strategy": strategy,
            "access_directions": ordering,
            "stock_rule": stock.get("rationale", ""),
            "operation_order_rule": "Rough before finish; holes before profile; chamfers last",
            "machine": machine.code,
            "fixture": fixture.code if fixture else None,
        },
    )


def _plan_setup_operations(
    *,
    setup: PlannedSetup,
    model: PartModel,
    features: list[Feature],
    tools: list[ToolCandidate],
    stock: dict[str, Any],
    unplanned: list[dict[str, Any]],
    strategy: str,
    first_setup: bool,
    fixture: FixtureCandidate | None = None,
) -> None:
    stock_top = float(stock["max"][2])
    operations: list[PlannedOperation] = []

    # 1 - face the top down to the finished face.
    if first_setup and stock_top > model.z_top + 1e-6:
        face_tool, rationale = select_tool(
            Feature(key="__face__", feature_type=FeatureType.FACE, params={}),
            tools,
            depth=stock_top - model.z_top,
            operation_class="rough",
            wanted_type="facemill",
        )
        if face_tool is None:
            face_tool, rationale = select_tool(
                Feature(key="__face__", feature_type=FeatureType.FACE, params={}),
                tools,
                depth=stock_top - model.z_top,
                operation_class="rough",
            )
        if face_tool:
            operations.append(
                PlannedOperation(
                    sequence=0,
                    operation_type=OperationType.FACING,
                    feature_keys=[],
                    tool=face_tool,
                    label=f"Face top to Z{model.z_top:.2f}",
                    z_start=stock_top,
                    z_target=model.z_top,
                    geometry={"region": "stock_outline"},
                    operation_class="rough",
                    rationale={**rationale, "why": "Stock is above the finished top face"},
                )
            )

    # 2 - pockets, slots and steps.
    for feature in features:
        ftype = feature.feature_type
        floor = feature_floor_z(model, feature)
        top = float(feature.params.get("top_z", model.z_top))
        depth = top - floor

        if ftype in (FeatureType.POCKET, FeatureType.STEP):
            tool, rationale = select_tool(feature, tools, depth=depth, operation_class="rough")
            if tool is None:
                unplanned.append({"feature": feature.key, "type": ftype, "reason": "No tool fits the internal radius and reaches the depth", "detail": rationale})
                continue
            use_adaptive = strategy in ("min_time", "balanced") and depth > tool.diameter
            operations.append(
                PlannedOperation(
                    sequence=0,
                    operation_type=OperationType.ADAPTIVE_ROUGH if use_adaptive else OperationType.POCKET,
                    feature_keys=[feature.key],
                    tool=tool,
                    label=f"{'Adaptive rough' if use_adaptive else 'Pocket'} {feature.key}",
                    z_start=top,
                    z_target=floor,
                    geometry={"polygon": feature_polygon(feature)},
                    operation_class="rough",
                    rationale={**rationale, "why": f"{ftype} {depth:.2f} mm deep"},
                )
            )
            finish_tool, finish_rationale = select_tool(feature, tools, depth=depth, operation_class="finish")
            if finish_tool and (use_adaptive or finish_tool.id != tool.id):
                operations.append(
                    PlannedOperation(
                        sequence=0,
                        operation_type=OperationType.REST_ROUGH,
                        feature_keys=[feature.key],
                        tool=finish_tool,
                        label=f"Rest and wall finish {feature.key}",
                        z_start=top,
                        z_target=floor,
                        geometry={"polygon": feature_polygon(feature)},
                        operation_class="finish",
                        rationale={**finish_rationale, "why": "Remove the stock the roughing cutter could not reach"},
                    )
                )

        elif ftype == FeatureType.SLOT:
            tool, rationale = select_tool(feature, tools, depth=depth, operation_class="slot")
            if tool is None:
                unplanned.append({"feature": feature.key, "type": ftype, "reason": "No cutter narrower than the slot reaches the depth", "detail": rationale})
                continue
            operations.append(
                PlannedOperation(
                    sequence=0,
                    operation_type=OperationType.SLOT,
                    feature_keys=[feature.key],
                    tool=tool,
                    label=f"Slot {feature.key}",
                    z_start=top,
                    z_target=floor,
                    geometry={"start": feature.params["start"], "end": feature.params["end"], "width": feature.params["width"]},
                    operation_class="slot",
                    rationale=rationale,
                )
            )

    # 3 - holes, grouped by diameter so one tool does all of them.
    holes = [f for f in features if f.feature_type in (FeatureType.HOLE, FeatureType.THREAD_CANDIDATE, FeatureType.COUNTERBORE)]
    groups: dict[tuple[float, bool], list[Feature]] = {}
    for hole in holes:
        groups.setdefault((round(float(hole.params.get("diameter", 0.0)), 3), bool(hole.params.get("through"))), []).append(hole)

    for (diameter, through), group in sorted(groups.items()):
        reference = group[0]
        top = float(reference.params.get("top_z", model.z_top))
        floor = feature_floor_z(model, reference)
        depth = top - floor
        unresolved_thread = any(
            f.feature_type == FeatureType.THREAD_CANDIDATE and not f.thread_spec for f in group
        )

        drill, rationale = select_drill(diameter, tools, depth)
        if drill is None:
            mill, mill_rationale = select_tool(reference, tools, depth=depth, operation_class="rough")
            if mill is None:
                unplanned.append({"feature": reference.key, "type": reference.feature_type, "reason": f"No drill or cutter produces a {diameter:.2f} mm hole {depth:.1f} mm deep", "detail": mill_rationale})
                continue
            operations.append(
                PlannedOperation(
                    sequence=0,
                    operation_type=OperationType.BORE,
                    feature_keys=[f.key for f in group],
                    tool=mill,
                    label=f"Helical bore {diameter:.2f} mm x{len(group)}",
                    z_start=top,
                    z_target=floor,
                    geometry={"centers": [f.params["center"] for f in group], "diameter": diameter},
                    operation_class="finish",
                    rationale={**mill_rationale, "why": "No drill of this size is available, so the hole is interpolated"},
                )
            )
        else:
            operations.append(
                PlannedOperation(
                    sequence=0,
                    operation_type=OperationType.DRILL,
                    feature_keys=[f.key for f in group],
                    tool=drill,
                    label=f"Drill {drill.diameter:.2f} mm x{len(group)}{' through' if through else ''}",
                    z_start=top,
                    z_target=floor,
                    geometry={"centers": [f.params["center"] for f in group], "through": through, "diameter": drill.diameter},
                    operation_class="drill",
                    rationale=rationale,
                )
            )

        if unresolved_thread:
            unplanned.append(
                {
                    "feature": reference.key,
                    "type": FeatureType.THREAD_CANDIDATE,
                    "reason": "Thread pitch is not established, so tapping cannot be selected",
                    "blocking": True,
                }
            )

    # 4 - profile the outline on the first setup.
    if first_setup:
        outline = model.outline_polygon()
        profile_floor, profile_note = _profile_floor(model, stock, fixture)
        depth = model.z_top - profile_floor
        pseudo = Feature(key="__outline__", feature_type=FeatureType.POCKET, params={"shape": "rect", "size": [1e6, 1e6]})
        tool, rationale = select_tool(pseudo, tools, depth=depth, operation_class="finish")
        if tool:
            operations.append(
                PlannedOperation(
                    sequence=0,
                    operation_type=OperationType.CONTOUR,
                    feature_keys=["__outline__"],
                    tool=tool,
                    label="Profile outline",
                    z_start=model.z_top,
                    z_target=profile_floor,
                    geometry={"polygon": outline, "side": "outside"},
                    operation_class="finish",
                    rationale={**rationale, "why": "Separate the part from the stock envelope", "depth_limit": profile_note},
                )
            )
            if profile_floor > model.z_bottom + 1e-6:
                unplanned.append(
                    {
                        "feature": "__outline__",
                        "type": "Outline",
                        "reason": (
                            f"Profile stops at Z{profile_floor:.2f} to clear the workholding; "
                            f"{profile_floor - model.z_bottom:.2f} mm of web remains for a second setup or parting operation"
                        ),
                        "blocking": False,
                        "detail": {"profile_floor_z": profile_floor, "note": profile_note},
                    }
                )
        else:
            unplanned.append({"feature": "__outline__", "type": "Outline", "reason": "No cutter reaches the full part height for profiling", "detail": rationale})

    # 5 - chamfers last, once the edges exist.
    for feature in features:
        if feature.feature_type != FeatureType.CHAMFER:
            continue
        chamfer_tools = [t for t in tools if t.tool_type == "chamfer" and t.available]
        if not chamfer_tools:
            unplanned.append({"feature": feature.key, "type": FeatureType.CHAMFER, "reason": "No chamfer tool is available"})
            continue
        operations.append(
            PlannedOperation(
                sequence=0,
                operation_type=OperationType.CHAMFER,
                feature_keys=[feature.key],
                tool=chamfer_tools[0],
                label=f"Chamfer {feature.key}",
                z_start=model.z_top,
                z_target=model.z_top - float(feature.params.get("width", 0.5)),
                geometry={"on": feature.params.get("on", "outline"), "width": float(feature.params.get("width", 0.5))},
                operation_class="chamfer",
                rationale={"why": "Chamfers run after the edges they break have been created"},
            )
        )

    order = {t: i for i, t in enumerate(OPERATION_ORDER)}
    operations.sort(key=lambda op: (order.get(op.operation_type, 99), op.tool.code))
    for index, operation in enumerate(operations, start=1):
        operation.sequence = index
    setup.operations = operations


def _profile_floor(
    model: PartModel, stock: dict[str, Any], fixture: FixtureCandidate | None
) -> tuple[float, str]:
    """Lowest Z the profile may reach without cutting into the workholding.

    A vise grips the lower part of the stock, so profiling to the full part
    height would drive the cutter into the jaws. The planner stops above them
    and reports the remaining web rather than generating a path that the
    simulation would then have to reject.
    """
    floor = model.z_bottom
    if fixture is None:
        return floor, "No fixture assigned; profile depth not limited"
    jaw_top = float(stock["min"][2]) + fixture.clamp_height_mm
    safe = jaw_top + fixture.safe_clearance_mm
    if safe <= floor + 1e-6:
        return floor, f"{fixture.code} clamps below the part; full-depth profile is clear"
    return safe, (
        f"{fixture.code} grips {fixture.clamp_height_mm:.1f} mm of stock; profile stops "
        f"{fixture.safe_clearance_mm:.1f} mm above the jaws at Z{safe:.2f}"
    )


def required_rpm_for(tools: list[ToolCandidate], material_rules: dict[str, Any]) -> float:
    """Highest spindle speed any available cutter needs for its rated surface speed.

    Driven by the smallest cutter, since ``n = 1000 Vc / (pi D)`` rises as the
    diameter falls. Unavailable tools are ignored: a machine is not infeasible
    because of a cutter that is not in the crib.
    """
    best = 0.0
    for tool in tools:
        if not tool.available or tool.tool_type not in ("endmill", "chamfer"):
            continue
        rule = material_rules.get(f"{tool.material}/finish") or material_rules.get(f"{tool.material}/rough") or {}
        vc = float(rule.get("vc_m_min", 0.0))
        if vc and tool.diameter > 0:
            best = max(best, (1000.0 * vc) / (math.pi * tool.diameter))
    return best
