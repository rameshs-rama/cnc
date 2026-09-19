"""Runtime configuration.

Every setting is environment driven so the same signed image runs in the managed
cloud profile and the private cluster profile (PRD 11, Portability).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MIP_", env_file=".env", extra="ignore")

    # --- storage -----------------------------------------------------------
    database_url: str = "sqlite:///./var/mip.db"
    object_store: Path = Path("./var/object-store")

    # --- security ----------------------------------------------------------
    jwt_secret: str = "dev-only-insecure-secret"
    release_signing_secret: str = "dev-only-insecure-release-secret"
    access_token_minutes: int = 720
    jwt_algorithm: str = "HS256"

    # --- untrusted input guards (PRD 7.3) ----------------------------------
    max_upload_bytes: int = 256 * 1024 * 1024
    max_archive_ratio: int = 120

    # --- compute -----------------------------------------------------------
    worker_threads: int = 2
    job_poll_seconds: float = 0.5
    # Wall-clock ceiling for a single compute job. Postprocessor runtimes get a
    # tighter budget because they consume tenant-supplied configuration.
    job_timeout_seconds: int = 900
    post_runtime_timeout_seconds: int = 30

    # --- product behaviour -------------------------------------------------
    seed_demo: bool = True
    cors_origins: str = "http://localhost:5173,http://localhost:4173"
    api_prefix: str = "/v1"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.object_store.mkdir(parents=True, exist_ok=True)
    if settings.is_sqlite:
        db_path = settings.database_url.split("///", 1)[-1]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return settings
