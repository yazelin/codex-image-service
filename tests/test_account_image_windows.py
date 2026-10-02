"""帳號卡的 Images 5h / 24h / 48h。

存在理由：2026-10-01 五個帳號一起撞 ChatGPT 生圖上限，當時只能手算各帳號
往回幾小時出了幾張。算的是成功請求的張數加總（一筆可出多張），失敗不算。
"""
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from app import db
from app.api.admin import _codex_accounts_section
from app.config import Settings


def _settings(tmp: Path):
    s = Settings(
        admin_username="admin", admin_password="x", admin_session_secret="x",
        admin_url_prefix="", database_url="sqlite:///" + str(tmp / "app.db"),
        generated_dir=tmp / "generated", public_base_url="http://localhost",
        codex_timeout_seconds=5, codex_workdir=tmp, codex_worker_concurrency=1,
        codex_homes=("/h/a",), generation_queue_max_size=1,
        request_wait_timeout_seconds=5, image_retention_days=7, cleanup_interval_hours=6,
    )
    db.init_db(s)
    return s


def _row(s, rid, hours_ago, status, count):
    created = (db.utc_now() - timedelta(hours=hours_ago)).isoformat()
    with db.connect(s) as c:
        c.execute(
            "INSERT INTO image_requests (id, prompt, size, quality, count, status,"
            " created_at, expires_at, codex_home) VALUES (?, 'p', '1024x1024', 'low', ?, ?, ?, ?, '/h/a')",
            (rid, count, status, created, created),
        )


def test_windows_sum_succeeded_images():
    with TemporaryDirectory() as d:
        s = _settings(Path(d))
        _row(s, "a", 1, "succeeded", 2)    # 5h/24h/48h
        _row(s, "b", 1, "failed", 4)       # 失敗不算
        _row(s, "c", 10, "succeeded", 1)   # 24h/48h
        _row(s, "d", 30, "succeeded", 3)   # 48h
        _row(s, "e", 60, "succeeded", 5)   # 都不算
        windows = {
            "5h": db.per_account_stats(s, days=5 / 24),
            "24h": db.per_account_stats(s, days=1),
            "48h": db.per_account_stats(s, days=2),
        }
        html = _codex_accounts_section(("/h/a",), db.per_account_stats(s), image_windows=windows)
        assert "<strong>2</strong><span>Images 5h</span>" in html
        assert "<strong>3</strong><span>Images 24h</span>" in html
        assert "<strong>6</strong><span>Images 48h</span>" in html
