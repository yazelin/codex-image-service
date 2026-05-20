from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import db
from app.config import Settings


@dataclass
class CleanupResult:
    expired_requests: int = 0
    deleted_files: int = 0
    deleted_workdirs: int = 0
    errors: list[str] | None = None

    def add_error(self, message: str) -> None:
        if self.errors is None:
            self.errors = []
        self.errors.append(message)


def ensure_storage(settings: Settings) -> None:
    settings.generated_dir.mkdir(parents=True, exist_ok=True)
    settings.codex_workdir.mkdir(parents=True, exist_ok=True)


def generated_image_path(settings: Settings, request_id: str, index: int, count: int) -> Path:
    filename = f"{request_id}.png" if count == 1 else f"{request_id}_{index + 1}.png"
    return settings.generated_dir / filename


def public_image_url(settings: Settings, image_path: Path) -> str:
    filename = image_path.name
    if settings.public_base_url:
        return f"{settings.public_base_url}/generated/{filename}"
    return f"/generated/{filename}"


def expiration_iso(settings: Settings) -> str:
    return (db.utc_now() + timedelta(days=settings.image_retention_days)).isoformat()


def cleanup_expired_storage(settings: Settings) -> CleanupResult:
    ensure_storage(settings)
    result = CleanupResult(errors=[])
    now = db.iso_now()
    expired_rows = db.list_expired_image_requests(settings, before_iso=now)

    for row in expired_rows:
        for raw_path in row.get("image_paths", []):
            image_path = Path(raw_path)
            try:
                if image_path.exists() and image_path.is_file():
                    image_path.unlink()
                    result.deleted_files += 1
            except OSError as exc:
                result.add_error(f"Could not delete image {image_path}: {exc}")

        workdir = row.get("workdir")
        if workdir:
            try:
                workdir_path = Path(workdir)
                if workdir_path.exists() and workdir_path.is_dir():
                    shutil.rmtree(workdir_path)
                    result.deleted_workdirs += 1
            except OSError as exc:
                result.add_error(f"Could not delete workdir {workdir}: {exc}")

        db.mark_image_request_expired(settings, row["id"])
        result.expired_requests += 1

    result.deleted_files += _cleanup_orphan_images(settings, result)
    result.deleted_workdirs += _cleanup_old_workdirs(settings, result)
    return result


def _cleanup_orphan_images(settings: Settings, result: CleanupResult) -> int:
    cutoff = db.utc_now() - timedelta(days=settings.image_retention_days)
    deleted = 0
    if not settings.generated_dir.exists():
        return deleted

    tracked = {
        Path(path).resolve()
        for request in db.list_image_requests(settings, limit=10000)
        for path in request.get("image_paths", [])
    }
    for image_path in settings.generated_dir.iterdir():
        if image_path.name == ".gitkeep" or not image_path.is_file():
            continue
        if image_path.resolve() in tracked:
            continue
        modified_at = datetime.fromtimestamp(image_path.stat().st_mtime, tz=timezone.utc)
        if modified_at > cutoff:
            continue
        try:
            image_path.unlink()
            deleted += 1
        except OSError as exc:
            result.add_error(f"Could not delete orphan image {image_path}: {exc}")
    return deleted


def _cleanup_old_workdirs(settings: Settings, result: CleanupResult) -> int:
    cutoff = db.utc_now() - timedelta(days=settings.image_retention_days)
    deleted = 0
    if not settings.codex_workdir.exists():
        return deleted

    tracked = {
        str(request.get("workdir"))
        for request in db.list_image_requests(settings, limit=10000)
        if request.get("workdir")
    }
    for workdir in settings.codex_workdir.iterdir():
        if not workdir.is_dir() or str(workdir) in tracked:
            continue
        modified_at = datetime.fromtimestamp(workdir.stat().st_mtime, tz=timezone.utc)
        if modified_at > cutoff:
            continue
        try:
            shutil.rmtree(workdir)
            deleted += 1
        except OSError as exc:
            result.add_error(f"Could not delete orphan workdir {workdir}: {exc}")
    return deleted
