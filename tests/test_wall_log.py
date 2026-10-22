"""撞牆紀錄：從 stderr 的 image_gen 429 推回每次撞牆，與撞牆當下往回 24h 的張數。

存在理由：2026-10-22 查出撞牆線不是固定張數（77～296 都有），只能靠每次實際撞到時的數字校正。
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app import db
from app.api.admin import _wall_log, _wall_log_html
from tests.test_usage_watch import _settings

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
_429 = ('ERROR codex_core::tools::router: error=image generation failed: http 429 Too Many Requests: '
        'Some("{\\"error\\":{\\"type\\":\\"usage_limit_reached\\",\\"resets_at\\":%d}}")')


def _row(s, rid, when, home, count=1, error=None, stderr=None, status="succeeded"):
    with db.connect(s) as c:
        c.execute(
            "INSERT INTO image_requests (id, prompt, size, quality, count, status, created_at, finished_at,"
            " expires_at, codex_home, error, stderr) VALUES (?, 'p', '1024x1024', 'low', ?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, count, status, when.isoformat(), when.isoformat(), when.isoformat(), home, error, stderr),
        )


def test_wall_events_and_safe_floor():
    with TemporaryDirectory() as d:
        s = _settings(Path(d))
        _row(s, "old", T0 - timedelta(hours=25), "/h/a", 50, status="expired")  # 窗外
        _row(s, "w1", T0 - timedelta(hours=23), "/h/a", 3, status="expired")    # 過保留期的成功也要算
        _row(s, "w2", T0 - timedelta(hours=1), "/h/a", 4)
        _row(s, "f0", T0 - timedelta(hours=2), "/h/a", 9, error="x", status="failed")  # 失敗不算
        reset = int((T0 + timedelta(hours=1)).timestamp())
        _row(s, "b1", T0, "/h/a", error="limit", stderr=_429 % reset, status="failed")
        _row(s, "b2", T0 + timedelta(minutes=5), "/h/a", error="limit", stderr=_429 % (reset + 1), status="failed")
        # 舊版沒記帳號：兩個帳號的 429 交錯，要合成兩次，不是三次
        _row(s, "u1", T0, None, error="limit", stderr=_429 % reset, status="expired")
        _row(s, "u2", T0 + timedelta(minutes=1), None, error="limit", stderr=_429 % (reset + 7200), status="expired")
        _row(s, "u3", T0 + timedelta(minutes=2), None, error="limit", stderr=_429 % (reset + 1), status="expired")
        _row(s, "n1", T0 + timedelta(hours=2), "/h/a", 7)                        # 解除之後
        _row(s, "n2", T0 + timedelta(hours=3), "/h/a", 2)
        _row(s, "b", T0, "/h/b", 5)

        events, per = _wall_log(s, ["/h/a", "/h/b"], now=T0 + timedelta(hours=3, minutes=1))
        a = [e for e in events if e["home"] == "/h/a"]
        assert len(a) == 1 and a[0]["hits"] == 2 and a[0]["images_24h"] == 7
        assert sorted(e["hits"] for e in events if not e["home"]) == [1, 2]
        assert all(e["images_24h"] is None for e in events if not e["home"])
        assert per["/h/a"] == {"now_24h": 4 + 7 + 2, "max_24h_ok": 9}   # 只算解除之後
        assert per["/h/b"] == {"now_24h": 5, "max_24h_ok": 5}            # 沒撞過就看全部

        page = "".join(_wall_log_html(events, per, ["/h/a", "/h/b"]))
        assert "帳號不明" in page and "<strong>7</strong>" in page and "最高 24h：9 張" in page


def test_no_walls():
    with TemporaryDirectory() as d:
        s = _settings(Path(d))
        assert "還沒撞過牆" in _wall_log_html(*_wall_log(s, ["/h/a"]), ["/h/a"])[1]
