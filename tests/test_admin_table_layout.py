"""表格版面的回歸測試。

History 欄位一多，1180px 的 .content 就塞不下、整片爆出版面（2026-07-27
實際發生）。修法是放寬 + 每張表包一層可橫向捲動的容器，兩件都要在。
"""
from app.api import admin


def test_content_is_wide_enough():
    assert "max-width: 1600px" in admin._STYLES


def test_tables_are_wrapped_for_horizontal_scroll():
    assert ".table-wrap { overflow-x: auto" in admin._STYLES
    opens = admin.__dict__  # noqa: F841  (只是為了讓 import 生效)
    src = open(admin.__file__).read()
    assert src.count("<div class='table-wrap'><table>") == src.count("</tbody></table></div>")
    assert src.count("<div class='table-wrap'><table>") >= 2  # History + API Keys


def test_mode_form_has_its_own_css():
    """沒有自己的規則就會掉進全域 label{display:grid} / select{width:100%}。"""
    assert ".mode-form {" in admin._STYLES
    assert ".mode-form select" in admin._STYLES
    assert ".mode-form label" in admin._STYLES


def test_stats_row_is_not_hardcoded_to_four():
    """Overview 有 5 格（含 Uptime）；寫死 4 欄會把第 5 格擠到下一行。"""
    assert "repeat(auto-fit, minmax(180px, 1fr))" in admin._STYLES
    assert "repeat(4, minmax(0, 1fr))" not in admin._STYLES
