"""File classification.

The declared extension is a hint, not a fact. Magic bytes and a content sniff
decide, and anything that cannot be identified is quarantined rather than
guessed at (FR-INT-002).
"""

from __future__ import annotations

from app.core.enums import ArtifactKind, AuthorityRank

_EXTENSIONS = {
    "jpg": ArtifactKind.IMAGE,
    "jpeg": ArtifactKind.IMAGE,
    "png": ArtifactKind.IMAGE,
    "webp": ArtifactKind.IMAGE,
    "mp4": ArtifactKind.VIDEO,
    "mov": ArtifactKind.VIDEO,
    "step": ArtifactKind.STEP,
    "stp": ArtifactKind.STEP,
    "stl": ArtifactKind.STL,
    "obj": ArtifactKind.OBJ,
    "dxf": ArtifactKind.DXF,
    "pdf": ArtifactKind.DRAWING_PDF,
    "csv": ArtifactKind.MEASUREMENT_CSV,
}

_MAGIC: list[tuple[bytes, ArtifactKind]] = [
    (b"\xff\xd8\xff", ArtifactKind.IMAGE),
    (b"\x89PNG\r\n\x1a\n", ArtifactKind.IMAGE),
    (b"RIFF", ArtifactKind.IMAGE),
    (b"%PDF", ArtifactKind.DRAWING_PDF),
    (b"ISO-10303-21", ArtifactKind.STEP),
    (b"solid", ArtifactKind.STL),
]

#: Default authority for each kind. An engineer may override with a reason
#: (FR-INT-004); a STEP file is only "verified CAD" once someone says so.
_DEFAULT_AUTHORITY = {
    ArtifactKind.STEP: AuthorityRank.SCAN,
    ArtifactKind.STL: AuthorityRank.SCAN,
    ArtifactKind.OBJ: AuthorityRank.SCAN,
    ArtifactKind.DXF: AuthorityRank.DRAWING,
    ArtifactKind.DRAWING_PDF: AuthorityRank.DRAWING,
    ArtifactKind.MEASUREMENT_CSV: AuthorityRank.MEASUREMENT,
    ArtifactKind.IMAGE: AuthorityRank.PHOTO,
    ArtifactKind.VIDEO: AuthorityRank.PHOTO,
    ArtifactKind.UNKNOWN: AuthorityRank.AI_INFERENCE,
}


def detect_kind(filename: str, head: bytes) -> ArtifactKind:
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            if kind is ArtifactKind.STL and b"facet" not in head[:2048].lower():
                continue
            return kind
    # Binary STL has no magic; it is an 80-byte header then a triangle count.
    if len(head) >= 84 and not head.lstrip().startswith(b"solid"):
        try:
            count = int.from_bytes(head[80:84], "little")
            if 0 < count < 50_000_000:
                suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
                if suffix == "stl":
                    return ArtifactKind.STL
        except ValueError:
            pass
    if b"ISO-10303" in head[:512]:
        return ArtifactKind.STEP
    if b"SECTION" in head[:2048] and b"HEADER" in head[:2048] and b"\n  0\n" in head[:2048].replace(b"\r", b""):
        return ArtifactKind.DXF

    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _EXTENSIONS.get(suffix, ArtifactKind.UNKNOWN)


def classify(filename: str, head: bytes) -> tuple[ArtifactKind, AuthorityRank]:
    kind = detect_kind(filename, head)
    return kind, _DEFAULT_AUTHORITY[kind]


def default_authority(kind: ArtifactKind) -> AuthorityRank:
    return _DEFAULT_AUTHORITY.get(kind, AuthorityRank.AI_INFERENCE)
