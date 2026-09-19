"""Drawing text extraction and dimension candidate recognition (FR-INT-005).

Text is pulled from the PDF content streams, then scanned for dimension-shaped
tokens. Everything found is a *candidate*. A drawing outranks a photograph in
the evidence hierarchy, but only once a human confirms that the reader picked up
the right value - OCR-grade extraction is not verification.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field
from typing import Any

MAX_STREAMS = 4000
MAX_TEXT = 2_000_000

_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
_TJ_SIMPLE = re.compile(rb"\((?:\\.|[^\\()])*\)\s*Tj", re.DOTALL)
_TJ_ARRAY = re.compile(rb"\[(.*?)\]\s*TJ", re.DOTALL)
_STRING = re.compile(rb"\((?:\\.|[^\\()])*\)", re.DOTALL)

#: Dimension-shaped tokens. Ordered most specific first so a toleranced
#: diameter is not first matched as a bare number.
PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "diameter_toleranced",
        re.compile(r"(?:Ø|⌀|DIA\.?\s*)(\d+(?:\.\d+)?)\s*(?:\+\s*(\d+\.\d+)\s*/?\s*-\s*(\d+\.\d+)|±\s*(\d+\.\d+))", re.IGNORECASE),
        "diameter",
    ),
    ("diameter", re.compile(r"(?:Ø|⌀|DIA\.?\s*)(\d+(?:\.\d+)?)", re.IGNORECASE), "diameter"),
    (
        "linear_toleranced",
        re.compile(r"(?<![\d.])(\d+\.\d+)\s*(?:±\s*(\d+\.\d+)|\+\s*(\d+\.\d+)\s*/?\s*-\s*(\d+\.\d+))"),
        "length",
    ),
    ("thread", re.compile(r"\bM(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)"), "thread"),
    ("radius", re.compile(r"\bR\s?(\d+(?:\.\d+)?)\b"), "radius"),
    ("surface_finish", re.compile(r"\bRa\s*(\d+(?:\.\d+)?)", re.IGNORECASE), "surface_finish"),
    ("angle", re.compile(r"(\d+(?:\.\d+)?)\s*°"), "angle"),
    ("counterbore", re.compile(r"(?:⌴|C['’]?BORE)\s*(?:Ø|⌀)?\s*(\d+(?:\.\d+)?)", re.IGNORECASE), "counterbore"),
]

_MATERIAL = re.compile(
    r"\b(AL\s?\d{4}[-\s]?[A-Z0-9]*|ALUMINI?UM\s+\d{4}[-\s]?[A-Z0-9]*|EN\s?AW[-\s]?\d{4}|"
    r"S\s?235|S\s?355|1\.\d{4}|AISI\s?\d{3}[A-Z]?|304L?|316L?|C45|42CrMo4)\b",
    re.IGNORECASE,
)
_GENERAL_TOLERANCE = re.compile(r"\bISO\s?2768\s*[-–]?\s*([a-zA-Z]{1,2})\b", re.IGNORECASE)
_UNITS = re.compile(r"\b(DIMENSIONS?\s+IN\s+(MM|MILLIMET(?:RE|ER)S|INCH(?:ES)?))\b", re.IGNORECASE)


@dataclass
class DrawingExtract:
    page_count: int = 0
    text: str = ""
    characters: int = 0
    dimension_candidates: list[dict[str, Any]] = field(default_factory=list)
    material_candidates: list[str] = field(default_factory=list)
    general_tolerance: str | None = None
    declared_units: str | None = None
    extraction_method: str = "content stream text"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_count": self.page_count,
            "characters": self.characters,
            "text_sample": self.text[:4000],
            "dimension_candidates": self.dimension_candidates,
            "material_candidates": self.material_candidates,
            "general_tolerance": self.general_tolerance,
            "declared_units": self.declared_units,
            "extraction_method": self.extraction_method,
            "warnings": self.warnings,
        }


def parse(data: bytes) -> DrawingExtract:
    result = DrawingExtract()
    result.page_count = data.count(b"/Type /Page") + data.count(b"/Type/Page")

    chunks: list[str] = []
    for index, match in enumerate(_STREAM.finditer(data)):
        if index >= MAX_STREAMS:
            result.warnings.append(f"Stopped after {MAX_STREAMS} content streams")
            break
        raw = match.group(1)
        payload = raw
        try:
            payload = zlib.decompress(raw)
        except zlib.error:
            # Not deflate-compressed, or an image stream. Uncompressed content
            # streams are still readable; anything else yields no text.
            pass
        chunks.append(_text_from_stream(payload))
        if sum(len(c) for c in chunks) > MAX_TEXT:
            result.warnings.append("Text truncated at the parser limit")
            break

    result.text = "\n".join(c for c in chunks if c).strip()
    result.characters = len(result.text)
    if not result.text:
        result.warnings.append(
            "No extractable text. The drawing is probably a scan; optical recognition or manual entry is required."
        )
        result.extraction_method = "none"
        return result

    result.dimension_candidates = find_dimensions(result.text)
    result.material_candidates = sorted({m.group(1).upper().replace("  ", " ") for m in _MATERIAL.finditer(result.text)})
    if match := _GENERAL_TOLERANCE.search(result.text):
        result.general_tolerance = f"ISO 2768-{match.group(1)}"
    if match := _UNITS.search(result.text):
        result.declared_units = "inch" if "INCH" in match.group(2).upper() else "mm"
    else:
        result.warnings.append("Drawing does not declare its units in extractable text; units must be confirmed")
    return result


def _text_from_stream(payload: bytes) -> str:
    pieces: list[str] = []
    for match in _TJ_SIMPLE.finditer(payload):
        pieces.append(_decode_pdf_string(match.group(0)[:-2].strip()))
    for match in _TJ_ARRAY.finditer(payload):
        parts = [_decode_pdf_string(s.group(0)) for s in _STRING.finditer(match.group(1))]
        if parts:
            pieces.append("".join(parts))
    return "\n".join(p for p in pieces if p.strip())


_OCTAL = re.compile(r"\\([0-7]{1,3})")


def _decode_pdf_string(raw: bytes) -> str:
    """Decode a PDF literal string, including its escape sequences.

    Octal escapes matter here: symbols an engineer actually reads - the plus or
    minus sign, the diameter sign, the degree sign - are almost always written
    that way, so skipping them silently drops the toleranced callouts that are
    the most useful thing on the drawing.
    """
    body = raw.strip()
    if body.startswith(b"(") and body.endswith(b")"):
        body = body[1:-1]
    text = body.decode("latin-1", errors="replace")
    text = _OCTAL.sub(lambda m: chr(int(m.group(1), 8)), text)
    for escaped, literal in (("\\(", "("), ("\\)", ")"), ("\\\\", "\\"), ("\\n", "\n"), ("\\r", ""), ("\\t", "\t")):
        text = text.replace(escaped, literal)
    return text


def find_dimensions(text: str) -> list[dict[str, Any]]:
    """Extract dimension-shaped tokens with their kind, value and tolerance."""
    found: list[dict[str, Any]] = []
    claimed: list[tuple[int, int]] = []

    for name, pattern, attribute in PATTERNS:
        for match in pattern.finditer(text):
            span = match.span()
            if any(span[0] < end and start < span[1] for start, end in claimed):
                continue
            claimed.append(span)
            entry = _build_candidate(name, attribute, match, text)
            if entry:
                found.append(entry)
    found.sort(key=lambda c: c["source_offset"])
    return found[:512]


def _build_candidate(name: str, attribute: str, match: re.Match[str], text: str) -> dict[str, Any] | None:
    groups = match.groups()
    try:
        value = float(groups[0])
    except (TypeError, ValueError):
        return None

    tolerance: dict[str, float] | None = None
    if name == "diameter_toleranced":
        if groups[3]:
            tolerance = {"plus": float(groups[3]), "minus": float(groups[3])}
        elif groups[1] and groups[2]:
            tolerance = {"plus": float(groups[1]), "minus": float(groups[2])}
    elif name == "linear_toleranced":
        if groups[1]:
            tolerance = {"plus": float(groups[1]), "minus": float(groups[1])}
        elif groups[2] and groups[3]:
            tolerance = {"plus": float(groups[2]), "minus": float(groups[3])}

    entry: dict[str, Any] = {
        "kind": name,
        "attribute": attribute,
        "value": value,
        "unit": "mm",
        "tolerance": tolerance,
        "raw": match.group(0).strip(),
        "context": text[max(0, match.start() - 40) : match.end() + 40].replace("\n", " ").strip(),
        "source_offset": match.start(),
        # A toleranced callout is a far stronger signal than a loose number.
        "confidence": 0.86 if tolerance else (0.72 if name in ("diameter", "thread", "counterbore") else 0.55),
    }
    if name == "thread":
        entry["value_text"] = f"M{groups[0]}x{groups[1]}"
        entry["pitch"] = float(groups[1])
        entry["confidence"] = 0.88
    return entry
