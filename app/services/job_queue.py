from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass
from typing import Any

from app import db
from app.config import Settings
from app.models import ImageGenerateRequest
from app.services import storage
from app.services.codex_image import CodexGenerationError, CodexImageGenerator


class GenerationJobFailed(Exception):
    pass


class GenerationQueueUnavailable(Exception):
    pass


@dataclass
class ImageJob:
    request_id: str
    api_key_id: str
    payload: ImageGenerateRequest
    expires_at: str
    future: asyncio.Future[dict[str, Any]]


class ImageJobQueue:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.queue: asyncio.Queue[ImageJob] = asyncio.Queue(
            maxsize=settings.generation_queue_max_size
        )
        self.generator = CodexImageGenerator(settings)
        self.workers: list[asyncio.Task[None]] = []
        self.started = False

    async def start(self) -> None:
        if self.started:
            return
        self.started = True
        for worker_index in range(max(1, self.settings.codex_worker_concurrency)):
            self.workers.append(asyncio.create_task(self._worker(worker_index)))

    async def stop(self) -> None:
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()
        self.started = False

    def _build_job(
        self, *, api_key_id: str, payload: ImageGenerateRequest
    ) -> ImageJob:
        request_id = f"img_{secrets.token_hex(12)}"
        expires_at = storage.expiration_iso(self.settings)
        db.insert_image_request(
            self.settings,
            request_id=request_id,
            api_key_id=api_key_id,
            prompt=payload.prompt,
            size=payload.size,
            quality=payload.quality,
            count=payload.count,
            expires_at=expires_at,
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        return ImageJob(
            request_id=request_id,
            api_key_id=api_key_id,
            payload=payload,
            expires_at=expires_at,
            future=future,
        )

    async def submit_only(
        self, *, api_key_id: str, payload: ImageGenerateRequest
    ) -> str:
        if not self.started:
            raise GenerationQueueUnavailable("Generation queue is not running")
        job = self._build_job(api_key_id=api_key_id, payload=payload)
        job.future.add_done_callback(_consume_future_exception)
        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull:
            db.mark_image_request_failed(
                self.settings,
                request_id=job.request_id,
                error="Queue is full",
            )
            raise GenerationQueueUnavailable("Generation queue is full") from None
        db.mark_api_key_used(self.settings, api_key_id)
        return job.request_id

    async def submit_and_wait(
        self, *, api_key_id: str, payload: ImageGenerateRequest
    ) -> dict[str, Any]:
        if not self.started:
            raise GenerationQueueUnavailable("Generation queue is not running")

        job = self._build_job(api_key_id=api_key_id, payload=payload)
        request_id = job.request_id
        expires_at = job.expires_at
        future = job.future
        started_waiting = time.monotonic()
        try:
            await asyncio.wait_for(
                self.queue.put(job),
                timeout=self.settings.request_wait_timeout_seconds,
            )
        except asyncio.TimeoutError:
            db.mark_image_request_failed(
                self.settings,
                request_id=request_id,
                error="Timed out waiting for generation queue capacity",
            )
            raise

        remaining_timeout = max(
            1,
            self.settings.request_wait_timeout_seconds - (time.monotonic() - started_waiting),
        )

        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=remaining_timeout)
        except asyncio.TimeoutError:
            future.add_done_callback(_consume_future_exception)
            raise

    async def _worker(self, worker_index: int) -> None:
        while True:
            job = await self.queue.get()
            try:
                await self._run_job(job)
            finally:
                self.queue.task_done()

    async def _run_job(self, job: ImageJob) -> None:
        db.mark_image_request_running(self.settings, job.request_id)
        try:
            result = await self.generator.generate(
                request_id=job.request_id,
                prompt=job.payload.prompt,
                size=job.payload.size,
                quality=job.payload.quality,
                count=job.payload.count,
                reference_image_base64=job.payload.reference_image_base64,
            )
            db.mark_image_request_succeeded(
                self.settings,
                request_id=job.request_id,
                image_paths=result.image_paths,
                stdout=result.stdout,
                stderr=result.stderr,
                duration_seconds=result.duration_seconds,
                workdir=result.workdir,
                codex_command=result.command,
                codex_home=result.codex_home,
            )
            response = {
                "id": job.request_id,
                "status": "succeeded",
                "images": [
                    {
                        "url": storage.public_image_url(self.settings, path),
                        "expires_at": job.expires_at,
                    }
                    for path in result.image_paths
                ],
                "created_at": db.get_image_request(self.settings, job.request_id)["created_at"],
            }
            if not job.future.done():
                job.future.set_result(response)
        except CodexGenerationError as exc:
            db.mark_image_request_failed(
                self.settings,
                request_id=job.request_id,
                error=str(exc),
                stdout=exc.stdout,
                stderr=exc.stderr,
                workdir=exc.workdir,
                codex_command=exc.command,
            )
            if not job.future.done():
                job.future.set_exception(GenerationJobFailed(str(exc)))
        except Exception as exc:
            db.mark_image_request_failed(
                self.settings,
                request_id=job.request_id,
                error=str(exc),
            )
            if not job.future.done():
                job.future.set_exception(GenerationJobFailed(str(exc)))


def _consume_future_exception(future: asyncio.Future[dict[str, Any]]) -> None:
    if future.cancelled():
        return
    try:
        future.exception()
    except Exception:
        return
