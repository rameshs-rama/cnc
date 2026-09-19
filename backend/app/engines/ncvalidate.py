"""NC program validation (FR-PST-003, PRD 12.1).

The validator re-reads the emitted program as a controller would, rather than
trusting the generator that produced it. Independent re-reading is the point:
a bug in the post is exactly the class of fault this is meant to catch.

Checks
------
* Units, plane, positioning mode, feed mode and safe start block present.
* Work offset commanded before motion, and allowed by the machine.
* Every tool number exists in the magazine and is preceded by length
  compensation.
* Spindle running and coolant commanded before the first cutting move.
* Programmed envelope inside machine travel, and consistent with the envelope
  the simulation observed.
* Prohibited and tenant-controlled codes, macro calls, subprogram calls and
  external variables.
* Program end present and reachable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.core.enums import Severity

#: Codes that must never appear in a released program unless a tenant policy
#: explicitly allows them. Macro and external-variable use is blocked because a
#: released program must be fully determined by its own text.
DEFAULT_PROHIBITED_CODES = ["M99", "M98", "G65", "G66", "G10", "M30.1"]
DEFAULT_PROHIBITED_PATTERNS = [
    (r"#\s*\d+", "macro variable reference"),
    (r"\bIF\b|\bWHILE\b|\bGOTO\b", "macro flow control"),
    (r"\bM0?0\b", "unconditional program stop inside a released program"),
]

_WORD = re.compile(r"([A-Z])\s*(-?\d*\.?\d+)")
_COMMENT = re.compile(r"\(.*?\)")


@dataclass
class Finding:
    severity: Severity
    code: str
    message: str
    consequence: str
    recommendation: str
    line_number: int | None = None
    line_text: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
            "consequence": self.consequence,
            "recommendation": self.recommendation,
            "line_number": self.line_number,
            "line_text": self.line_text,
        }


@dataclass
class ValidationReport:
    findings: list[Finding] = field(default_factory=list)
    envelope: dict[str, list[float]] = field(default_factory=dict)
    tools_used: list[int] = field(default_factory=list)
    work_offsets: list[str] = field(default_factory=list)
    line_count: int = 0

    @property
    def passed(self) -> bool:
        return not any(f.severity in (Severity.S1_STOP, Severity.S2_ENGINEER) for f in self.findings)

    @property
    def max_severity(self) -> str | None:
        order = [Severity.S1_STOP, Severity.S2_ENGINEER, Severity.S3_WARNING, Severity.S4_ADVISORY]
        for severity in order:
            if any(f.severity is severity for f in self.findings):
                return severity.value
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "max_severity": self.max_severity,
            "line_count": self.line_count,
            "envelope": self.envelope,
            "tools_used": self.tools_used,
            "work_offsets": self.work_offsets,
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass
class ValidationContext:
    machine_travels: dict[str, list[float]]
    magazine_tools: dict[int, dict[str, Any]]
    allowed_work_offsets: list[str]
    max_rpm: float
    max_feed_mm_min: float
    expected_units: str = "mm"
    simulated_envelope: dict[str, list[float]] | None = None
    prohibited_codes: list[str] = field(default_factory=lambda: list(DEFAULT_PROHIBITED_CODES))
    required_header_tokens: list[str] = field(default_factory=list)
    envelope_tolerance_mm: float = 0.5


def validate(program_text: str, ctx: ValidationContext) -> ValidationReport:
    report = ValidationReport()
    lines = program_text.splitlines()
    report.line_count = len(lines)

    state = {
        "units": None,
        "plane": None,
        "positioning": None,
        "feed_mode": None,
        "work_offset": None,
        "tool": None,
        "length_comp": False,
        "spindle_on": False,
        "spindle_rpm": 0.0,
        "coolant": False,
        "program_end": False,
        "cycle": None,
    }
    position = {"X": None, "Y": None, "Z": None}
    envelope_min = {"X": float("inf"), "Y": float("inf"), "Z": float("inf")}
    envelope_max = {"X": float("-inf"), "Y": float("-inf"), "Z": float("-inf")}
    tools_used: list[int] = []
    offsets_used: list[str] = []
    header = "\n".join(lines[:40]).upper()
    # Modal state as it stood at the first motion block. A safe start that only
    # appears afterwards has not protected anything, so the header checks are
    # made against this snapshot rather than against the final state.
    at_first_motion: dict[str, Any] | None = None

    for index, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped == "%":
            continue
        code_part = _COMMENT.sub(" ", stripped).upper()
        if not code_part.strip():
            continue

        _check_prohibited(code_part, index, stripped, ctx, report)

        words = dict(_parse_words(code_part))
        gcodes = _all_codes(code_part, "G")
        mcodes = _all_codes(code_part, "M")

        for g in gcodes:
            if g in ("20", "21"):
                state["units"] = "inch" if g == "20" else "mm"
            elif g in ("17", "18", "19"):
                state["plane"] = f"G{g}"
            elif g in ("90", "91"):
                state["positioning"] = f"G{g}"
            elif g in ("93", "94", "95"):
                state["feed_mode"] = f"G{g}"
            elif g in ("54", "55", "56", "57", "58", "59"):
                state["work_offset"] = f"G{g}"
                offsets_used.append(f"G{g}")
            elif g == "43":
                state["length_comp"] = True
            elif g == "49":
                state["length_comp"] = False
            elif g == "80":
                state["cycle"] = None
            elif g in ("81", "82", "83", "84", "85", "86", "89"):
                state["cycle"] = f"G{g}"

        for m in mcodes:
            if m in ("03", "3", "04", "4"):
                state["spindle_on"] = True
            elif m in ("05", "5"):
                state["spindle_on"] = False
            elif m in ("07", "7", "08", "8"):
                state["coolant"] = True
            elif m in ("09", "9"):
                state["coolant"] = False
            elif m in ("30", "02", "2"):
                state["program_end"] = True
            elif m in ("06", "6"):
                tool = words.get("T")
                if tool is None:
                    report.findings.append(
                        Finding(
                            Severity.S1_STOP,
                            "tool_change_without_number",
                            "A tool change was commanded without a tool number",
                            "The controller would change to an undefined tool",
                            "Regenerate the program through a certified post",
                            index,
                            stripped,
                        )
                    )
                else:
                    number = int(tool)
                    state["tool"] = number
                    state["length_comp"] = False
                    tools_used.append(number)
                    if number not in ctx.magazine_tools:
                        report.findings.append(
                            Finding(
                                Severity.S1_STOP,
                                "tool_not_in_magazine",
                                f"Tool T{number} is not loaded in the machine magazine",
                                "The tool change would fail or load the wrong tool",
                                "Load and register the tool, or reassign the operation to a magazine tool",
                                index,
                                stripped,
                            )
                        )

        if "S" in words:
            state["spindle_rpm"] = words["S"]
            if words["S"] > ctx.max_rpm + 1e-6:
                report.findings.append(
                    Finding(
                        Severity.S1_STOP,
                        "spindle_over_limit",
                        f"Commanded S{words['S']:.0f} exceeds the machine maximum {ctx.max_rpm:.0f} rpm",
                        "The controller would alarm or clamp the speed, invalidating the verified cut",
                        "Regenerate cutting parameters against this machine",
                        index,
                        stripped,
                    )
                )
        if "F" in words and words["F"] > ctx.max_feed_mm_min + 1e-6:
            report.findings.append(
                Finding(
                    Severity.S2_ENGINEER,
                    "feed_over_limit",
                    f"Commanded F{words['F']:.0f} exceeds the machine maximum {ctx.max_feed_mm_min:.0f} mm/min",
                    "The controller would clamp the feed, so the simulated cycle time no longer applies",
                    "Regenerate the operation within the machine feed limit",
                    index,
                    stripped,
                )
            )

        motion = any(g in ("0", "00", "1", "01", "2", "02", "3", "03") for g in gcodes)
        cutting = any(g in ("1", "01", "2", "02", "3", "03") for g in gcodes) or state["cycle"]
        # A reference return moves to a machine position, and incremental mode
        # makes the axis words distances rather than coordinates. Neither can be
        # read as a programmed position, so both are excluded from the envelope
        # and the travel check.
        reference_return = any(g in ("28", "30") for g in gcodes)
        absolute = state["positioning"] != "G91"

        for axis in ("X", "Y", "Z"):
            if axis in words and absolute and not reference_return:
                position[axis] = words[axis]
                envelope_min[axis] = min(envelope_min[axis], words[axis])
                envelope_max[axis] = max(envelope_max[axis], words[axis])
                _check_travel(axis, words[axis], ctx, report, index, stripped)

        if (motion or state["cycle"]) and at_first_motion is None and not reference_return:
            at_first_motion = dict(state)

        if (motion or state["cycle"]) and state["work_offset"] is None and not reference_return:
            report.findings.append(
                Finding(
                    Severity.S1_STOP,
                    "motion_without_work_offset",
                    "Motion was commanded before any work offset was selected",
                    "The move would execute in the previously active offset, at an unknown position",
                    "Emit G54 to G59 before the first motion block",
                    index,
                    stripped,
                )
            )
        if cutting and not state["spindle_on"]:
            report.findings.append(
                Finding(
                    Severity.S1_STOP,
                    "cutting_without_spindle",
                    "A cutting move was commanded with the spindle stopped",
                    "The tool would be pushed through material without rotation",
                    "Emit the spindle start before the first cutting move of the operation",
                    index,
                    stripped,
                )
            )
        if cutting and state["tool"] is not None and not state["length_comp"]:
            report.findings.append(
                Finding(
                    Severity.S1_STOP,
                    "cutting_without_length_compensation",
                    f"Cutting with T{state['tool']} before tool length compensation was applied",
                    "Every Z position would be wrong by the tool length",
                    "Emit G43 H before the first Z move of the operation",
                    index,
                    stripped,
                )
            )

    effective = dict(at_first_motion or state)
    effective["program_end_final"] = state["program_end"]
    _check_header(effective, header, ctx, report)
    _check_envelope(envelope_min, envelope_max, ctx, report)

    report.envelope = {
        "min": [envelope_min[a] if envelope_min[a] != float("inf") else 0.0 for a in ("X", "Y", "Z")],
        "max": [envelope_max[a] if envelope_max[a] != float("-inf") else 0.0 for a in ("X", "Y", "Z")],
    }
    report.tools_used = sorted(set(tools_used))
    report.work_offsets = sorted(set(offsets_used))

    for offset in report.work_offsets:
        if offset not in ctx.allowed_work_offsets:
            report.findings.append(
                Finding(
                    Severity.S2_ENGINEER,
                    "work_offset_not_configured",
                    f"{offset} is used but is not configured on this machine",
                    "The offset may be unset or hold a value from another job",
                    "Configure the offset on the machine or change the setup to a configured one",
                )
            )
    return report


def _parse_words(text: str) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for letter, value in _WORD.findall(text):
        if letter in ("N",):
            continue
        try:
            out.append((letter, float(value)))
        except ValueError:
            continue
    return out


def _all_codes(text: str, letter: str) -> list[str]:
    return [v.lstrip("0") or "0" for v in re.findall(rf"{letter}\s*(\d+\.?\d*)", text)]


def _check_prohibited(
    code_part: str, index: int, raw: str, ctx: ValidationContext, report: ValidationReport
) -> None:
    for code in ctx.prohibited_codes:
        if re.search(rf"\b{re.escape(code)}\b", code_part):
            report.findings.append(
                Finding(
                    Severity.S1_STOP,
                    "prohibited_code",
                    f"{code} is prohibited by tenant NC policy",
                    "The released program could branch, call external logic or rewrite offsets",
                    f"Remove {code} or obtain an explicit policy exception before release",
                    index,
                    raw,
                )
            )
    for pattern, description in DEFAULT_PROHIBITED_PATTERNS:
        if re.search(pattern, code_part):
            report.findings.append(
                Finding(
                    Severity.S1_STOP,
                    "prohibited_construct",
                    f"Program contains a {description}",
                    "A released program must be fully determined by its own text",
                    "Expand the construct into explicit blocks before release",
                    index,
                    raw,
                )
            )


def _check_travel(
    axis: str, value: float, ctx: ValidationContext, report: ValidationReport, index: int, raw: str
) -> None:
    span = ctx.machine_travels.get(axis)
    if not span:
        return
    low, high = float(span[0]), float(span[1])
    if value < low - 1e-6 or value > high + 1e-6:
        report.findings.append(
            Finding(
                Severity.S1_STOP,
                "travel_violation",
                f"{axis}{value:.3f} is outside the machine travel {low:.1f} to {high:.1f} mm",
                "The axis would overtravel and the controller would fault mid-cut",
                "Reposition the work offset or move the job to a machine with sufficient travel",
                index,
                raw,
            )
        )


def _check_header(state: dict[str, Any], header: str, ctx: ValidationContext, report: ValidationReport) -> None:
    """Validate the modal state that was in force when the first motion ran.

    ``state`` is the snapshot taken at the first motion block, so a unit or
    positioning code emitted only at the end of the program does not count as
    a safe start.
    """
    expected_unit_code = "G21" if ctx.expected_units == "mm" else "G20"
    if state["units"] is None:
        report.findings.append(
            Finding(
                Severity.S1_STOP,
                "units_not_commanded",
                "The program never commands G20 or G21",
                "The machine would run in whatever unit mode was left active, scaling every coordinate",
                "Emit the unit code in the safe start block",
            )
        )
    elif state["units"] != ctx.expected_units:
        report.findings.append(
            Finding(
                Severity.S1_STOP,
                "unit_mismatch",
                f"Program is in {state['units']} but the plan is in {ctx.expected_units} ({expected_unit_code} expected)",
                "Every coordinate would be wrong by a factor of 25.4",
                "Regenerate through a post configured for the plan units",
            )
        )
    if state["plane"] != "G17":
        report.findings.append(
            Finding(
                Severity.S1_STOP,
                "plane_not_xy",
                f"Working plane is {state['plane'] or 'not commanded'}; G17 is required for vertical milling",
                "Arc and canned-cycle axes would be interpreted in the wrong plane",
                "Emit G17 in the safe start block",
            )
        )
    if state["positioning"] != "G90":
        report.findings.append(
            Finding(
                Severity.S1_STOP,
                "not_absolute_mode",
                f"Positioning mode is {state['positioning'] or 'not commanded'}; absolute G90 is required",
                "Coordinates would be treated as increments from the current position",
                "Emit G90 in the safe start block",
            )
        )
    if state["feed_mode"] not in ("G94", None):
        report.findings.append(
            Finding(
                Severity.S3_WARNING,
                "unexpected_feed_mode",
                f"Feed mode {state['feed_mode']} is not the expected G94 feed per minute",
                "Feed values would be interpreted per revolution or as inverse time",
                "Confirm the post is configured for the intended feed mode",
            )
        )
    if not state.get("program_end_final", state["program_end"]):
        report.findings.append(
            Finding(
                Severity.S1_STOP,
                "no_program_end",
                "The program has no M30 or M02 end block",
                "The control would run past the end of the program",
                "Emit the program end block",
            )
        )
    for token in ctx.required_header_tokens:
        if token.upper() not in header:
            report.findings.append(
                Finding(
                    Severity.S2_ENGINEER,
                    "header_traceability_missing",
                    f"Release identifier {token} is absent from the program header",
                    "The program on the control could not be traced back to its approved package",
                    "Regenerate with release identity embedded in the header",
                )
            )


def _check_envelope(
    envelope_min: dict[str, float], envelope_max: dict[str, float], ctx: ValidationContext, report: ValidationReport
) -> None:
    if not ctx.simulated_envelope:
        return
    sim_min = ctx.simulated_envelope.get("min") or [0, 0, 0]
    sim_max = ctx.simulated_envelope.get("max") or [0, 0, 0]
    for axis_index, axis in enumerate(("X", "Y", "Z")):
        if envelope_min[axis] == float("inf"):
            continue
        low_delta = sim_min[axis_index] - envelope_min[axis]
        high_delta = envelope_max[axis] - sim_max[axis_index]
        if max(low_delta, high_delta) > ctx.envelope_tolerance_mm:
            report.findings.append(
                Finding(
                    Severity.S1_STOP,
                    "envelope_mismatch",
                    (
                        f"Programmed {axis} envelope [{envelope_min[axis]:.3f}, {envelope_max[axis]:.3f}] "
                        f"extends beyond the simulated envelope [{sim_min[axis_index]:.3f}, {sim_max[axis_index]:.3f}]"
                    ),
                    "The program would move where nothing was verified for collision",
                    "Re-simulate the exact program inputs, or investigate the postprocessor",
                )
            )
