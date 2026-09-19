"""Content addressing.

Every immutable artifact, geometry version, toolpath, simulation and NC program
is identified by the SHA-256 of its canonical bytes. Release packages bind these
hashes so a released program can be reproduced and audited (PRD 8.2, FR-REL-002).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

_CHUNK = 1024 * 1024


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(payload: Any) -> bytes:
    """Deterministic JSON encoding.

    Sorted keys and fixed separators mean the same logical content always
    produces the same hash, which is what makes simulation identity and release
    integrity checks meaningful.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def sha256_json(payload: Any) -> str:
    return sha256_bytes(canonical_json(payload))


def merkle_manifest(entries: dict[str, str]) -> str:
    """Hash a manifest of ``name -> content hash`` pairs into one package hash."""
    return sha256_json({k: entries[k] for k in sorted(entries)})
