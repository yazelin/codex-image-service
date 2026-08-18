from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


@dataclass(frozen=True)
class Settings:
    admin_username: str
    admin_password: str
    admin_session_secret: str
    admin_url_prefix: str
    database_url: str
    generated_dir: Path
    public_base_url: str
    codex_timeout_seconds: int
    codex_workdir: Path
    codex_worker_concurrency: int
    codex_homes: tuple[str, ...]  # CODEX_HOME paths in round-robin order; () = container default
    generation_queue_max_size: int
    request_wait_timeout_seconds: int
    image_retention_days: int
    cleanup_interval_hours: int
    # 有預設值：既有的呼叫端（測試、腳本）不必為了這個欄位全部改一輪
    session_retention_days: int = 3

    @classmethod
    def from_env(cls) -> "Settings":
        _load_env_file()
        return cls(
            admin_username=os.getenv("ADMIN_USERNAME", "admin"),
            admin_password=os.getenv("ADMIN_PASSWORD", "change-me"),
            admin_session_secret=os.getenv("ADMIN_SESSION_SECRET", "dev-only-session-secret"),
            admin_url_prefix=os.getenv("ADMIN_URL_PREFIX", "").rstrip("/"),
            database_url=os.getenv("DATABASE_URL", "sqlite:///./data/app.db"),
            generated_dir=Path(os.getenv("GENERATED_DIR", "static/generated")),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
            codex_timeout_seconds=_int_env("CODEX_TIMEOUT_SECONDS", 360),
            codex_workdir=Path(os.getenv("CODEX_WORKDIR", "./data/codex-runs")),
            codex_worker_concurrency=_int_env("CODEX_WORKER_CONCURRENCY", 2),
            codex_homes=tuple(
                p.strip()
                for p in (os.getenv("CODEX_HOMES") or "").split(":")
                if p.strip()
            ),
            generation_queue_max_size=_int_env("GENERATION_QUEUE_MAX_SIZE", 50),
            request_wait_timeout_seconds=_int_env("REQUEST_WAIT_TIMEOUT_SECONDS", 600),
            image_retention_days=_int_env("IMAGE_RETENTION_DAYS", 7),
            session_retention_days=_int_env("SESSION_RETENTION_DAYS", 3),
            cleanup_interval_hours=_int_env("CLEANUP_INTERVAL_HOURS", 6),
        )

    @property
    def database_path(self) -> Path:
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            raise ValueError("Only sqlite:/// DATABASE_URL values are supported in v0.1")
        raw_path = self.database_url[len(prefix) :]
        return Path(raw_path)


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()
