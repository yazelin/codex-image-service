from __future__ import annotations

import html
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db
from app.services import codex_image
from app.models import ImageGenerateRequest
from app.security import constant_equals, create_admin_session, verify_admin_session
from app.services.job_queue import GenerationQueueUnavailable


router = APIRouter()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _prefix(request: Request) -> str:
    return request.app.state.settings.admin_url_prefix or ""


def _url(request: Request, path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return _prefix(request) + path


def _admin_user(request: Request) -> str | None:
    settings = request.app.state.settings
    username = verify_admin_session(
        request.cookies.get("admin_session"),
        settings.admin_session_secret,
    )
    if username != settings.admin_username:
        return None
    return username


def _redirect_login(request: Request) -> RedirectResponse:
    return RedirectResponse(_url(request, "/admin/login"), status_code=303)


# ---------------------------------------------------------------------------
# auth routes
# ---------------------------------------------------------------------------

@router.get("/", include_in_schema=False)
async def root(request: Request) -> RedirectResponse:
    return RedirectResponse(_url(request, "/admin"), status_code=303)


@router.get("/admin/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request):
    if _admin_user(request):
        return RedirectResponse(_url(request, "/admin"), status_code=303)
    return HTMLResponse(_login_layout(_login_form(_prefix(request))))


@router.post("/admin/login", include_in_schema=False)
async def login(request: Request):
    settings = request.app.state.settings
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    if not (
        constant_equals(username, settings.admin_username)
        and constant_equals(password, settings.admin_password)
    ):
        return HTMLResponse(
            _login_layout(
                _login_form(_prefix(request), error="Invalid username or password")
            ),
            status_code=401,
        )

    # "Remember me" just widens the session TTL — 30 days vs. the default
    # 24h — so the admin isn't re-typing the password every day. Not tied to
    # browser password-manager saving, which already works via
    # autocomplete="current-password" on the input.
    remember = str(form.get("remember", "")) == "on"
    ttl_seconds = 30 * 86400 if remember else 86400

    response = RedirectResponse(_url(request, "/admin"), status_code=303)
    # Any session cookie set before the Path scoping fix landed defaulted to
    # Path=/ (root) — it still lives in returning browsers and coexists with
    # the new Path-scoped one (different Path = different cookie in the jar),
    # so the browser sends both and whichever one the server happens to read
    # can be the stale/wrong one. Clear the old root-path cookie explicitly
    # so only the correctly-scoped one survives.
    response.delete_cookie("admin_session", path="/")
    response.set_cookie(
        "admin_session",
        create_admin_session(username, settings.admin_session_secret, ttl_seconds=ttl_seconds),
        httponly=True,
        samesite="lax",
        max_age=ttl_seconds,
        # Without an explicit path, Starlette defaults to "/" — behind the
        # shared ching-tech.ddns.net domain that collides with any other
        # admin webui on the same host (e.g. gemini-web's), since both use
        # the same cookie name. Scope it to this service's own prefix.
        path=_prefix(request) or "/",
    )
    return response


@router.post("/admin/logout", include_in_schema=False)
async def logout(request: Request) -> RedirectResponse:
    response = RedirectResponse(_url(request, "/admin/login"), status_code=303)
    response.delete_cookie("admin_session", path=_prefix(request) or "/")
    return response


# ---------------------------------------------------------------------------
# page routes (GET)
# ---------------------------------------------------------------------------

@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def overview(request: Request):
    if not _admin_user(request):
        return _redirect_login(request)
    return HTMLResponse(_overview_page(request.app.state.settings, _prefix(request)))


@router.get("/admin/keys", response_class=HTMLResponse, include_in_schema=False)
async def keys_page(request: Request):
    if not _admin_user(request):
        return _redirect_login(request)
    # One-shot reveal: read+immediately clear the flash cookie that POST set.
    new_key = request.cookies.get("_new_api_key")
    html_body = _keys_page(request.app.state.settings, _prefix(request), new_api_key=new_key)
    response = HTMLResponse(html_body)
    if new_key:
        # Path must match the one POST set, otherwise the browser won't drop it.
        response.delete_cookie("_new_api_key", path=_url(request, "/admin/keys"))
    return response


@router.get("/admin/test", response_class=HTMLResponse, include_in_schema=False)
async def test_page(request: Request):
    if not _admin_user(request):
        return _redirect_login(request)
    return HTMLResponse(_test_page(request.app.state.settings, _prefix(request)))


@router.get("/admin/requests", response_class=HTMLResponse, include_in_schema=False)
async def requests_page(request: Request):
    if not _admin_user(request):
        return _redirect_login(request)
    return HTMLResponse(_requests_page(request.app.state.settings, _prefix(request)))


# ---------------------------------------------------------------------------
# mutation routes (POST)
# ---------------------------------------------------------------------------

@router.post("/admin/api-keys", include_in_schema=False)
async def create_api_key(request: Request):
    if not _admin_user(request):
        return _redirect_login(request)
    settings = request.app.state.settings
    form = await request.form()
    name = str(form.get("name", ""))
    _, raw_key = db.create_api_key(settings, name)
    # PRG: stash the raw key in a short-lived flash cookie, then redirect.
    # The GET handler reads it once and immediately clears the cookie, so
    # a refresh on the keys page will NOT show the value again.
    response = RedirectResponse(_url(request, "/admin/keys"), status_code=303)
    response.set_cookie(
        "_new_api_key",
        raw_key,
        httponly=True,
        samesite="lax",
        max_age=60,
        path=_url(request, "/admin/keys"),
    )
    return response


@router.post("/admin/api-keys/{key_id}/disable", include_in_schema=False)
async def disable_api_key(request: Request, key_id: str) -> RedirectResponse:
    if not _admin_user(request):
        return _redirect_login(request)
    db.disable_api_key(request.app.state.settings, key_id)
    return RedirectResponse(_url(request, "/admin/keys"), status_code=303)


@router.post("/admin/api-keys/{key_id}/delete", include_in_schema=False)
async def delete_api_key(request: Request, key_id: str) -> RedirectResponse:
    if not _admin_user(request):
        return _redirect_login(request)
    db.delete_api_key(request.app.state.settings, key_id)
    return RedirectResponse(_url(request, "/admin/keys"), status_code=303)


@router.post("/admin/dispatch-mode", include_in_schema=False)
async def set_dispatch_mode(request: Request) -> RedirectResponse:
    """切換帳號輪替模式（存進 DB，重啟後沿用）。比照 gemini-web 的 dispatch mode。"""
    if not _admin_user(request):
        return _redirect_login(request)
    form = await request.form()
    mode = str(form.get("mode", ""))
    if mode in codex_image.DISPATCH_MODES:
        db.set_setting(request.app.state.settings, codex_image.DISPATCH_MODE_KEY, mode)
    return RedirectResponse(_url(request, "/admin"), status_code=303)


@router.post("/admin/image-requests/{request_id}/delete", include_in_schema=False)
async def delete_image_request(request: Request, request_id: str) -> RedirectResponse:
    if not _admin_user(request):
        return _redirect_login(request)
    settings = request.app.state.settings
    row = db.get_image_request(settings, request_id)
    if row:
        for raw_path in row.get("image_paths", []) or []:
            image_path = Path(raw_path)
            if image_path.is_file():
                image_path.unlink(missing_ok=True)
        workdir = row.get("workdir")
        if workdir:
            workdir_path = Path(workdir)
            if workdir_path.is_dir():
                shutil.rmtree(workdir_path, ignore_errors=True)
        db.delete_image_request(settings, request_id)
    return RedirectResponse(_url(request, "/admin/requests"), status_code=303)


@router.post("/admin/test-generate", response_class=HTMLResponse, include_in_schema=False)
async def admin_test_generate(request: Request):
    if not _admin_user(request):
        return _redirect_login(request)
    settings = request.app.state.settings
    form = await request.form()
    api_key_id = str(form.get("api_key_id", "")).strip()
    prompt = str(form.get("prompt", "")).strip()
    size = str(form.get("size", "1024x1024")).strip()
    quality = str(form.get("quality", "low")).strip()
    try:
        count = int(str(form.get("count", "1")).strip())
    except ValueError:
        count = 1

    keys = {key["id"]: key for key in db.list_api_keys(settings)}
    notice: str | None = None
    error: str | None = None
    if not api_key_id or api_key_id not in keys:
        error = "Pick a valid API key."
    elif not keys[api_key_id]["enabled"]:
        error = "That API key is disabled."
    elif not prompt:
        error = "Prompt is required."
    else:
        try:
            payload = ImageGenerateRequest(
                prompt=prompt, size=size, quality=quality, count=count
            )
        except Exception as exc:
            error = f"Invalid request: {exc}"
        else:
            try:
                request_id = await request.app.state.job_queue.submit_only(
                    api_key_id=api_key_id, payload=payload
                )
                notice = (
                    f"Queued <code>{html.escape(request_id)}</code> with key "
                    f"<strong>{html.escape(keys[api_key_id]['name'])}</strong>. "
                    "Open History in 1-3 minutes to see the result."
                )
            except GenerationQueueUnavailable as exc:
                error = str(exc)
    return HTMLResponse(_test_page(settings, _prefix(request), notice=notice, error=error))


@router.post("/admin/cleanup", response_class=HTMLResponse, include_in_schema=False)
async def cleanup(request: Request):
    if not _admin_user(request):
        return _redirect_login(request)
    result = await request.app.state.cleanup.run_once()
    return HTMLResponse(
        _requests_page(request.app.state.settings, _prefix(request), cleanup_result=result)
    )


# ---------------------------------------------------------------------------
# page renderers
# ---------------------------------------------------------------------------

def _format_uptime(seconds: float) -> str:
    """跟 gemini-web 的 Overview 對齊的寫法：1d 4h / 4h 12m / 12m 30s。"""
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _overview_page(settings: Any, prefix: str) -> str:
    from app.main import _START_TIME  # deferred: app.main imports this module

    stats = db.dashboard_stats(settings)
    recent = db.list_image_requests(settings, limit=10)
    per_account = db.per_account_stats(settings, days=30)
    # 30 天的總量會把「今天早上才開始壞」稀釋掉；24h 那欄才抓得到現在的死活。
    per_account_24h = db.per_account_stats(settings, days=1)
    current_mode = codex_image.dispatch_mode(settings)
    homes_configured = getattr(settings, "codex_homes", ()) or ()
    body = f"""
      <div class="page-head">
        <h2>Overview</h2>
        <p class="page-sub">A snapshot of keys, queue depth, and recent jobs.</p>
      </div>
      <section class="stats">
        <div><strong>{stats['api_key_count']}</strong><span>Total keys</span></div>
        <div><strong>{stats['active_key_count']}</strong><span>Active keys</span></div>
        <div><strong>{stats['request_count']}</strong><span>Requests</span></div>
        <div><strong>{stats['queued_count']}</strong><span>Queued / running</span></div>
        <div><strong>{_format_uptime(time.time() - _START_TIME)}</strong><span>Uptime</span></div>
      </section>
      {_codex_accounts_section(homes_configured, per_account, per_account_24h, prefix, current_mode)}
      <section>
        <div class="section-title">
          <h2>Recent activity</h2>
          <a class="link" href="{prefix}/admin/requests">View all →</a>
        </div>
        {_activity_feed(recent, prefix)}
      </section>
    """
    return _shell("Overview", "overview", prefix, body)


_DISPATCH_MODE_LABELS = {
    "round-robin": "Round-robin — 每筆請求換下一個帳號，用量平均攤開",
    "primary-first": "Primary-first — 固定用第一個帳號，失敗才換下一個",
}


def _dispatch_mode_form(prefix: str, current: str) -> str:
    opts = "".join(
        f"<option value='{m}'{' selected' if m == current else ''}>{html.escape(label)}</option>"
        for m, label in _DISPATCH_MODE_LABELS.items()
    )
    return (
        f"<form method='post' action='{prefix}/admin/dispatch-mode' class='mode-form'>"
        f"<label>Dispatch mode <select name='mode'>{opts}</select></label> "
        "<button type='submit'>Apply</button></form>"
    )


def _codex_accounts_section(
    homes_configured: tuple[str, ...],
    per_account: list[dict[str, Any]],
    per_account_24h: list[dict[str, Any]] | None = None,
    prefix: str = "",
    current_mode: str = "round-robin",
) -> str:
    """Render a card per Codex account in use, with usage stats + auth health.

    Always renders if there's at least one accessible auth.json — even in
    single-account mode, so operators can see token expiry / refresh state
    without first setting up multi-account.
    """
    # Effective homes: CODEX_HOMES list when set; otherwise the container
    # default $HOME/.codex (which our docker-compose mounts from the host).
    effective_homes: list[str] = list(homes_configured)
    if not effective_homes:
        default = str(Path.home() / ".codex")
        if Path(default, "auth.json").is_file():
            effective_homes.append(default)

    has_data = any(row.get("codex_home") for row in per_account)
    if not effective_homes and not has_data:
        return ""

    by_path = {row["codex_home"]: row for row in per_account}
    by_path_24h = {row["codex_home"]: row for row in (per_account_24h or [])}

    cards = []
    seen: set[str] = set()
    for home in effective_homes:
        seen.add(home)
        stats = by_path.get(home, {"total": 0, "succeeded": 0, "failed": 0, "last_seen": None})
        cards.append(_codex_account_card(home, stats, configured=True,
                                         stats_24h=by_path_24h.get(home)))

    # also show any historical homes that appeared in DB but aren't currently configured
    for row in per_account:
        h = row.get("codex_home") or ""
        if h and h not in seen:
            cards.append(_codex_account_card(h, row, configured=False,
                                             stats_24h=by_path_24h.get(h)))

    multi = len(effective_homes) > 1
    subtitle = (
        "last 30 days · round-robin between accounts"
        if multi
        else "last 30 days · single-account mode"
    )

    mode_form = _dispatch_mode_form(prefix, current_mode) if multi else ""
    return f"""
      <section>
        <div class="section-title">
          <h2>Codex accounts</h2>
          <span class="muted" style="font-size: 13px">{subtitle}</span>
          {mode_form}
        </div>
        <div class="account-grid">{''.join(cards)}</div>
      </section>
    """


def _decode_access_token_exp(access_token: str) -> datetime | None:
    """Pull the `exp` claim out of the access_token JWT and return it as UTC
    datetime. None on any parse error (corrupt token, padding mismatch, etc.)
    so the card can fall back to last_refresh-based heuristics."""
    if not access_token or "." not in access_token:
        return None
    try:
        import base64 as _b64
        import json as _json
        payload_b64 = access_token.split(".")[1]
        # JWT base64 segments often omit padding; restore it.
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = _json.loads(_b64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)):
            return None
        return datetime.fromtimestamp(int(exp), tz=timezone.utc)
    except Exception:
        return None


def _success_rate_24h(stats: dict[str, Any] | None) -> str:
    """近 24 小時成功率。沒流量顯示 —，不是 0%（0/0 誤報成掛掉比沒有這欄還糟）。

    存在理由跟 gemini-web 那張表一樣：消費端（catime）在 codex 回錯誤時會自動
    改走 gemini，所以單一帳號整條壞掉時，外面看起來仍然「有圖」，只有這裡看得到。
    """
    if not stats:
        return "—"
    ok = int(stats.get("succeeded") or 0)
    failed = int(stats.get("failed") or 0)
    total = ok + failed
    if total == 0:
        return "—"
    return f"{round(100 * ok / total)}%"


def _codex_account_card(
    home_path: str,
    stats: dict[str, Any],
    *,
    configured: bool,
    stats_24h: dict[str, Any] | None = None,
) -> str:
    """One CODEX_HOME → one card. Reads auth.json for last_refresh, account_id,
    and the access_token's actual JWT `exp`. Health chip + expiry text are
    driven by the real `exp` when we can parse it; falls back to
    last_refresh + 10d heuristic for corrupt or unreadable tokens."""
    import json as _json
    label = _short_home_label(home_path) or home_path
    total = int(stats.get("total") or 0)
    succeeded = int(stats.get("succeeded") or 0)
    failed = int(stats.get("failed") or 0)
    last_seen = stats.get("last_seen")

    auth_status = "<span class='chip chip-mute'>auth.json not found</span>"
    last_refresh_str = ""
    expires_str = "expires —"
    account_hint = ""
    try:
        auth_path = Path(home_path) / "auth.json"
        if auth_path.is_file():
            data = _json.loads(auth_path.read_text(encoding="utf-8"))
            last_refresh = data.get("last_refresh") or ""
            tokens = data.get("tokens") or {}
            account_id = tokens.get("account_id") or ""
            access_token = tokens.get("access_token") or ""

            if last_refresh:
                last_refresh_str = _relative_time(last_refresh)

            # Prefer the access_token's real JWT exp claim — it's the actual
            # ground truth. Falls back to last_refresh + 10d guess if exp is
            # unparseable for any reason.
            exp_dt = _decode_access_token_exp(access_token)
            now = datetime.now(timezone.utc)
            if exp_dt is not None:
                seconds_left = (exp_dt - now).total_seconds()
                days_left = seconds_left / 86400
                if seconds_left < 0:
                    auth_status = "<span class='chip chip-fail'>expired</span>"
                    expires_str = f"expired {_relative_time(exp_dt.isoformat())}"
                elif days_left < 1:
                    auth_status = "<span class='chip chip-fail'>expires soon</span>"
                    expires_str = f"expires in {int(seconds_left // 3600)}h"
                elif days_left < 3:
                    auth_status = "<span class='chip chip-queue'>refresh soon</span>"
                    expires_str = f"expires in {days_left:.1f}d"
                else:
                    auth_status = "<span class='chip chip-ok'>healthy</span>"
                    expires_str = f"expires in {days_left:.1f}d"
            elif last_refresh:
                # Fallback: estimate from last_refresh + 10d window
                try:
                    ts = datetime.fromisoformat(last_refresh.replace("Z", "+00:00"))
                    age_days = (now - ts).days
                    if age_days > 9:
                        auth_status = "<span class='chip chip-fail'>token may be expired</span>"
                    elif age_days > 6:
                        auth_status = "<span class='chip chip-queue'>refresh soon</span>"
                    else:
                        auth_status = "<span class='chip chip-ok'>healthy</span>"
                    expires_str = f"~{max(0, 10 - age_days)}d remaining (estimated)"
                except ValueError:
                    auth_status = "<span class='chip chip-mute'>refresh time unreadable</span>"

            if account_id:
                account_hint = f"<code class='handle'>account {account_id[:8]}…</code>"
    except Exception:
        auth_status = "<span class='chip chip-fail'>auth.json read error</span>"

    status_chip = (
        "<span class='chip chip-ok'>configured</span>"
        if configured
        else "<span class='chip chip-mute'>historical</span>"
    )

    return f"""
      <div class="account-card">
        <div class="account-head">
          <strong class='key-name'>{html.escape(label)}</strong>
          {status_chip}
        </div>
        <div class="account-meta">{account_hint or '&nbsp;'}</div>
        <div class="account-stats">
          <div><strong>{total}</strong><span>Requests</span></div>
          <div><strong>{succeeded}</strong><span>Succeeded</span></div>
          <div><strong>{failed}</strong><span>Failed</span></div>
          <div><strong>{_success_rate_24h(stats_24h)}</strong><span>24h success</span></div>
        </div>
        <div class="account-footer">
          <span>Auth: {auth_status}</span>
          <span>{html.escape(expires_str)}</span>
          <span>{'last_refresh ' + last_refresh_str if last_refresh_str else 'last_refresh —'}</span>
          <span>{'last_used ' + _relative_time(last_seen) if last_seen else 'last_used —'}</span>
        </div>
      </div>
    """


def _keys_page(settings: Any, prefix: str, new_api_key: str | None = None) -> str:
    keys = db.list_api_keys(settings)
    notice = ""
    if new_api_key:
        notice = (
            "<div class='notice notice-prominent'>"
            "<strong>New API key created.</strong> Copy it now — refresh or "
            "leave this page and the raw value is gone forever "
            "(only the sha256 hash stays on the server)."
            "<div class='key-reveal-row'>"
            f"<code class='key-reveal' id='new-key-value'>{html.escape(new_api_key)}</code>"
            "<button class='copy-btn' type='button' data-copy-target='new-key-value'>Copy</button>"
            "</div>"
            "</div>"
        )
    body = f"""
      <div class="page-head">
        <h2>API Keys</h2>
        <p class="page-sub">Issue bearer keys for each caller. The raw <code>cimg_&lt;random-token&gt;</code> value is only shown once at creation; the server stores a sha256 hash.</p>
      </div>
      {notice}
      <section>
        <h2>Create a new key</h2>
        <form class="inline" method="post" action="{prefix}/admin/api-keys">
          <input name="name" placeholder="Caller / project name (e.g. catime-gh-actions)" required>
          <button type="submit">Create key</button>
        </form>
      </section>
      <section>
        <div class="section-title">
          <h2>All keys</h2>
        </div>
        <p class="muted" style="margin: -4px 0 14px; font-size: 13px;">
          <strong>Heads up:</strong> the <em>Handle</em> column below is the admin reference
          ID (<code>key_&lt;last-12-chars&gt;</code>), <strong>not</strong> the bearer key.
          Callers must use the original <code>cimg_&lt;random-token&gt;</code> from creation time.
        </p>
        {_api_keys_table(keys, prefix)}
      </section>
    """
    return _shell("API Keys", "keys", prefix, body)


def _test_page(
    settings: Any,
    prefix: str,
    notice: str | None = None,
    error: str | None = None,
) -> str:
    keys = db.list_api_keys(settings)
    notice_html = f"<div class='notice'>{notice}</div>" if notice else ""
    error_html = f"<div class='error'>{html.escape(error)}</div>" if error else ""
    body = f"""
      <div class="page-head">
        <h2>Test image generation</h2>
        <p class="page-sub">Queue a real generation against any enabled key — tracked under that key's usage stats.</p>
      </div>
      {notice_html}
      {error_html}
      <section>
        {_test_generate_form(keys, prefix)}
      </section>
    """
    return _shell("Test image", "test", prefix, body)


def _requests_page(
    settings: Any,
    prefix: str,
    cleanup_result: Any | None = None,
) -> str:
    requests = db.list_image_requests(settings, limit=200)
    cleanup_html = ""
    if cleanup_result:
        errors = cleanup_result.errors or []
        message = (
            f"Cleanup complete — expired requests: {cleanup_result.expired_requests}, "
            f"deleted files: {cleanup_result.deleted_files}, "
            f"deleted workdirs: {cleanup_result.deleted_workdirs}."
        )
        if errors:
            message += " Errors: " + html.escape("; ".join(errors))
        cleanup_html = f"<div class='notice'>{message}</div>"
    body = f"""
      <div class="page-head">
        <h2>History</h2>
        <p class="page-sub">Every generation request, with status, prompt, and (if failed) Codex stderr.</p>
      </div>
      {cleanup_html}
      <section>
        <div class="section-title">
          <h2>Image requests</h2>
          <form method="post" action="{prefix}/admin/cleanup"><button class="ghost" type="submit">Run cleanup</button></form>
        </div>
        {_requests_table(requests, settings)}
      </section>
    """
    return _shell("History", "requests", prefix, body)


# ---------------------------------------------------------------------------
# component helpers
# ---------------------------------------------------------------------------

def _status_chip(status: str) -> str:
    cls = {
        "succeeded": "chip chip-ok",
        "running":   "chip chip-run",
        "queued":    "chip chip-queue",
        "failed":    "chip chip-fail",
        "expired":   "chip chip-mute",
    }.get(status, "chip chip-mute")
    return f"<span class='{cls}'>{html.escape(status)}</span>"


def _relative_time(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return html.escape(iso)
    now = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = (now - ts).total_seconds()
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _activity_feed(requests: list[dict[str, Any]], prefix: str) -> str:
    if not requests:
        return "<p class='muted'>No requests yet. Generate one via <a href='" + prefix + "/admin/test'>Test image</a>.</p>"
    items = []
    for item in requests:
        when = _relative_time(item.get("created_at"))
        key_name = html.escape(item.get("api_key_name") or "—")
        prompt_short = (item.get("prompt") or "")
        if len(prompt_short) > 80:
            prompt_short = prompt_short[:77] + "..."
        items.append(
            "<li class='activity-item'>"
            f"<div class='activity-row'>{_status_chip(item['status'])}"
            f"<code class='activity-id'>{html.escape(item['id'])}</code>"
            f"<span class='activity-time'>{when}</span></div>"
            f"<div class='activity-meta'>key: {key_name} · {html.escape(prompt_short)}</div>"
            "</li>"
        )
    return "<ul class='activity'>" + "".join(items) + "</ul>"


def _test_generate_form(keys: list[dict[str, Any]], prefix: str) -> str:
    enabled_keys = [k for k in keys if k["enabled"]]
    if not enabled_keys:
        return (
            "<p class='muted'>No enabled keys yet. "
            f"<a href='{prefix}/admin/keys'>Create one →</a></p>"
        )
    options = "".join(
        f"<option value='{html.escape(k['id'])}'>{html.escape(k['name'])} ({html.escape(k['id'])})</option>"
        for k in enabled_keys
    )
    return f"""
      <form method="post" action="{prefix}/admin/test-generate" class="form-grid">
        <label>API key
          <select name="api_key_id" required>{options}</select>
        </label>
        <label>Prompt
          <textarea name="prompt" rows="3" required placeholder="A minimalist orange tabby cat clock face on white"></textarea>
        </label>
        <div class="form-row-3">
          <label>Size
            <select name="size">
              <option>1024x1024</option>
              <option>1024x1536</option>
              <option>1536x1024</option>
            </select>
          </label>
          <label>Quality
            <select name="quality">
              <option value="low" selected>low (fastest)</option>
              <option value="medium">medium</option>
              <option value="high">high</option>
              <option value="auto">auto</option>
            </select>
          </label>
          <label>Count
            <input name="count" type="number" min="1" max="4" value="1">
          </label>
        </div>
        <div>
          <button type="submit">Queue test job</button>
        </div>
      </form>
    """


def _api_keys_table(keys: list[dict[str, Any]], prefix: str) -> str:
    rows = []
    for key in keys:
        enabled = (
            "<span class='chip chip-ok'>enabled</span>"
            if key["enabled"]
            else "<span class='chip chip-mute'>disabled</span>"
        )
        action_forms = []
        if key["enabled"]:
            action_forms.append(
                f"<form method='post' action='{prefix}/admin/api-keys/{html.escape(key['id'])}/disable' style='display:inline'>"
                "<button class='ghost' type='submit'>Disable</button></form>"
            )
        action_forms.append(
            f"<form method='post' action='{prefix}/admin/api-keys/{html.escape(key['id'])}/delete' style='display:inline;margin-left:6px'"
            " onsubmit=\"return confirm('Delete this API key permanently? History rows stay but the key can no longer authenticate.');\">"
            "<button class='danger' type='submit'>Delete</button></form>"
        )
        action = "".join(action_forms)
        key_id_esc = html.escape(key['id'])
        rows.append(
            "<tr>"
            f"<td><code class='handle'>{key_id_esc}</code></td>"
            f"<td><strong class='key-name'>{html.escape(key['name'])}</strong></td>"
            f"<td>{enabled}</td>"
            f"<td>{html.escape(str(key['requests_count']))}</td>"
            f"<td>{_relative_time(key['last_used_at']) or '—'}</td>"
            f"<td class='actions'>{action}</td>"
            "</tr>"
        )
    if not rows:
        rows.append(
            "<tr><td colspan='6' class='empty'>"
            "No API keys yet. Use the form above to create your first one.</td></tr>"
        )
    return (
        "<div class='table-wrap'><table><thead><tr>"
        "<th title='Admin reference ID — not the bearer key. The bearer key (cimg_*) was only shown once at creation.'>Handle</th>"
        "<th>Name</th><th>Status</th><th>Requests</th>"
        "<th>Last used</th><th>Action</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _requests_table(requests: list[dict[str, Any]], settings: Any) -> str:
    prefix = (getattr(settings, "admin_url_prefix", "") or "").rstrip("/")
    rows = []
    for item in requests:
        links = []
        for raw_path in item.get("image_paths", []):
            path = Path(raw_path)
            links.append(
                f"<a href='{prefix}/generated/{html.escape(path.name)}' target='_blank'>image</a>"
            )
        error = item.get("error") or ""
        if len(error) > 180:
            error = error[:177] + "..."
        # 摘要就把重點露出來，不用每筆都點開 <details>：prompt 看得出是哪一張、
        # error 看得出是哪一類失敗（逾時 / 拒絕 / 重複圖），要全文再展開。
        prompt_full = item.get("prompt") or ""
        prompt_peek = _peek(prompt_full, 90)
        error_peek = _peek(error, 90)
        delete_form = (
            f"<form method='post' action='{prefix}/admin/image-requests/"
            f"{html.escape(item['id'])}/delete' style='display:inline'"
            " onsubmit=\"return confirm('Delete this image, workdir, and history row?');\">"
            "<button class='danger' type='submit'>Delete</button>"
            "</form>"
        )
        codex_home = item.get("codex_home") or ""
        home_label = _short_home_label(codex_home) if codex_home else "—"
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(item['id'])}</code></td>"
            f"<td>{_status_chip(item['status'])}</td>"
            f"<td>{html.escape(item.get('api_key_name') or '—')}</td>"
            f"<td title='{html.escape(codex_home)}'>{html.escape(home_label)}</td>"
            f"<td>{_relative_time(item['created_at'])}</td>"
            f"<td>{_relative_time(item['expires_at'])}</td>"
            f"<td>{', '.join(links) or '—'}</td>"
            f"<td class='cell-peek'><details><summary>{html.escape(prompt_peek) or 'Prompt'}</summary>"
            f"<pre>{html.escape(prompt_full)}</pre></details></td>"
            f"<td class='cell-peek'>" + (
                f"<details><summary class='summary-error'>{html.escape(error_peek)}</summary>"
                f"<pre>{html.escape(error)}</pre></details>" if error else "—"
            ) + "</td>"
            f"<td>{delete_form}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='10' class='empty'>No image requests yet.</td></tr>")
    return (
        "<div class='table-wrap'><table><thead><tr><th>ID</th><th>Status</th><th>Key</th>"
        "<th title='Which CODEX_HOME (ChatGPT account) ran this request'>Account</th>"
        "<th>Created</th>"
        "<th title='Auto-deleted after this time by the scheduled cleanup'>Expires</th>"
        "<th>Images</th><th>Prompt</th><th>Error</th><th>Action</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _peek(text: str, limit: int) -> str:
    """摘要用的單行預覽：把換行壓掉再截斷，太長補省略號。"""
    one_line = " ".join((text or "").split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 1] + "…"


def _short_home_label(home_path: str) -> str:
    """Pull the last meaningful path segment for display in tables.

    `/host_codex_homes/personal` → `personal`
    `/root/.codex`              → `.codex` (single-account default)
    """
    if not home_path:
        return ""
    return Path(home_path.rstrip("/")).name or home_path


def _login_form(prefix: str, error: str | None = None) -> str:
    error_html = f"<div class='error'>{html.escape(error)}</div>" if error else ""
    return f"""
    <div class="login">
      <div class="login-brand">
        <span class="brand-mark brand-mark-lg">✽</span>
        <div class="login-brand-text">
          <div class="login-brand-name">Codex Image Service</div>
          <div class="login-brand-sub">Admin sign-in</div>
        </div>
      </div>
      {error_html}
      <form method="post" action="{prefix}/admin/login">
        <label>Username<input name="username" autocomplete="username" required></label>
        <label>Password<input type="password" name="password" autocomplete="current-password" required></label>
        <label class="remember-row"><input type="checkbox" name="remember"> Remember me for 30 days</label>
        <button type="submit" style="width: 100%">Sign in</button>
      </form>
    </div>
    """


# ---------------------------------------------------------------------------
# layouts (shell with sidebar; minimal login shell)
# ---------------------------------------------------------------------------

NAV_ITEMS = [
    ("overview", "Overview",   "⌂", "/admin"),
    ("keys",     "API Keys",   "⌘", "/admin/keys"),
    ("test",     "Test image", "▸", "/admin/test"),
    ("requests", "History",    "☰", "/admin/requests"),
]


def _sidebar(current_nav: str, prefix: str) -> str:
    items = []
    for slug, label, ico, path in NAV_ITEMS:
        active = " active" if slug == current_nav else ""
        items.append(
            f"<a class='nav-item{active}' href='{prefix}{path}'>"
            f"<span class='nav-ico'>{ico}</span><span>{label}</span>"
            "</a>"
        )
    return (
        "<aside class='sidebar'>"
        f"<nav class='nav'>{''.join(items)}</nav>"
        "</aside>"
    )


def _shell(title: str, current_nav: str, prefix: str, body: str) -> str:
    return _base_layout(
        title,
        f"""
        <header class='topbar'>
          <div class='brand'>
            <span class='brand-mark'>✽</span>
            <span class='brand-name'>Codex Image Service</span>
          </div>
          <div class='topbar-actions'>
            <span class='user-chip'>admin</span>
            <form method='post' action='{prefix}/admin/logout'>
              <button class='ghost' type='submit'>Logout</button>
            </form>
          </div>
        </header>
        <div class='layout'>
          {_sidebar(current_nav, prefix)}
          <main class='content'>{body}</main>
        </div>
        """,
    )


def _login_layout(body: str) -> str:
    return _base_layout("Admin Login", body)


def _base_layout(title: str, body: str) -> str:
    return f"""
    <!doctype html>
    <html lang="zh-Hant">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>{html.escape(title)}</title>
      <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%23fda4af'/%3E%3Ctext x='50%25' y='58%25' text-anchor='middle' font-size='34' fill='white' font-family='ui-sans-serif,system-ui'%3E%E2%9C%BD%3C/text%3E%3C/svg%3E">
      <style>{_STYLES}</style>
    </head>
    <body>{body}
      <script>{_COPY_SCRIPT}</script>
    </body>
    </html>
    """


_COPY_SCRIPT = """
document.addEventListener('click', function(e) {
  const btn = e.target.closest('.copy-btn');
  if (!btn) return;
  let value = btn.getAttribute('data-copy-value');
  if (!value) {
    const targetId = btn.getAttribute('data-copy-target');
    if (targetId) {
      const el = document.getElementById(targetId);
      if (el) value = el.textContent.trim();
    }
  }
  if (!value) return;
  const done = () => {
    const original = btn.textContent;
    btn.textContent = btn.classList.contains('copy-btn-mini') ? '✓' : 'Copied ✓';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = original; btn.classList.remove('copied'); }, 1400);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(value).then(done).catch(() => {
      // fallback below
      const ta = document.createElement('textarea');
      ta.value = value; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); done(); } finally { document.body.removeChild(ta); }
    });
  } else {
    const ta = document.createElement('textarea');
    ta.value = value; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); done(); } finally { document.body.removeChild(ta); }
  }
});
"""


_STYLES = """
  :root {
    color-scheme: light;
    --ink: #2d3142;
    --ink-soft: #4a5072;
    --muted: #7d839b;
    --bg-1: #fdf8f3;
    --bg-2: #fff1f2;
    --bg-3: #eef2ff;
    --accent-1: #fda4af;
    --accent-2: #a5b4fc;
    --accent-3: #86efac;
    --accent-4: #fcd34d;
    --card: #ffffffcc;
    --card-edge: #f0e3e5;
    --code-bg: #1e2336;
    --code-ink: #e9ecf8;
    --danger: #e11d48;
    --shadow: 0 6px 30px -8px rgba(120,60,80,.18);
    --shadow-sm: 0 2px 8px -2px rgba(120,60,80,.10);
    --radius: 18px;
    --sidebar-w: 232px;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    font: 15px/1.6 "Noto Sans TC", Inter, ui-sans-serif, system-ui, "PingFang TC", "Helvetica Neue", sans-serif;
    color: var(--ink);
    background:
      radial-gradient(ellipse 1200px 600px at 10% -10%, var(--bg-2) 0%, transparent 60%),
      radial-gradient(ellipse 1100px 500px at 95% 5%, var(--bg-3) 0%, transparent 55%),
      var(--bg-1);
    min-height: 100vh;
    -webkit-font-smoothing: antialiased;
  }
  h1 { font-size: 26px; font-weight: 700; letter-spacing: -0.02em; margin: 0; color: var(--ink); }
  h2 { font-size: 18px; font-weight: 700; letter-spacing: -0.01em; margin: 0 0 16px; color: var(--ink); }
  p { margin: 0 0 12px; color: var(--ink-soft); }
  p.muted { color: var(--muted); }
  a { color: var(--accent-1); text-decoration: none; }
  a:hover { text-decoration: underline; }
  a.link { font-size: 13.5px; font-weight: 500; }

  /* ---- topbar ---- */
  .topbar {
    position: sticky; top: 0; z-index: 5;
    display: flex; justify-content: space-between; align-items: center;
    padding: 14px 24px;
    background: #ffffffd8;
    backdrop-filter: blur(10px);
    border-bottom: 1px solid var(--card-edge);
  }
  .brand { display: flex; align-items: center; gap: 10px; }
  .brand-mark {
    width: 30px; height: 30px; border-radius: 9px;
    background: var(--accent-1); color: white;
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 17px;
  }
  .brand-name { font-weight: 700; font-size: 16px; letter-spacing: -0.01em; }
  .topbar-actions { display: flex; align-items: center; gap: 12px; }
  .user-chip {
    padding: 6px 12px; border-radius: 999px;
    background: #fff0f1; color: var(--ink-soft);
    font-size: 13px; font-weight: 500;
  }

  /* ---- layout ---- */
  .layout {
    display: grid;
    grid-template-columns: var(--sidebar-w) 1fr;
    gap: 0;
    min-height: calc(100vh - 60px);
  }
  .sidebar {
    border-right: 1px solid var(--card-edge);
    background: #ffffff9c;
    backdrop-filter: blur(10px);
    padding: 20px 14px;
  }
  .nav { display: flex; flex-direction: column; gap: 4px; }
  .nav-item {
    display: flex; align-items: center; gap: 12px;
    padding: 10px 14px; border-radius: 12px;
    color: var(--ink-soft);
    font-size: 14px; font-weight: 500;
    text-decoration: none;
    transition: background .12s ease, color .12s ease;
    border-left: 3px solid transparent;
  }
  .nav-item:hover { background: #fff7f8; color: var(--ink); text-decoration: none; }
  .nav-item.active {
    background: #ffe9eb;
    color: var(--ink);
    border-left-color: var(--accent-1);
    font-weight: 600;
  }
  .nav-ico {
    width: 22px; height: 22px; border-radius: 7px;
    background: #fff0f1; color: var(--accent-1);
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 13px;
  }
  .nav-item.active .nav-ico { background: var(--accent-1); color: white; }

  /* History 欄位多，1180px 塞不下就整片爆出版面。放寬到 1600，仍不夠時由
     .table-wrap 接手橫向捲動。 */
  .content { padding: 32px 36px 64px; max-width: 1600px; }
  .page-head { margin-bottom: 24px; }
  .page-head h2 { font-size: 26px; margin: 0 0 6px; }
  .page-sub { margin: 0; color: var(--muted); font-size: 14.5px; }

  /* ---- card sections ---- */
  section {
    background: var(--card);
    backdrop-filter: blur(8px);
    border: 1px solid var(--card-edge);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    padding: 24px 26px;
    margin: 0 0 22px;
  }

  /* ---- stats ---- */
  .stats {
    display: grid;
    /* 寫死 4 欄的話，第 5 格（Uptime）會被擠到下一行自己佔滿整排。改成
       auto-fit：容器放寬後五格一列排得下，以後增減格子也不必再改這裡。 */
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 14px;
    background: transparent;
    border: 0;
    backdrop-filter: none;
    box-shadow: none;
    padding: 0;
    margin: 0 0 22px;
  }
  .stats > div {
    background: white;
    border: 1px solid var(--card-edge);
    border-radius: 14px;
    padding: 18px 20px;
    box-shadow: var(--shadow-sm);
  }
  .stats strong { display: block; font-size: 30px; font-weight: 700; color: var(--ink); line-height: 1.1; }
  .stats span { color: var(--muted); font-size: 13px; }

  /* ---- section title row ---- */
  .section-title { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 16px; }
  .section-title h2 { margin: 0; }

  /* 表格一律包一層可橫向捲動的容器：欄位再多也只有表格自己捲，版面不會被撐破 */
  .table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .table-wrap table { min-width: 100%; }

  /* Dispatch mode 切換器塞在 section 標題列，別把標題撐開 */
  .mode-form { display: flex; align-items: center; gap: 10px; margin: 0; }
  .mode-form label { display: flex; align-items: center; gap: 8px; margin: 0; white-space: nowrap; }
  .mode-form select { width: auto; min-width: 260px; }
  .mode-form button { padding: 6px 16px; font-size: 12.5px; box-shadow: none; }

  /* ---- inline form (single input + button) ---- */
  .inline { display: flex; gap: 10px; align-items: stretch; }
  .inline input { flex: 1; }

  /* ---- multi-field form (test page) ---- */
  .form-grid { display: grid; gap: 14px; max-width: 720px; }
  .form-row-3 { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }

  /* ---- inputs ---- */
  input, select, textarea {
    width: 100%;
    padding: 11px 14px;
    border: 1px solid var(--card-edge);
    border-radius: 10px;
    font: inherit;
    color: var(--ink);
    background: white;
    transition: border-color .12s ease, box-shadow .12s ease;
  }
  input:focus, select:focus, textarea:focus {
    outline: none;
    border-color: var(--accent-1);
    box-shadow: 0 0 0 3px #fda4af33;
  }
  textarea { resize: vertical; min-height: 80px; font-family: inherit; }
  label {
    display: grid; gap: 6px;
    margin: 0;
    font-size: 13px;
    color: var(--ink-soft);
    font-weight: 500;
  }

  /* ---- buttons ---- */
  button {
    padding: 10px 22px;
    border: 0; border-radius: 999px;
    background: var(--accent-1); color: white;
    font: inherit; font-size: 14px; font-weight: 600;
    cursor: pointer; white-space: nowrap;
    box-shadow: var(--shadow-sm);
    transition: transform .12s ease, filter .12s ease;
  }
  button:hover { transform: translateY(-1px); }
  button:focus-visible { outline: 2px solid var(--accent-2); outline-offset: 2px; }
  button.danger { background: var(--danger); }
  button.danger:hover { filter: brightness(1.08); }
  button.ghost {
    background: white; color: var(--ink-soft);
    border: 1px solid var(--card-edge); box-shadow: none;
  }
  button.ghost:hover { background: #fff8f9; }

  /* ---- table ---- */
  table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
  th, td {
    padding: 12px 12px;
    border-bottom: 1px solid #f3e8eb;
    text-align: left; vertical-align: top;
  }
  th {
    color: var(--muted); font-weight: 600;
    font-size: 11.5px;
    text-transform: uppercase;
    letter-spacing: .04em;
  }
  tr:last-child td { border-bottom: 0; }
  tbody tr:hover { background: #fff8f9; }
  td.actions { white-space: nowrap; }
  td.empty { text-align: center; color: var(--muted); padding: 32px 12px; }
  /* 表格裡的按鈕用小一號的尺寸：原本沿用全域 pill(10px 22px / 14px)，在
     13.5px 的表格裡整整佔掉兩行高，而那一格只有一顆按鈕，空的那行純浪費。 */
  td button { padding: 5px 14px; font-size: 12.5px; box-shadow: none; }
  td form { margin: 0; }

  /* History 的 Prompt / Error 欄：摘要行直接顯示前 90 字，要全文再展開 */
  td.cell-peek { max-width: 320px; }
  td.cell-peek summary {
    cursor: pointer; color: var(--ink-soft);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  td.cell-peek summary:hover { color: var(--ink); }
  td.cell-peek summary.summary-error { color: var(--danger); }
  td.cell-peek pre {
    white-space: pre-wrap; word-break: break-word;
    margin: 8px 0 0; font-size: 12px;
  }

  /* ---- codex accounts grid ---- */
  .account-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 14px;
  }
  .account-card {
    background: white;
    border: 1px solid var(--card-edge);
    border-radius: 14px;
    padding: 16px 18px;
    box-shadow: var(--shadow-sm);
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .account-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
  }
  .account-meta {
    font-size: 12px;
    color: var(--muted);
    min-height: 1.2em;
  }
  .account-stats {
    display: grid;
    /* 卡片裡現在有 4 格（Requests / Succeeded / Failed / 24h success），
       寫死 3 欄會讓第 4 格自己佔一整排。auto-fit + 120px 下限：卡片夠寬時
       四格一列，窄的時候直接掉成 2×2（而不是難看的 3+1）。 */
    grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
    gap: 8px;
  }
  .account-stats > div {
    background: #fffafb;
    border: 1px solid #f3e8eb;
    border-radius: 10px;
    padding: 8px 10px;
    text-align: center;
  }
  .account-stats strong { display: block; font-size: 20px; font-weight: 700; color: var(--ink); }
  .account-stats span { color: var(--muted); font-size: 11.5px; }
  .account-footer {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 12px;
    color: var(--muted);
    padding-top: 6px;
    border-top: 1px solid #f3e8eb;
  }

  /* ---- chips ---- */
  .chip {
    display: inline-block;
    padding: 3px 10px;
    border-radius: 999px;
    font-size: 12px; font-weight: 600;
    letter-spacing: .01em;
  }
  .chip-ok    { background: #d1fae5; color: #047857; }
  .chip-run   { background: #dbeafe; color: #1d4ed8; }
  .chip-queue { background: #fef3c7; color: #b45309; }
  .chip-fail  { background: #fee2e2; color: #b91c1c; }
  .chip-mute  { background: #f1f5f9; color: #64748b; }

  /* ---- activity feed ---- */
  .activity { list-style: none; padding: 0; margin: 0; }
  .activity-item {
    padding: 12px 14px;
    border-bottom: 1px solid #f3e8eb;
  }
  .activity-item:last-child { border-bottom: 0; }
  .activity-row { display: flex; align-items: center; gap: 10px; }
  .activity-id { font-size: 12.5px; }
  .activity-time { color: var(--muted); font-size: 12.5px; margin-left: auto; }
  .activity-meta { color: var(--muted); font-size: 13px; margin-top: 4px; }

  /* ---- code / pre ---- */
  code, pre { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
  code {
    background: #fff0f1; color: var(--ink);
    padding: 2px 7px; border-radius: 6px;
    font-size: 12.5px;
  }
  pre {
    white-space: pre-wrap;
    max-width: 420px;
    background: #fdf5f6;
    color: var(--ink);
    border: 1px solid var(--card-edge);
    padding: 10px 12px;
    border-radius: 10px;
    font-size: 12.5px;
    margin: 0;
    line-height: 1.55;
  }
  details { margin: 0; }
  details summary {
    cursor: pointer; color: var(--ink-soft);
    font-weight: 500; font-size: 13px;
    list-style: none;
  }
  details summary::-webkit-details-marker { display: none; }
  details summary::before { content: "▸ "; color: var(--muted); }
  details[open] summary::before { content: "▾ "; }
  details[open] summary { margin-bottom: 8px; }
  /* Error column: soft red wash so failure rows are scannable */
  td details + details pre,
  details.error-pre pre {
    background: #fef5f5;
    border-color: #fecaca;
    color: #7f1d1d;
  }

  /* ---- notice / error ---- */
  .notice {
    margin: 0 0 18px;
    padding: 14px 18px;
    background: var(--card);
    border: 1px solid var(--card-edge);
    border-radius: 14px;
    box-shadow: var(--shadow-sm);
    color: var(--ink);
  }
  .notice strong { color: var(--accent-1); }
  .notice code { background: #fef3c7; color: #b45309; }
  .notice-prominent { border-color: #fcd34d; background: #fffbeb; }
  .key-reveal-row {
    display: flex;
    align-items: stretch;
    gap: 10px;
    margin-top: 10px;
    max-width: 720px;
  }
  .key-reveal {
    flex: 1;
    padding: 10px 14px;
    background: white;
    border: 1px dashed #fcd34d;
    border-radius: 10px;
    color: #92400e;
    font-size: 13px;
    word-break: break-all;
    user-select: all;
  }
  .copy-btn {
    padding: 8px 16px;
    border: 1px solid var(--card-edge);
    border-radius: 10px;
    background: white;
    color: var(--ink);
    font: inherit; font-size: 13px; font-weight: 600;
    cursor: pointer; white-space: nowrap;
    transition: background .12s ease, transform .12s ease;
  }
  .copy-btn:hover { background: #fff8f9; transform: translateY(-1px); }
  .copy-btn.copied {
    background: #d1fae5; color: #047857;
    border-color: #86efac;
  }
  /* Handle column: visibly subordinate so it doesn't read as "the API key" */
  code.handle {
    background: transparent;
    color: var(--muted);
    padding: 0;
    font-size: 11.5px;
  }
  .key-name { color: var(--ink); font-weight: 600; font-size: 14px; }
  .error {
    margin: 0 0 18px;
    padding: 14px 18px;
    background: #fef2f2;
    border: 1px solid #fecaca;
    border-radius: 14px;
    color: #b91c1c;
    font-size: 14px; font-weight: 500;
  }

  /* ---- login page ---- */
  .login {
    max-width: 420px;
    margin: 100px auto;
    padding: 36px 32px;
    background: var(--card);
    backdrop-filter: blur(8px);
    border: 1px solid var(--card-edge);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
  }
  .login h1 { text-align: center; margin-bottom: 22px; font-size: 22px; }
  .login label { margin-bottom: 14px; }
  .login label.remember-row {
    display: flex; flex-direction: row; align-items: center; gap: 8px;
    font-size: 13.5px; color: var(--ink-soft); font-weight: 500;
  }
  .login label.remember-row input { width: auto; }
  .login-brand {
    display: flex; align-items: center; gap: 14px;
    margin-bottom: 26px;
  }
  .brand-mark-lg {
    width: 44px; height: 44px; border-radius: 12px;
    font-size: 24px;
  }
  .login-brand-text { display: flex; flex-direction: column; }
  .login-brand-name {
    font-size: 17px; font-weight: 700; color: var(--ink);
    letter-spacing: -0.01em;
  }
  .login-brand-sub {
    font-size: 13px; color: var(--muted);
  }

  /* ---- mobile ---- */
  @media (max-width: 900px) {
    .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .form-row-3 { grid-template-columns: 1fr; }
  }
  @media (max-width: 760px) {
    .layout { grid-template-columns: 1fr; }
    .sidebar { border-right: 0; border-bottom: 1px solid var(--card-edge); padding: 12px; }
    .nav { flex-direction: row; overflow-x: auto; gap: 6px; }
    .nav-item { border-left: 0; border-bottom: 3px solid transparent; }
    .nav-item.active { border-left: 0; border-bottom-color: var(--accent-1); }
    .content { padding: 24px 20px 48px; }
    .inline { flex-direction: column; }
    table { display: block; overflow-x: auto; }
  }
"""
