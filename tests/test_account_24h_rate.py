"""Overview 每張帳號卡的『24h success』。

存在理由：catime 在 codex 回錯誤時會自動改走 gemini，所以單一 Codex 帳號
整條壞掉時，外面看起來仍然有圖 —— 只有這個數字看得到。
"""
from app.api.admin import _success_rate_24h


def test_rate_all_good():
    assert _success_rate_24h({"succeeded": 8, "failed": 0}) == "100%"


def test_rate_partial():
    assert _success_rate_24h({"succeeded": 3, "failed": 1}) == "75%"


def test_rate_all_failed():
    assert _success_rate_24h({"succeeded": 0, "failed": 6}) == "0%"


def test_no_traffic_is_not_zero_percent():
    # 0/0 顯示 0% 會讓閒置帳號長得跟全滅一樣，比沒有這欄還糟
    assert _success_rate_24h({"succeeded": 0, "failed": 0}) == "—"
    assert _success_rate_24h(None) == "—"
