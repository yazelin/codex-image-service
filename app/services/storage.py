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
    deleted_sessions: int = 0
    freed_session_bytes: int = 0
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
    _cleanup_old_codex_sessions(settings, result)
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


def _codex_session_dirs(settings: Settings) -> list[Path]:
    """Every CODEX_HOME the service rotates through, plus the container default.

    CODEX_HOMES is a colon-separated list of home directories (one per account).
    When it is empty the service falls back to the container's own ~/.codex.
    """
    from app.services.codex_image import _codex_home

    # 沒設 CODEX_HOMES 時，用跟 codex_image.py 完全同一套 fallback（$CODEX_HOME → ~/.codex）。
    # 兩處對「home 在哪」如果各判各的，就會清錯目錄或整個漏掉。
    homes = [Path(h) for h in settings.codex_homes] or [_codex_home(None)]
    return [h / "sessions" for h in homes]


def _cleanup_old_codex_sessions(settings: Settings, result: CleanupResult) -> None:
    """Delete rollout .jsonl files older than SESSION_RETENTION_DAYS.

    Why this exists: newer Codex embeds the generated image as base64 inside the
    session rollout jsonl instead of writing a png (see codex_image.py), and this
    service reads the image back out of it. That makes the rollout a *required*
    intermediate — and a large one, since every image is carried in full as
    base64. Once the image has been extracted and saved to generated/, the
    rollout is dead weight.

    Left alone it grows without bound: on one deployment ~/codex-homes reached
    23 GB across three accounts, 5.7 GB of it from a single month of heavy
    generation, and it was still growing by ~1.3 GB every three days.

    Retention is deliberately short (3 days). These files are only useful for
    `codex resume`, which this service never does — it runs one-shot
    subprocesses. The window exists purely so a human can inspect a recent
    failure.
    """
    if settings.session_retention_days <= 0:
        return  # 0 或負數 = 停用，留給想自己管的部署

    cutoff = (db.utc_now() - timedelta(days=settings.session_retention_days)).timestamp()
    for sessions_dir in _codex_session_dirs(settings):
        if not sessions_dir.is_dir():
            continue
        for rollout in sessions_dir.rglob("*.jsonl"):
            try:
                stat = rollout.stat()
                if stat.st_mtime >= cutoff:
                    continue
                size = stat.st_size
                rollout.unlink()
                result.deleted_sessions += 1
                result.freed_session_bytes += size
            except OSError as exc:
                result.add_error(f"Could not delete session rollout {rollout}: {exc}")

        # 清掉因此變空的日期目錄（sessions/YYYY/MM/DD），別動 sessions/ 本身
        for path in sorted(sessions_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir() and not any(path.iterdir()):
                try:
                    path.rmdir()
                except OSError:
                    pass
