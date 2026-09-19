"""Parser dispatch.

Each artifact kind maps to one reader. Parsing is bounded and total: an
unreadable file produces a recorded failure with a reason, never a silent empty
result (FR-INT-002).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.enums import ArtifactKind, ArtifactStatus
from app.parsers import drawing, dxf, measurement, step, stl


@dataclass
class ParseResult:
    status: ArtifactStatus
    parser: str
    extracted: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def parse_artifact(kind: ArtifactKind, data: bytes, filename: str = "") -> ParseResult:
    try:
        if kind is ArtifactKind.STEP:
            extract = step.parse(data)
            return ParseResult(ArtifactStatus.PROCESSED, "step/1.0", extract.to_dict(), extract.warnings)
        if kind is ArtifactKind.STL:
            extract = stl.parse(data)
            return ParseResult(ArtifactStatus.PROCESSED, "stl/1.0", extract.to_dict(), extract.warnings)
        if kind is ArtifactKind.DXF:
            extract = dxf.parse(data)
            payload = extract.to_dict()
            payload["outline_candidate"] = dxf.outline_candidate(extract)
            payload["hole_candidates"] = dxf.hole_candidates(extract)
            return ParseResult(ArtifactStatus.PROCESSED, "dxf/1.0", payload, extract.warnings)
        if kind is ArtifactKind.DRAWING_PDF:
            extract = drawing.parse(data)
            return ParseResult(ArtifactStatus.PROCESSED, "drawing-pdf/1.0", extract.to_dict(), extract.warnings)
        if kind is ArtifactKind.MEASUREMENT_CSV:
            extract = measurement.parse(data)
            return ParseResult(ArtifactStatus.PROCESSED, "measurement-csv/1.0", extract.to_dict(), extract.warnings)
        if kind in (ArtifactKind.IMAGE, ArtifactKind.VIDEO):
            return ParseResult(
                ArtifactStatus.PROCESSED,
                "media/1.0",
                {
                    "note": (
                        "Media is retained as evidence. Geometry is produced by the reconstruction "
                        "service, not by this parser."
                    ),
                    "byte_size": len(data),
                },
            )
        if kind is ArtifactKind.OBJ:
            return ParseResult(
                ArtifactStatus.PROCESSED,
                "obj/1.0",
                {"vertex_count": data.count(b"\nv "), "face_count": data.count(b"\nf ")},
                ["OBJ is accepted as a visual derivative; it is not used as engineering geometry"],
            )
        return ParseResult(
            ArtifactStatus.UNSUPPORTED,
            "none",
            {},
            [],
            f"No parser is registered for {kind}; the file is retained but cannot be interpreted",
        )
    except Exception as exc:  # noqa: BLE001 - untrusted input must never crash the worker
        return ParseResult(ArtifactStatus.FAILED, f"{kind}/failed", {}, [], f"{type(exc).__name__}: {exc}")
