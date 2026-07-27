"""帳號輪替模式（admin 可即時切，存 DB）。

round-robin  = 每筆換下一個帳號，用量平均攤開
primary-first = 固定用第一個，失敗才由 retry 換下一個（把備用帳號額度留著）
"""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from app import db
from app.config import Settings
from app.services import codex_image
from app.services.codex_image import CodexImageGenerator


def _settings(tmp: Path, homes):
    s = Settings(
        admin_username="admin", admin_password="x", admin_session_secret="x",
        admin_url_prefix="", database_url="sqlite:///" + str(tmp / "app.db"),
        generated_dir=tmp / "generated", public_base_url="http://localhost",
        codex_timeout_seconds=5, codex_workdir=tmp, codex_worker_concurrency=1,
        codex_homes=homes, generation_queue_max_size=1,
        request_wait_timeout_seconds=5, image_retention_days=7, cleanup_interval_hours=6,
    )
    db.init_db(s)
    return s


def test_default_is_round_robin():
    with TemporaryDirectory() as d:
        s = _settings(Path(d), ("/h/a", "/h/b"))
        assert codex_image.dispatch_mode(s) == "round-robin"
        gen = CodexImageGenerator(s)
        picks = [asyncio.run(gen._claim_primary_base()) for _ in range(4)]
        assert picks == [0, 1, 0, 1]


def test_primary_first_pins_to_first_account():
    with TemporaryDirectory() as d:
        s = _settings(Path(d), ("/h/a", "/h/b"))
        db.set_setting(s, codex_image.DISPATCH_MODE_KEY, "primary-first")
        assert codex_image.dispatch_mode(s) == "primary-first"
        gen = CodexImageGenerator(s)
        assert [asyncio.run(gen._claim_primary_base()) for _ in range(3)] == [0, 0, 0]
        # 失敗時 retry 仍會換帳號
        assert gen._home_for_attempt(0, 1) == "/h/b"


def test_garbage_mode_falls_back_to_round_robin():
    """壞掉的設定不該讓生圖整條停擺。"""
    with TemporaryDirectory() as d:
        s = _settings(Path(d), ("/h/a", "/h/b"))
        db.set_setting(s, codex_image.DISPATCH_MODE_KEY, "nonsense")
        assert codex_image.dispatch_mode(s) == "round-robin"
