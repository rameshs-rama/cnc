"""Source artifact parsers.

Every parser runs on untrusted bytes, so each one is bounded in input size and
iteration count and returns candidates rather than verified values. Extraction
never produces a Confirmed observation on its own (FR-INT-005).
"""

from app.parsers.detect import classify, detect_kind
from app.parsers.registry import ParseResult, parse_artifact

__all__ = ["ParseResult", "classify", "detect_kind", "parse_artifact"]
