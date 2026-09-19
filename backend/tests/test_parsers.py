"""Parser tests against the benchmark artifacts shipped with the repository."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.enums import ArtifactKind, ArtifactStatus, AuthorityRank
from app.parsers import classify, parse_artifact

SAMPLES = Path(__file__).resolve().parents[2] / "samples"


def read(relative: str) -> bytes:
    return (SAMPLES / relative).read_bytes()


def test_classification_uses_content_not_just_the_extension():
    kind, authority = classify("BRK-1042.step", read("cad/BRK-1042.step")[:4096])
    assert kind is ArtifactKind.STEP
    # A CAD file is not "verified CAD" until an engineer says so (FR-INT-004).
    assert authority is AuthorityRank.SCAN

    kind, _ = classify("mislabelled.txt", read("drawings/BRK-1042.pdf")[:4096])
    assert kind is ArtifactKind.DRAWING_PDF

    kind, authority = classify("unknown.bin", b"\x00\x01\x02\x03")
    assert kind is ArtifactKind.UNKNOWN
    assert authority is AuthorityRank.AI_INFERENCE


def test_unsupported_file_fails_safely_rather_than_silently():
    result = parse_artifact(ArtifactKind.UNKNOWN, b"\x00\x01", "unknown.bin")
    assert result.status is ArtifactStatus.UNSUPPORTED
    assert "No parser is registered" in (result.error or "")


def test_a_hostile_file_does_not_crash_the_worker():
    # Truncated, contradictory binary STL: declares a huge triangle count.
    hostile = b"x" * 80 + (10**9).to_bytes(4, "little") + b"\x00" * 10
    result = parse_artifact(ArtifactKind.STL, hostile, "hostile.stl")
    assert result.status is ArtifactStatus.PROCESSED
    assert any("parser limit" in w for w in result.warnings)


def test_step_reads_units_envelope_and_cylinders():
    result = parse_artifact(ArtifactKind.STEP, read("cad/BRK-1042.step"))
    extracted = result.extracted
    assert extracted["units"] == "millimetre"
    assert extracted["size_mm"] == pytest.approx([100.0, 60.0, 20.0])
    assert extracted["product_name"] == "Mounting bracket"
    cylinders = extracted["cylinders"]
    assert len(cylinders) == 4
    assert all(c["axis_is_z"] for c in cylinders)
    assert all(c["diameter_mm"] == pytest.approx(8.2) for c in cylinders)


def test_stl_reports_volume_only_for_a_closed_mesh():
    result = parse_artifact(ArtifactKind.STL, read("cad/BRK-1042-envelope.stl"))
    extracted = result.extracted
    assert extracted["closed"] is True
    assert extracted["volume_mm3"] == pytest.approx(100 * 60 * 20, rel=1e-6)
    assert extracted["size_mm"] == pytest.approx([100.0, 60.0, 20.0])

    open_mesh = (
        b"solid open\nfacet normal 0 0 1\nouter loop\n"
        b"vertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\n"
        b"endloop\nendfacet\nendsolid open\n"
    )
    result = parse_artifact(ArtifactKind.STL, open_mesh)
    assert result.extracted["closed"] is False
    assert result.extracted["volume_mm3"] is None
    assert any("not closed" in w for w in result.warnings)


def test_dxf_gives_an_outline_and_a_hole_pattern():
    result = parse_artifact(ArtifactKind.DXF, read("drawings/BRK-1042.dxf"))
    extracted = result.extracted
    assert extracted["units"] == "mm"
    assert extracted["counts"]["circles"] == 4
    outline = extracted["outline_candidate"]
    assert outline["shape"] == "polygon"
    assert len(outline["points"]) == 4
    holes = extracted["hole_candidates"]
    assert len(holes) == 4
    assert {tuple(h["center"]) for h in holes} == {(40.0, 20.0), (-40.0, 20.0), (-40.0, -20.0), (40.0, -20.0)}
    assert all("not established by a 2D view" in h["note"] for h in holes)


def test_drawing_extracts_toleranced_callouts_as_candidates():
    result = parse_artifact(ArtifactKind.DRAWING_PDF, read("drawings/BRK-1042.pdf"))
    extracted = result.extracted
    assert extracted["declared_units"] == "mm"
    assert extracted["general_tolerance"] == "ISO 2768-m"
    assert "EN AW-6082" in extracted["material_candidates"]

    kinds = {c["kind"] for c in extracted["dimension_candidates"]}
    assert {"linear_toleranced", "diameter", "diameter_toleranced", "thread", "surface_finish"} <= kinds

    reamed = next(c for c in extracted["dimension_candidates"] if c["kind"] == "diameter_toleranced")
    assert reamed["value"] == pytest.approx(12.02)
    assert reamed["tolerance"] == {"plus": 0.02, "minus": 0.0}
    # A toleranced callout is a stronger signal than a loose number, but still
    # a candidate: extraction is never verification.
    assert reamed["confidence"] > 0.8

    thread = next(c for c in extracted["dimension_candidates"] if c["kind"] == "thread")
    assert thread["value_text"] == "M8x1.25"
    assert thread["pitch"] == pytest.approx(1.25)


def test_measurement_csv_normalises_units_and_rejects_bad_rows():
    result = parse_artifact(ArtifactKind.MEASUREMENT_CSV, read("measurements/BRK-1042-firstpiece.csv"))
    extracted = result.extracted
    assert extracted["row_count"] == 6
    assert extracted["rejected_count"] == 0
    bore = next(r for r in extracted["rows"] if r["feature_key"] == "H5")
    assert bore["value"] == pytest.approx(12.019)
    assert bore["uncertainty"] == pytest.approx(0.005)  # CMM

    messy = b"feature,attribute,value,unit\nA,length,4.0,in\nB,length,not-a-number,mm\nC,length,10,furlong\n"
    result = parse_artifact(ArtifactKind.MEASUREMENT_CSV, messy)
    rows = result.extracted["rows"]
    assert len(rows) == 1
    assert rows[0]["value"] == pytest.approx(101.6)  # 4 inch converted explicitly
    reasons = " ".join(r["reason"] for r in result.extracted["rejected"])
    assert "not a number" in reasons
    assert "not recognised" in reasons  # a bad unit is never assumed away
