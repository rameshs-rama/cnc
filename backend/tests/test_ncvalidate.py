"""NC validation tests (PRD 12.1).

The validator must catch faults the generator could introduce, so each test
feeds it a program with one specific defect.
"""

from __future__ import annotations

import pytest

from app.core.enums import Severity
from app.engines.ncvalidate import ValidationContext, validate

SAFE_PROGRAM = """O1000
(PART TEST REV A)
(RELEASE ID REL-9999)
N10 G17 G21 G40 G49 G80 G90 G94
N20 G91 G28 Z0.
N30 G90
N40 T1 M06
N50 S8000 M03
N60 G54 G00 X0. Y0.
N70 G43 H1 G00 Z25.0
N80 M08
N90 G01 Z-2.0 F400.0
N100 G01 X40.0 Y20.0 F1200.0
N110 G00 Z25.0
N120 M09
N130 M05
N140 G49
N150 G91 G28 Z0.
N160 G90
N170 M30
%
"""


@pytest.fixture()
def context():
    return ValidationContext(
        machine_travels={"X": [-150, 150], "Y": [-100, 100], "Z": [-160, 40]},
        magazine_tools={1: {"code": "T01"}},
        allowed_work_offsets=["G54", "G55"],
        max_rpm=12000.0,
        max_feed_mm_min=15000.0,
        expected_units="mm",
        required_header_tokens=["REL-9999"],
    )


def codes(report) -> set[str]:
    return {f.code for f in report.findings}


def test_a_correct_program_passes(context):
    report = validate(SAFE_PROGRAM, context)
    assert report.passed is True, [f.message for f in report.findings]
    assert report.tools_used == [1]
    assert report.work_offsets == ["G54"]
    assert report.envelope["max"][0] == pytest.approx(40.0)


def test_inch_program_against_a_metric_plan_is_a_stop(context):
    report = validate(SAFE_PROGRAM.replace("G21", "G20"), context)
    assert "unit_mismatch" in codes(report)
    finding = next(f for f in report.findings if f.code == "unit_mismatch")
    assert finding.severity is Severity.S1_STOP
    assert "25.4" in finding.consequence


def test_incremental_mode_left_active_before_motion_is_a_stop(context):
    """The real hazard is G91 surviving a reference return into the first move."""
    program = SAFE_PROGRAM.replace("N10 G17 G21 G40 G49 G80 G90 G94", "N10 G17 G21 G40 G49 G80 G94").replace(
        "N30 G90", "N30 M01"
    )
    report = validate(program, context)
    assert "not_absolute_mode" in codes(report)
    finding = next(f for f in report.findings if f.code == "not_absolute_mode")
    assert "increments" in finding.consequence


def test_a_safe_start_that_only_appears_after_motion_does_not_count(context):
    """G21 emitted at the end of the program has protected nothing."""
    program = SAFE_PROGRAM.replace("N10 G17 G21 G40 G49 G80 G90 G94", "N10 G17 G40 G49 G80 G90 G94").replace(
        "N160 G90", "N160 G90 G21"
    )
    report = validate(program, context)
    assert "units_not_commanded" in codes(report)


def test_motion_before_a_work_offset_is_a_stop(context):
    program = SAFE_PROGRAM.replace("N60 G54 G00 X0. Y0.", "N60 G00 X0. Y0.")
    report = validate(program, context)
    assert "motion_without_work_offset" in codes(report)


def test_cutting_without_length_compensation_is_a_stop(context):
    program = SAFE_PROGRAM.replace("N70 G43 H1 G00 Z25.0", "N70 G00 Z25.0")
    report = validate(program, context)
    assert "cutting_without_length_compensation" in codes(report)


def test_cutting_with_the_spindle_stopped_is_a_stop(context):
    program = SAFE_PROGRAM.replace("N50 S8000 M03", "N50 S8000")
    report = validate(program, context)
    assert "cutting_without_spindle" in codes(report)


def test_a_tool_not_in_the_magazine_is_a_stop(context):
    program = SAFE_PROGRAM.replace("N40 T1 M06", "N40 T7 M06").replace("H1", "H7")
    report = validate(program, context)
    assert "tool_not_in_magazine" in codes(report)


def test_travel_beyond_the_envelope_is_a_stop(context):
    program = SAFE_PROGRAM.replace("N100 G01 X40.0 Y20.0 F1200.0", "N100 G01 X400.0 Y20.0 F1200.0")
    report = validate(program, context)
    assert "travel_violation" in codes(report)


def test_overspeed_is_a_stop(context):
    report = validate(SAFE_PROGRAM.replace("S8000", "S18000"), context)
    assert "spindle_over_limit" in codes(report)


def test_macro_and_subprogram_constructs_are_prohibited(context):
    report = validate(SAFE_PROGRAM.replace("N100 G01 X40.0 Y20.0 F1200.0", "N100 #100 = 5.0"), context)
    assert "prohibited_construct" in codes(report)

    report = validate(SAFE_PROGRAM.replace("N110 G00 Z25.0", "N110 M98 P1234"), context)
    assert "prohibited_code" in codes(report)


def test_missing_program_end_is_a_stop(context):
    report = validate(SAFE_PROGRAM.replace("N170 M30", "N170 G90"), context)
    assert "no_program_end" in codes(report)


def test_missing_release_identity_is_reported(context):
    report = validate(SAFE_PROGRAM.replace("(RELEASE ID REL-9999)", "(NO TRACE)"), context)
    assert "header_traceability_missing" in codes(report)


def test_reference_return_blocks_are_not_read_as_programmed_positions(context):
    """G91 G28 Z0. is an incremental reference return, not a move to Z0."""
    report = validate(SAFE_PROGRAM, context)
    assert "travel_violation" not in codes(report)
    # Z0 from the G28 block must not widen the reported envelope.
    assert report.envelope["max"][2] == pytest.approx(25.0)


def test_program_beyond_the_simulated_envelope_is_a_stop(context):
    context.simulated_envelope = {"min": [0.0, 0.0, -2.0], "max": [10.0, 10.0, 25.0]}
    report = validate(SAFE_PROGRAM, context)
    assert "envelope_mismatch" in codes(report)
    finding = next(f for f in report.findings if f.code == "envelope_mismatch")
    assert "nothing was verified" in finding.consequence
