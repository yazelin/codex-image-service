"""Overview 的用量警戒：每個帳號往回 48h 的張數對撞牆線。

存在理由：2026-10-01 撞 ChatGPT 生圖上限，事後分析只有「往回 48 小時累積」
對得上撞牆時間點。這裡驗滑動視窗算對、門檻分級對、頁面上有數字不只靠顏色。
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app import db
from app.api.admin import _rolling_24h, _usage_watch_section, _watch_level
from app.config import Settings


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


def _row(s, rid, when, status, count, home="/h/a"):
    with db.connect(s) as c:
        c.execute(
            "INSERT INTO image_requests (id, prompt, size, quality, count, status,"
            " created_at, expires_at, codex_home) VALUES (?, 'p', '1024x1024', 'low', ?, ?, ?, ?, ?)",
            (rid, count, status, when.isoformat(), when.isoformat(), home),
        )


def test_rolling_window_and_levels():
    with TemporaryDirectory() as d:
        s = _settings(Path(d))
        now = datetime.now(timezone.utc).replace(minute=30, second=0, microsecond=0)
        _row(s, "a", now - timedelta(minutes=10), "succeeded", 2)   # 這個小時
        _row(s, "b", now - timedelta(hours=23), "expired", 3)       # 視窗最舊那一小時，過保留期也要算
        _row(s, "c", now - timedelta(hours=24), "succeeded", 50)    # 剛滑出視窗
        _row(s, "d", now - timedelta(hours=1), "failed", 9)         # 失敗不算
        times, series = _rolling_24h(s, ["/h/a", "/h/b"], now=now)
        assert len(times) == 168 and times[-1] == now.replace(minute=0)
        assert series["/h/a"][-1] == 5
        assert series["/h/a"][-2] == 3 + 50                        # 一小時前，24h 前那筆還在窗內
        assert series["/h/b"] == [0] * 168

        assert _watch_level(0.69)[0] == "lvl-ok"
        assert _watch_level(0.7)[0] == "lvl-warn"
        assert _watch_level(0.9)[0] == "lvl-crit"

        page = _usage_watch_section(s, ["/h/a", "/h/b"])
        assert "撞牆線" not in page
        assert page.count("<polyline") == 2


def test_wall_dot_on_chart():
    from tests.test_wall_log import _429, _row as wrow
    with TemporaryDirectory() as d:
        s = _settings(Path(d))
        now = datetime.now(timezone.utc)
        wrow(s, "ok", now - timedelta(hours=10), "/h/a", 4)
        reset = int((now + timedelta(hours=14)).timestamp())
        wrow(s, "x", now - timedelta(hours=5), "/h/a", error="limit", stderr=_429 % reset, status="failed")
        page = _usage_watch_section(s, ["/h/a", "/h/b"])
        assert page.count("class='wall-dot'") == 1 and "往回 24h 4 張" in page


def test_quota_rows_show_used_percent_and_unknown():
    from app.api.admin import _quota_watch_rows
    usage = {"/h/a": {"windows": [
        {"label": "5h", "remaining_percent": 98, "reset_at": None},
        {"label": "Weekly", "remaining_percent": 8, "reset_at": None},
    ]}}
    page = _quota_watch_rows(["/h/a", "/h/b"], usage)
    assert "已用 <strong>2%</strong>" in page
    assert "已用 <strong>92%</strong>" in page and "lvl-crit" in page   # 週用了 92% → 危險
    assert "查不到額度" in page                                          # /h/b 查不到，不畫成 0%
