"""Postprocessor runtime (FR-PST-002).

The runtime consumes a declarative post definition - format specifications,
templates and capability flags - and never executes tenant-supplied code. A
compromised post can therefore change the shape of the output but cannot run
anything on the host (PRD 12, "Compromised post script").

Only a post that is enabled, certified for the exact machine and controller, and
not revoked may produce output. That check lives in the release service, but the
runtime refuses an obvious mismatch as well.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.hashing import sha256_json
from app.engines.ir import IROperation, IRSetup, ManufacturingIR

RUNTIME_VERSION = "1.0.0"


class PostError(RuntimeError):
    """The post definition cannot express something the IR requires."""


#: Baseline FANUC milling definition. A tenant post overrides these keys.
FANUC_DEFAULTS: dict[str, Any] = {
    "dialect": "fanuc",
    "units_code": {"mm": "G21", "inch": "G20"},
    "plane_code": "G17",
    "positioning_mode": "G90",
    "feed_mode": "G94",
    "cancel_codes": ["G40", "G49", "G80"],
    "rapid_code": "G00",
    "linear_code": "G01",
    "arc_cw_code": "G02",
    "arc_ccw_code": "G03",
    "spindle_cw": "M03",
    "spindle_ccw": "M04",
    "spindle_off": "M05",
    "tool_change": "M06",
    "coolant": {"flood": "M08", "mist": "M07", "through": "M08", "air": "M07", "off": "M09"},
    "length_comp": "G43",
    "cancel_length_comp": "G49",
    "canned_cycles": {"G81": True, "G82": True, "G83": True, "G84": True, "G85": True},
    "cycle_cancel": "G80",
    "return_mode": "G98",
    "program_end": "M30",
    "home_block": "G91 G28 Z0.",
    "decimals": {"xyz": 3, "feed": 1, "rpm": 0, "ijk": 3},
    "line_numbers": {"enabled": True, "start": 10, "increment": 10, "prefix": "N"},
    "comments": {"enabled": True, "open": "(", "close": ")", "upper": True},
    "max_line_length": 80,
    "program_number_format": "O{number}",
    "modal_output": True,
    "sequence_stop_between_setups": True,
}


@dataclass
class PostResult:
    text: str
    line_count: int
    warnings: list[str] = field(default_factory=list)
    envelope: dict[str, list[float]] = field(default_factory=dict)
    tool_numbers: list[int] = field(default_factory=list)
    work_offsets: list[str] = field(default_factory=list)


class FanucPost:
    """Deterministic IR to FANUC G-code transformation."""

    def __init__(self, definition: dict[str, Any] | None = None) -> None:
        self.definition = {**FANUC_DEFAULTS, **(definition or {})}
        self._lines: list[str] = []
        self._n = int(self.definition["line_numbers"]["start"])
        self._modal: dict[str, Any] = {}
        self._min = [float("inf")] * 3
        self._max = [float("-inf")] * 3
        self.warnings: list[str] = []

    # ------------------------------------------------------------------ utils
    @property
    def runtime_hash(self) -> str:
        return sha256_json({"runtime": RUNTIME_VERSION, "definition": self.definition})

    def _fmt(self, value: float, kind: str = "xyz") -> str:
        decimals = int(self.definition["decimals"].get(kind, 3))
        text = f"{value:.{decimals}f}"
        # FANUC accepts a trailing decimal point; keep it so a value is never
        # read as an integer in "no decimal point" increment mode.
        if "." in text:
            text = text.rstrip("0")
            if text.endswith("."):
                text += "0"
        return text

    def _emit(self, block: str, *, numbered: bool = True) -> None:
        block = block.strip()
        if not block:
            return
        config = self.definition["line_numbers"]
        if numbered and config.get("enabled", True):
            block = f"{config.get('prefix', 'N')}{self._n} {block}"
            self._n += int(config.get("increment", 10))
        limit = int(self.definition.get("max_line_length", 80))
        if len(block) > limit:
            self.warnings.append(f"Block exceeds {limit} characters and was emitted unwrapped: {block[:40]}...")
        self._lines.append(block)

    def _comment(self, text: str) -> None:
        cfg = self.definition["comments"]
        if not cfg.get("enabled", True):
            return
        body = text.upper() if cfg.get("upper", True) else text
        body = body.replace("(", "[").replace(")", "]")
        self._lines.append(f"{cfg.get('open', '(')}{body}{cfg.get('close', ')')}")

    def _track(self, x: float | None, y: float | None, z: float | None) -> None:
        for index, value in enumerate((x, y, z)):
            if value is None:
                continue
            self._min[index] = min(self._min[index], value)
            self._max[index] = max(self._max[index], value)

    def _axis_words(self, move: Any) -> str:
        words = []
        for axis, key in (("X", "x"), ("Y", "y"), ("Z", "z")):
            value = getattr(move, key)
            if value is None:
                continue
            if self.definition.get("modal_output", True) and self._modal.get(key) == value:
                continue
            words.append(f"{axis}{self._fmt(value)}")
            self._modal[key] = value
        return " ".join(words)

    # ------------------------------------------------------------------ build
    def run(self, ir: ManufacturingIR, *, program_number: str, release_header: dict[str, Any] | None = None) -> PostResult:
        if ir.units != "mm" and self.definition["units_code"].get(ir.units) is None:
            raise PostError(f"Post definition has no unit code for {ir.units}")
        if ir.machine.controller.upper() != "FANUC":
            raise PostError(
                f"This runtime emits FANUC syntax; the IR names controller {ir.machine.controller}. "
                "A machine-specific certified post is required for other controllers."
            )

        self._write_header(ir, program_number, release_header or {})

        tools: list[int] = []
        offsets: list[str] = []
        for index, setup in enumerate(ir.setups):
            if index > 0 and self.definition.get("sequence_stop_between_setups", True):
                self._comment(f"OPERATOR: REFIXTURE FOR {setup.name or f'SETUP {setup.sequence}'}")
                self._emit("M01")
            self._write_setup(ir, setup, tools, offsets)

        self._write_footer()

        text = "\n".join(self._lines) + "\n"
        return PostResult(
            text=text,
            line_count=len(self._lines),
            warnings=self.warnings,
            envelope={
                "min": [v if v != float("inf") else 0.0 for v in self._min],
                "max": [v if v != float("-inf") else 0.0 for v in self._max],
            },
            tool_numbers=sorted(set(tools)),
            work_offsets=sorted(set(offsets)),
        )

    def _write_header(self, ir: ManufacturingIR, program_number: str, release_header: dict[str, Any]) -> None:
        number = program_number if program_number.upper().startswith("O") else f"O{program_number}"
        self._lines.append(number)
        self._comment(f"PART {ir.provenance.part_number} REV {ir.provenance.revision}")
        self._comment(f"MACHINE {ir.machine.code} {ir.machine.model} CONTROLLER {ir.machine.controller} {ir.machine.controller_version}")
        self._comment(f"MATERIAL {ir.stock.material_name or ir.stock.material_code or 'UNSPECIFIED'}")
        # Release identity in the header so the program on the control can be
        # traced back to the exact approved package (PRD 12.1).
        for key in ("release_id", "release_revision", "post_version", "ir_hash", "plan_hash", "simulation_hash"):
            if release_header.get(key):
                self._comment(f"{key.replace('_', ' ').upper()} {release_header[key]}")
        self._comment(f"CYCLE TIME EST {ir.verification.get('cycle_time_seconds', 0):.0f} S")
        self._comment("UNVERIFIED COPY IF THIS HEADER IS ABSENT OR EDITED")

        for tool in ir.tools:
            self._comment(
                f"T{tool.number} {tool.code} D{tool.diameter_mm:.2f} "
                f"{'R' + format(tool.corner_radius_mm, '.2f') + ' ' if tool.corner_radius_mm else ''}"
                f"FL{tool.flute_length_mm:.1f} GL{tool.gauge_length_mm:.1f}"
            )

        units = self.definition["units_code"][ir.units]
        cancels = " ".join(self.definition["cancel_codes"])
        self._emit(
            f"{self.definition['plane_code']} {units} {cancels} "
            f"{self.definition['positioning_mode']} {self.definition['feed_mode']}"
        )
        self._emit(self.definition["home_block"])
        self._emit(self.definition["positioning_mode"])

    def _write_setup(self, ir: ManufacturingIR, setup: IRSetup, tools: list[int], offsets: list[str]) -> None:
        self._comment(f"SETUP {setup.sequence} {setup.name} WCS {setup.work_offset}")
        offsets.append(setup.work_offset)
        if setup.index_position:
            words = " ".join(f"{axis}{self._fmt(value)}" for axis, value in sorted(setup.index_position.items()))
            self._emit(f"{self.definition['rapid_code']} {words}")
            self._comment("INDEXED POSITION SET")

        current_tool: int | None = None
        for operation in setup.operations:
            self._modal.clear()
            tool = ir.tool_by_number(operation.tool_number)
            if tool is None:
                raise PostError(f"Operation {operation.id} references tool {operation.tool_number}, which is not in the IR tool list")
            tools.append(tool.number)

            self._comment(f"OP{operation.sequence} {operation.type} {operation.label}"[:70])
            if current_tool != tool.number:
                self._emit(f"T{tool.number} {self.definition['tool_change']}")
                current_tool = tool.number
            spindle = self.definition["spindle_cw"] if operation.spindle_direction == "cw" else self.definition["spindle_ccw"]
            self._emit(f"S{self._fmt(operation.spindle_rpm, 'rpm')} {spindle}")
            self._emit(f"{setup.work_offset} {self.definition['rapid_code']} X0. Y0.")
            self._emit(
                f"{self.definition['length_comp']} H{tool.length_offset or tool.number} "
                f"{self.definition['rapid_code']} Z{self._fmt(setup.clearance_plane_mm)}"
            )
            self._modal["z"] = setup.clearance_plane_mm
            coolant = self.definition["coolant"].get(operation.coolant)
            if coolant and operation.coolant != "off":
                self._emit(coolant)

            self._write_moves(operation, setup)

            self._emit(self.definition["coolant"]["off"])
            self._emit(self.definition["spindle_off"])
            self._emit(f"{self.definition['home_block']}")
            self._emit(self.definition["positioning_mode"])

    def _write_moves(self, operation: IROperation, setup: IRSetup) -> None:
        cycle_open = False
        for move in operation.moves:
            if move.t == "drill":
                cycle_open = self._write_drill(move, operation, cycle_open)
                continue
            if cycle_open:
                self._emit(self.definition["cycle_cancel"])
                cycle_open = False

            if move.t == "dwell":
                self._emit(f"G04 P{int((move.seconds or 0) * 1000)}")
                continue
            if move.t == "rapid":
                words = self._axis_words(move)
                self._track(move.x, move.y, move.z)
                if words:
                    self._emit(f"{self.definition['rapid_code']} {words}")
                continue
            if move.t in ("linear", "plunge"):
                words = self._axis_words(move)
                self._track(move.x, move.y, move.z)
                feed = move.f or (operation.plunge_feed_mm_min if move.t == "plunge" else operation.feed_mm_min)
                feed_word = ""
                if self._modal.get("f") != feed:
                    feed_word = f" F{self._fmt(feed, 'feed')}"
                    self._modal["f"] = feed
                if words or feed_word:
                    self._emit(f"{self.definition['linear_code']} {words}{feed_word}".strip())
                continue
            if move.t == "arc":
                code = self.definition["arc_cw_code"] if move.cw else self.definition["arc_ccw_code"]
                words = self._axis_words(move)
                self._track(move.x, move.y, move.z)
                arc = f"I{self._fmt(move.i or 0.0, 'ijk')} J{self._fmt(move.j or 0.0, 'ijk')}"
                self._emit(f"{code} {words} {arc} F{self._fmt(move.f or operation.feed_mm_min, 'feed')}")
                continue
            raise PostError(f"Post definition cannot express move type {move.t!r}")

        if cycle_open:
            self._emit(self.definition["cycle_cancel"])

    def _write_drill(self, move: Any, operation: IROperation, cycle_open: bool) -> bool:
        cycle = move.cycle or {}
        code = cycle.get("type", "G81")
        if not self.definition["canned_cycles"].get(code):
            raise PostError(
                f"Post definition does not enable canned cycle {code}. "
                "Expand the cycle into explicit moves or certify a post that supports it."
            )
        z_depth = float(cycle["z_depth"])
        r_plane = float(cycle["r_plane"])
        self._track(move.x, move.y, z_depth)

        if not cycle_open:
            words = [self.definition["return_mode"], code]
            words.append(f"X{self._fmt(move.x)}")
            words.append(f"Y{self._fmt(move.y)}")
            words.append(f"Z{self._fmt(z_depth)}")
            words.append(f"R{self._fmt(r_plane)}")
            if code == "G83" and cycle.get("peck"):
                words.append(f"Q{self._fmt(float(cycle['peck']))}")
            if code == "G82" and cycle.get("dwell_s"):
                words.append(f"P{int(float(cycle['dwell_s']) * 1000)}")
            if code == "G84" and cycle.get("pitch_mm"):
                words.append(f"F{self._fmt(float(cycle['pitch_mm']) * operation.spindle_rpm, 'feed')}")
            else:
                words.append(f"F{self._fmt(float(cycle.get('feed', operation.feed_mm_min)), 'feed')}")
            self._emit(" ".join(words))
            self._modal.update({"x": move.x, "y": move.y})
            return True

        self._emit(f"X{self._fmt(move.x)} Y{self._fmt(move.y)}")
        self._modal.update({"x": move.x, "y": move.y})
        return True

    def _write_footer(self) -> None:
        self._emit(self.definition["cancel_length_comp"])
        self._emit(self.definition["home_block"])
        self._emit("G91 G28 X0. Y0.")
        self._emit(self.definition["positioning_mode"])
        self._emit(self.definition["program_end"])
        self._lines.append("%")


def build_post(definition: dict[str, Any] | None) -> FanucPost:
    return FanucPost(definition)
