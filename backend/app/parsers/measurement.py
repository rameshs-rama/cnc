"""Measurement CSV ingestion.

A measurement carries higher authority than a photograph, so the reader is
strict: an unparsable row is reported, never rounded into something usable.

Recognised columns (case-insensitive, flexible order):
    feature, attribute, value, unit, tolerance_plus, tolerance_minus,
    instrument, uncertainty, operator, criticality, note
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any

from app.core.enums import Criticality, VerificationStatus

MAX_ROWS = 100_000

_ALIASES = {
    "feature": {"feature", "feature_id", "feature_key", "id", "characteristic_id"},
    "attribute": {"attribute", "characteristic", "dimension", "parameter", "type"},
    "value": {"value", "measured", "actual", "reading", "measurement"},
    "unit": {"unit", "units", "uom"},
    "tolerance_plus": {"tolerance_plus", "tol_plus", "upper_tol", "plus", "usl"},
    "tolerance_minus": {"tolerance_minus", "tol_minus", "lower_tol", "minus", "lsl"},
    "nominal": {"nominal", "target", "design", "basic"},
    "instrument": {"instrument", "gauge", "gage", "device", "equipment"},
    "uncertainty": {"uncertainty", "u", "expanded_uncertainty", "accuracy"},
    "operator": {"operator", "inspector", "measured_by", "user"},
    "criticality": {"criticality", "critical", "importance", "class"},
    "note": {"note", "notes", "comment", "remark"},
}

_UNIT_TO_MM = {"mm": 1.0, "millimetre": 1.0, "millimeter": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "inch": 25.4, "thou": 0.0254, "mil": 0.0254}


@dataclass
class MeasurementExtract:
    rows: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    delimiter: str = ","
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_count": len(self.rows),
            "rejected_count": len(self.rejected),
            "columns": self.columns,
            "delimiter": self.delimiter,
            "rows": self.rows[:500],
            "rejected": self.rejected[:100],
            "warnings": self.warnings,
        }


def parse(data: bytes) -> MeasurementExtract:
    result = MeasurementExtract()
    text = data.decode("utf-8-sig", errors="replace")
    if not text.strip():
        result.warnings.append("File is empty")
        return result

    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
        result.delimiter = dialect.delimiter
    except csv.Error:
        result.delimiter = ","
        result.warnings.append("Delimiter could not be detected; comma assumed")

    reader = csv.DictReader(io.StringIO(text), delimiter=result.delimiter)
    result.columns = [c for c in (reader.fieldnames or []) if c]
    mapping = _map_columns(result.columns)
    if "value" not in mapping:
        result.warnings.append("No measured-value column was recognised; nothing can be imported from this file")
        return result

    for index, raw in enumerate(reader, start=2):
        if index - 1 > MAX_ROWS:
            result.warnings.append(f"Stopped after {MAX_ROWS} rows")
            break
        entry, error = _read_row(raw, mapping)
        if error:
            result.rejected.append({"line": index, "reason": error, "raw": {k: v for k, v in list(raw.items())[:8]}})
        else:
            entry["line"] = index
            result.rows.append(entry)
    return result


def _map_columns(columns: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for column in columns:
        key = column.strip().lower().replace(" ", "_")
        for canonical, aliases in _ALIASES.items():
            if key in aliases and canonical not in mapping:
                mapping[canonical] = column
    return mapping


def _read_row(raw: dict[str, Any], mapping: dict[str, str]) -> tuple[dict[str, Any], str | None]:
    def field_value(name: str) -> str:
        return str(raw.get(mapping.get(name, ""), "") or "").strip()

    value_text = field_value("value")
    if not value_text:
        return {}, "Measured value is blank"
    try:
        value = float(value_text.replace(",", "."))
    except ValueError:
        return {}, f"Measured value {value_text!r} is not a number"

    unit = (field_value("unit") or "mm").lower()
    scale = _UNIT_TO_MM.get(unit)
    if scale is None:
        return {}, f"Unit {unit!r} is not recognised; conversion is never assumed"

    entry: dict[str, Any] = {
        "feature_key": field_value("feature") or None,
        "attribute": (field_value("attribute") or "length").lower().replace(" ", "_"),
        "value": value * scale,
        "original_value": value,
        "unit": "mm",
        "original_unit": unit,
        "instrument": field_value("instrument") or None,
        "operator": field_value("operator") or None,
        "note": field_value("note") or None,
        "status": VerificationStatus.MEASURED,
    }

    for name in ("tolerance_plus", "tolerance_minus", "nominal", "uncertainty"):
        text = field_value(name)
        if not text:
            continue
        try:
            entry[name] = float(text.replace(",", ".")) * scale
        except ValueError:
            return {}, f"{name} value {text!r} is not a number"

    criticality = field_value("criticality").lower()
    if criticality:
        entry["criticality"] = {
            "safety": Criticality.SAFETY,
            "function": Criticality.FUNCTION,
            "functional": Criticality.FUNCTION,
            "quality": Criticality.QUALITY,
            "critical": Criticality.FUNCTION,
            "ctq": Criticality.QUALITY,
        }.get(criticality, Criticality.NONCRITICAL)
    else:
        entry["criticality"] = Criticality.NONCRITICAL

    # Instrument uncertainty defaults are deliberately conservative: an
    # unstated instrument does not get micron-grade confidence.
    if "uncertainty" not in entry:
        instrument = (entry.get("instrument") or "").lower()
        if "cmm" in instrument:
            entry["uncertainty"] = 0.005
        elif "micrometer" in instrument or "micrometre" in instrument:
            entry["uncertainty"] = 0.01
        elif "caliper" in instrument or "calliper" in instrument:
            entry["uncertainty"] = 0.03
        else:
            entry["uncertainty"] = 0.05
    return entry, None
