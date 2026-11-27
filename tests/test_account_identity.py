"""帳號卡顯示工作區＋信箱網域，並標出「其實是同一個登入」的資料夾（2026-11-27）。"""
import base64
import json

from app.api import admin


def _jwt(payload: dict) -> str:
    seg = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"x.{seg}.y"


def _home(tmp_path, name, account, user, email):
    home = tmp_path / name
    home.mkdir()
    id_token = _jwt({"email": email, "https://api.openai.com/auth": {"chatgpt_user_id": user}})
    (home / "auth.json").write_text(json.dumps({
        "tokens": {"account_id": account, "id_token": id_token, "access_token": "x.e30.y"},
    }), encoding="utf-8")
    return str(home)


def test_same_login_only_when_workspace_and_user_match(tmp_path):
    a = _home(tmp_path, "ct-year", "438c07f7aaaa", "user-gmail", "me@gmail.com")
    b = _home(tmp_path, "yaze-us", "438c07f7aaaa", "user-gmail", "me@gmail.com")      # 同一個登入
    c = _home(tmp_path, "company", "438c07f7aaaa", "user-company", "me@example.com")  # 同工作區、不同人
    groups = admin._same_login_groups([a, b, c])
    assert groups == {a: ["yaze-us"], b: ["ct-year"]}


def test_card_shows_domain_and_duplicate_chip(tmp_path):
    a = _home(tmp_path, "ct-year", "438c07f7aaaa", "user-gmail", "me@gmail.com")
    stats = {"total": 0, "succeeded": 0, "failed": 0, "last_seen": None}
    card = admin._codex_account_card(a, stats, configured=True, same_as=["yaze-us"])
    assert "account 438c07f7… · gmail.com" in card
    assert "與 yaze-us 是同一個帳號" in card
    plain = admin._codex_account_card(a, stats, configured=True)
    assert "是同一個帳號" not in plain
