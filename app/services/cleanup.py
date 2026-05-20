from __future__ import annotations

import asyncio
import logging

from app.config import Settings
from app.services.storage import CleanupResult, cleanup_expired_storage


logger = logging.getLogger(__name__)


class CleanupService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.task: asyncio.Task[None] | None = None

    async def run_once(self) -> CleanupResult:
        return await asyncio.to_thread(cleanup_expired_storage, self.settings)

    async def start(self) -> None:
        if self.task:
            return
        self.task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if not self.task:
            return
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self.task = None

    async def _loop(self) -> None:
        interval = max(1, self.settings.cleanup_interval_hours) * 60 * 60
        while True:
            await asyncio.sleep(interval)
            try:
                result = await self.run_once()
                if result.errors:
                    logger.warning("Cleanup completed with errors: %s", result.errors)
            except Exception:
                logger.exception("Cleanup failed")

