"""image_gen 額度用完的帳號冷卻到重置時間，期間不再派工。

存在理由:2026-10-23 yaze-fr 的 image_gen 額度用完（429 usage_limit_reached，
錯誤裡寫明 12:13 重置），但這個額度 wham/usage 看不到，_rotation 的「額度見底就跳過」
只看 5h／Weekly 窗，於是 yaze-fr 每 100 秒還是被派一次、先失敗再由 retry 換帳號，
十五分鐘撞了 11 次。撞過一次就記下 resets_at，之後的請求先跳過它。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from app import db
from app.config import Settings
from app.services import chatgpt_usage, codex_image
from app.services.codex_image import CodexGenerationError, CodexImageGenerator

A, B, C = "/homes/a", "/homes/b", "/homes/c"


def _limit_stderr(reset_at: int) -> str:
    """Codex 撞到 image_gen 額度時 stderr 的那一行（照 .11 的實際錯誤縮短）。"""
    body = json.dumps({"error": {"type": "usage_limit_reached", "message": "The usage limit has been reached",
                                 "resets_at": reset_at, "eligible_promo": None, "limit_window_minutes": None}})
    return f"ERROR codex_core::tools: image generation failed: http 429 Too Many Requests: Some({json.dumps(body)})\n"


def _settings(tmp: Path) -> Settings:
    s = Settings(
        admin_username="admin", admin_password="x", admin_session_secret="x",
        admin_url_prefix="", database_url="sqlite:///" + str(tmp / "app.db"),
        generated_dir=tmp / "generated", public_base_url="http://localhost",
        codex_timeout_seconds=5, codex_workdir=tmp / "runs", codex_worker_concurrency=1,
        codex_homes=(A, B, C), generation_queue_max_size=1,
        request_wait_timeout_seconds=5, image_retention_days=7, cleanup_interval_hours=6,
        codex_min_quota_percent=0,   # 只看冷卻，不讓 5h／Weekly 的跳過規則混進來
    )
    db.init_db(s)
    return s


def _rotation(gen: CodexImageGenerator) -> list[str | None]:
    async def fake_fetch(homes):
        return {}
    original = chatgpt_usage.fetch_many
    chatgpt_usage.fetch_many = fake_fetch
    try:
        return asyncio.run(gen._rotation())
    finally:
        chatgpt_usage.fetch_many = original


def test_撞到額度的帳號冷卻到重置時間():
    with TemporaryDirectory() as d:
        s = _settings(Path(d))
        reset = int(time.time()) + 3600
        assert codex_image.note_image_gen_cooldown(s, B, _limit_stderr(reset)) == reset
        assert codex_image.image_gen_cooldowns(s) == {B: reset}
        gen = CodexImageGenerator(s)
        for _ in range(3):   # 每一輪的輪替起點不同，B 都不在裡面
            rotation = _rotation(gen)
            assert B not in rotation and sorted(rotation) == [A, C]


def test_過了重置時間就回到輪替():
    with TemporaryDirectory() as d:
        s = _settings(Path(d))
        db.set_setting(s, codex_image.IMAGE_GEN_COOLDOWN_KEY, json.dumps({B: int(time.time()) - 5}))
        assert codex_image.image_gen_cooldowns(s) == {}
        assert sorted(_rotation(CodexImageGenerator(s))) == [A, B, C]


def test_全部都在冷卻就照舊輪():
    with TemporaryDirectory() as d:
        s = _settings(Path(d))
        reset = int(time.time()) + 3600
        for home in (A, B, C):
            codex_image.note_image_gen_cooldown(s, home, _limit_stderr(reset))
        assert sorted(_rotation(CodexImageGenerator(s))) == [A, B, C]


def test_別的失敗不會冷卻():   # 負控制：沒有 429 usage_limit_reached 那一行就不記
    with TemporaryDirectory() as d:
        s = _settings(Path(d))
        assert codex_image.note_image_gen_cooldown(s, A, "Codex returned no image in the session rollout") is None
        assert codex_image.note_image_gen_cooldown(s, A, "") is None
        assert codex_image.image_gen_cooldowns(s) == {}
        assert sorted(_rotation(CodexImageGenerator(s))) == [A, B, C]


def test_服務重啟也記得():
    with TemporaryDirectory() as d:
        s = _settings(Path(d))
        reset = int(time.time()) + 3600
        codex_image.note_image_gen_cooldown(s, A, _limit_stderr(reset))
        assert A not in _rotation(CodexImageGenerator(s))   # 新的一個 generator＝重啟後


def test_設定壞掉當成沒有冷卻():
    with TemporaryDirectory() as d:
        s = _settings(Path(d))
        db.set_setting(s, codex_image.IMAGE_GEN_COOLDOWN_KEY, "not json")
        assert codex_image.image_gen_cooldowns(s) == {}
        assert sorted(_rotation(CodexImageGenerator(s))) == [A, B, C]


def test_重試時第一個帳號撞到額度就記下冷卻():
    with TemporaryDirectory() as d:
        s = _settings(Path(d))
        gen = CodexImageGenerator(s)
        reset = int(time.time()) + 3600
        tried = []

        async def fake_once(*, codex_home=None, **_):
            tried.append(codex_home)
            if len(tried) == 1:
                raise CodexGenerationError("Codex returned no image", stderr=_limit_stderr(reset))
            return "codex exec", "ok", ""

        async def fake_fetch(homes):
            return {}
        gen._run_codex_once = fake_once
        original = chatgpt_usage.fetch_many
        chatgpt_usage.fetch_many = fake_fetch
        try:
            run_dir = Path(d) / "run"; run_dir.mkdir()
            _, _, _, used = asyncio.run(gen._run_codex_with_retry(
                run_dir=run_dir, output_path=run_dir / "out.png", prompt="p", size="1024x1024",
                quality="high", index=0, count=1, reference_paths=[]))
            assert used == tried[1] and tried[0] != tried[1]
            assert codex_image.image_gen_cooldowns(s) == {tried[0]: reset}
            assert tried[0] not in asyncio.run(gen._rotation())
        finally:
            chatgpt_usage.fetch_many = original


def test_codex_正常結束但沒有圖也記下冷卻():
    # .11 實際的樣子（2026-10-23）：Codex 撞到 image_gen 429 照樣 exit 0，
    # 是 generate() 後面「rollout 裡沒有圖」那一步才失敗；#44 只在重試那段記冷卻，部署後 yaze-fr 照樣被派了 18 次。
    with TemporaryDirectory() as d:
        s = _settings(Path(d))
        gen = CodexImageGenerator(s)
        reset = int(time.time()) + 3600
        tried = []

        async def fake_once(*, codex_home=None, **_):
            tried.append(codex_home)
            return "codex exec", "", _limit_stderr(reset)

        async def fake_fetch(homes):
            return {}
        gen._run_codex_once = fake_once
        original = chatgpt_usage.fetch_many
        chatgpt_usage.fetch_many = fake_fetch
        try:
            try:
                asyncio.run(gen.generate(request_id="r1", prompt="p", size="1024x1024", quality="high", count=1))
                raise AssertionError("沒有圖應該失敗")
            except CodexGenerationError as exc:
                assert "額度已用完" in str(exc)
            assert codex_image.image_gen_cooldowns(s) == {tried[0]: reset}
            assert tried[0] not in asyncio.run(gen._rotation())
        finally:
            chatgpt_usage.fetch_many = original
