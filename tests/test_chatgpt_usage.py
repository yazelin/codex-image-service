"""帳號卡上的訂閱額度剩餘 %。

存在理由：三個 ChatGPT 帳號輪流出圖，額度快見底時外面完全看不出來 ——
要嘛突然全部改走 fallback，要嘛整批失敗。這幾格是唯一的提前警訊。
"""
from app.api.admin import _quota_cells, _quota_reset_text
from app.services.chatgpt_usage import parse_payload, parse_window, window_label


def test_label_comes_from_window_length_not_position():
    # 2026-08-06 實測：team 方案的 primary_window 是七天窗，secondary 是 null。
    # 照 primary/secondary 的位置標「5h / 週」會把週限畫成 5 小時額度。
    assert window_label(604800) == "Weekly"
    assert window_label(18000) == "5h"
    assert window_label(None) == "Quota"


def test_window_converts_used_to_remaining():
    parsed = parse_window(
        {"used_percent": 30, "limit_window_seconds": 18000, "reset_at": 1754500000}
    )
    assert parsed == {"label": "5h", "remaining_percent": 70, "reset_at": 1754500000}


def test_window_clamps_over_hundred():
    # 超用時 API 會回 >100，剩餘不該變負數
    assert parse_window({"used_percent": 130})["remaining_percent"] == 0


def test_window_missing_used_percent_is_unknown():
    # 補 0 會讓「查不到」長得像「還沒用」，方向剛好相反
    assert parse_window({"reset_at": 1754500000}) is None
    assert parse_window(None) is None


def test_payload_keeps_only_real_windows():
    assert parse_payload({"rate_limit": {}}) is None
    assert parse_payload("nope") is None
    parsed = parse_payload(
        {
            "plan_type": "team",
            "rate_limit": {
                "primary_window": {"used_percent": 96, "limit_window_seconds": 604800},
                "secondary_window": None,
            },
        }
    )
    assert parsed["plan"] == "team"
    assert len(parsed["windows"]) == 1
    assert parsed["windows"][0]["label"] == "Weekly"
    assert parsed["windows"][0]["remaining_percent"] == 4


def test_card_shows_dash_when_usage_unavailable():
    assert "Quota left" in _quota_cells(None)
    assert "—" in _quota_cells({"windows": []})
    assert _quota_reset_text(None) == "quota resets —"


def test_card_renders_one_cell_per_window():
    usage = {
        "windows": [
            {"label": "5h", "remaining_percent": 70, "reset_at": None},
            {"label": "Weekly", "remaining_percent": 4, "reset_at": None},
        ]
    }
    cells = _quota_cells(usage)
    assert "70%" in cells and "5h left" in cells
    assert "4%" in cells and "Weekly left" in cells
