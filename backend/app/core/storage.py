"""Content-addressed object store.

Files are written under ``<tenant>/<prefix>/<hash>`` so the same bytes are
stored once and a path leak cannot cross a tenant boundary (PRD 7.3).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import BinaryIO

from app.config import get_settings
from app.core.errors import ValidationFailed
from app.core.hashing import sha256_bytes, sha256_file


class ObjectStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or get_settings().object_store)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not str(path).startswith(str(self.root.resolve())):
            raise ValidationFailed("Rejected object key outside the store root", code="bad_object_key")
        return path

    def put_bytes(self, tenant_id: str, prefix: str, data: bytes, suffix: str = "") -> tuple[str, str]:
        digest = sha256_bytes(data)
        key = f"{tenant_id}/{prefix}/{digest}{suffix}"
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(data)
        return key, digest

    def put_stream(
        self, tenant_id: str, prefix: str, stream: BinaryIO, suffix: str = "", max_bytes: int | None = None
    ) -> tuple[str, str, int]:
        settings = get_settings()
        limit = max_bytes or settings.max_upload_bytes
        staging = self.root / tenant_id / "_staging"
        staging.mkdir(parents=True, exist_ok=True)
        temp = staging / f"upload-{id(stream)}.part"
        size = 0
        with temp.open("wb") as handle:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    handle.close()
                    temp.unlink(missing_ok=True)
                    raise ValidationFailed(
                        f"Upload exceeds the {limit} byte limit for this tenant",
                        code="upload_too_large",
                    )
                handle.write(chunk)
        digest = sha256_file(temp)
        key = f"{tenant_id}/{prefix}/{digest}{suffix}"
        destination = self._path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            temp.unlink(missing_ok=True)
        else:
            shutil.move(str(temp), str(destination))
        return key, digest, size

    def get_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise ValidationFailed(f"Object {key} is missing from the store", code="object_missing")
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def path_for(self, key: str) -> Path:
        return self._path(key)


_store: ObjectStore | None = None


def get_store() -> ObjectStore:
    global _store
    if _store is None:
        _store = ObjectStore()
    return _store
