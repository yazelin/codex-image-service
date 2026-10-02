"""Overview 的用量警戒：每個帳號往回 48h 的張數對撞牆線。

存在理由：2026-10-01 撞 ChatGPT 生圖上限，事後分析只有「往回 48 小時累積」
對得上撞牆時間點。這裡驗滑動視窗算對、門檻分級對、頁面上有數字不只靠顏色。
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app import db
from app.api.admin import _rolling_48h, _usage_watch_section, _watch_level
from app.config import Settings


def _settings(tmp: Path):
    s = Settings(
        admin_username="admin", admin_password="x", admin_session_secret="x",
        admin_url_prefix="", database_url="sqlite:///" + str(tmp / "app.db"),
        generated_dir=tmp / "generated", public_base_url="http://localhost",
        codex_timeout_seconds=5, codex_workdir=tmp, codex_worker_concurrency=1,
        codex_homes=("/h/a", "/h/b"), generation_queue_max_size=1,
        request_wait_timeout_seconds=5, image_retention_days=7, cleanup_interval_hours=6,
        image_wall_48h=100,
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
        _row(s, "b", now - timedelta(hours=47), "succeeded", 3)     # 視窗最舊那一小時，還算
        _row(s, "c", now - timedelta(hours=48), "succeeded", 50)    # 剛滑出視窗
        _row(s, "d", now - timedelta(hours=1), "failed", 9)         # 失敗不算
        times, series = _rolling_48h(s, ["/h/a", "/h/b"], now=now)
        assert len(times) == 168 and times[-1] == now.replace(minute=0)
        assert series["/h/a"][-1] == 5
        assert series["/h/a"][-2] == 3 + 50                        # 一小時前，48h 前那筆還在窗內
        assert series["/h/b"] == [0] * 168

        assert _watch_level(0.69)[0] == "lvl-ok"
        assert _watch_level(0.7)[0] == "lvl-warn"
        assert _watch_level(0.9)[0] == "lvl-crit"

        page = _usage_watch_section(s, ["/h/a", "/h/b"])
        assert "<strong>5</strong> / 100 張" in page
        assert "撞牆線 100" in page
        assert page.count("<polyline") == 2
