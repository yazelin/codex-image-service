from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import db
from app.api import admin, public
from app.config import get_settings
from app.services.cleanup import CleanupService
from app.services.job_queue import ImageJobQueue
from app.services.storage import ensure_storage


# 服務啟動時間。Overview 的 Uptime 用它 —— 姊妹服務 gemini-web 早就有這格，
# 判「這台是不是剛被重啟過」時很常用（例如帳號 UI 變體修好後要確認生效）。
_START_TIME = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    ensure_storage(settings)
    db.init_db(settings)

    cleanup = CleanupService(settings)
    await cleanup.run_once()

    job_queue = ImageJobQueue(settings)
    await job_queue.start()

    app.state.settings = settings
    app.state.cleanup = cleanup
    app.state.job_queue = job_queue

    await cleanup.start()
    try:
        yield
    finally:
        await cleanup.stop()
        await job_queue.stop()


settings = get_settings()
app = FastAPI(title="Codex Image Web Service", version="0.1.0", lifespan=lifespan)
app.mount("/generated", StaticFiles(directory=settings.generated_dir, check_dir=False), name="generated")
app.include_router(public.router)
app.include_router(admin.router)


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok"}
