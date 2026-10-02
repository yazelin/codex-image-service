"""失敗的請求也要記是哪個帳號。

2026-10-01 五個帳號撞生圖上限，失敗列的 codex_home 全是 NULL，Overview
帳號卡的 Failed 因此永遠是 0 —— 撞牆在帳號統計上完全看不到。
"""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from app import db
from app.config import Settings
from app.models import ImageGenerateRequest
from app.services.codex_image import CodexGenerationError
from app.services.job_queue import ImageJobQueue


def _settings(tmp: Path):
    s = Settings(
        admin_username="admin", admin_password="x", admin_session_secret="x",
        admin_url_prefix="", database_url="sqlite:///" + str(tmp / "app.db"),
        generated_dir=tmp / "generated", public_base_url="http://localhost",
        codex_timeout_seconds=5, codex_workdir=tmp, codex_worker_concurrency=1,
        codex_homes=("/h/a", "/h/b"), generation_queue_max_size=1,
        request_wait_timeout_seconds=5, image_retention_days=7, cleanup_interval_hours=6,
    )
    db.init_db(s)
    return s


def test_retry_failure_carries_last_home():
    with TemporaryDirectory() as d:
        q = ImageJobQueue(_settings(Path(d)))
        gen = q.generator

        async def rotation():
            return ["/h/a", "/h/b"]

        async def once(**kw):
            raise CodexGenerationError("429")

        gen._rotation = rotation
        gen._run_codex_once = once
        try:
            asyncio.run(gen._run_codex_with_retry(
                run_dir=Path(d), output_path=Path(d) / "o.png", prompt="p", size="1024x1024",
                quality="low", index=0, count=1, reference_paths=[]))
        except CodexGenerationError as exc:
            assert exc.codex_home == "/h/b"
        else:
            raise AssertionError("should have raised")


def test_failed_job_counts_on_account_card():
    with TemporaryDirectory() as d:
        s = _settings(Path(d))
        q = ImageJobQueue(s)

        async def boom(**kw):
            raise CodexGenerationError("image_gen 額度已用完", codex_home="/h/a")

        q.generator.generate = boom

        async def run():
            key_id = db.create_api_key(s, "t")[0]["id"]
            job = q._build_job(api_key_id=key_id, payload=ImageGenerateRequest(prompt="p"))
            await q._run_job(job)

        asyncio.run(run())
        rows = {r["codex_home"]: r for r in db.per_account_stats(s)}
        assert rows["/h/a"]["failed"] == 1
