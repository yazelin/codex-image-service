"""ChatGPT 訂閱用量(5 小時窗 / 週窗)查詢。

跟 codex-auth-switcher 的 `cx usage` 同一個來源:讀 CODEX_HOME 底下 auth.json
的 access_token,打 chatgpt.com 的 wham/usage,取兩個 rate-limit window 的
used_percent。admin 頁要的是「還剩幾 %」,所以這裡直接換算成 remaining。

**只讀,絕不回寫 auth.json。** token 的 refresh 由 codex CLI 在 flock 保護下
進行(見 docker-compose 的 volumes 註解);查用量若順手更新檔案,會撞上
ChatGPT 的 refresh-token reuse 偵測,連帶作廢同一個使用者名下所有 session。
拿不到就回 None,由呼叫端顯示「—」——面板查不到是小事,把帳號弄掛是大事。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

# 每次進 admin 首頁都打三個帳號的外部 API 太吵,而且用量本來就不是秒級變化。
_CACHE_TTL_SECONDS = 120.0
_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}


def _access_token(home_path: str) -> str:
    try:
        data = json.loads((Path(home_path) / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    tokens = data.get("tokens") or {}
    token = tokens.get("access_token")
    return token if isinstance(token, str) else ""


def window_label(seconds: Any) -> str:
    """窗長 → 顯示名稱。

    **不能靠 primary / secondary 的位置判斷是 5 小時還是週。** 2026-08-06 實測
    team 方案:primary_window 的 limit_window_seconds 是 604800(七天)、
    secondary_window 直接是 null。照位置標會把週限畫成 5 小時額度,面板上
    「5h 只剩 4%」看起來等一下就回血,實際上要等三天多。codex-auth-switcher
    的 `cx usage` 目前就是照位置寫死的(bin/cx:638),同一個坑。
    """
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds <= 0:
        return "Quota"
    known = {18000: "5h", 3600: "1h", 10800: "3h", 86400: "24h", 604800: "Weekly"}
    if int(seconds) in known:
        return known[int(seconds)]
    if seconds >= 86400:
        return f"{round(seconds / 86400)}d"
    return f"{round(seconds / 3600)}h"


def parse_window(window: Any) -> dict[str, Any] | None:
    """把一個 rate_limit window 換算成 {label, remaining_percent, reset_at}。

    純函式,好測。used_percent 缺漏或不是數字就回 None(當作查不到),不要
    自己補 0 ——「剩 100%」跟「不知道」在面板上是完全不同的意思。
    """
    if not isinstance(window, dict):
        return None
    used = window.get("used_percent")
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        return None
    reset_at = window.get("reset_at")
    return {
        "label": window_label(window.get("limit_window_seconds")),
        "remaining_percent": max(0, min(100, round(100 - used))),
        "reset_at": reset_at if isinstance(reset_at, (int, float)) else None,
    }


def parse_payload(body: Any) -> dict[str, Any] | None:
    """回傳 {plan, windows, limit_reached};windows 是實際存在的窗,不補空位。"""
    if not isinstance(body, dict):
        return None
    rate_limit = body.get("rate_limit") or {}
    windows = [
        w
        for w in (
            parse_window(rate_limit.get("primary_window")),
            parse_window(rate_limit.get("secondary_window")),
        )
        if w is not None
    ]
    if not windows:
        return None
    return {
        "plan": body.get("plan_type") or "",
        "windows": windows,
        "limit_reached": bool(rate_limit.get("limit_reached")),
    }


async def _fetch_one(client: httpx.AsyncClient, home_path: str) -> dict[str, Any] | None:
    token = _access_token(home_path)
    if not token:
        return None
    try:
        response = await client.get(
            USAGE_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        response.raise_for_status()
        return parse_payload(response.json())
    except Exception:
        # 過期 token、離線、對方改格式 —— 一律當作查不到。admin 首頁不該因為
        # 一個外部端點的狀況而開不起來。
        return None


async def fetch_many(home_paths: list[str]) -> dict[str, dict[str, Any]]:
    """回傳 {home_path: usage},查不到的 home 不會出現在結果裡。"""
    now = time.monotonic()
    result: dict[str, dict[str, Any]] = {}
    stale: list[str] = []
    for home in home_paths:
        cached = _cache.get(home)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            if cached[1]:
                result[home] = cached[1]
        else:
            stale.append(home)

    if stale:
        async with httpx.AsyncClient(timeout=6.0) as client:
            fetched = await asyncio.gather(*(_fetch_one(client, h) for h in stale))
        for home, usage in zip(stale, fetched):
            _cache[home] = (now, usage)
            if usage:
                result[home] = usage
    return result
