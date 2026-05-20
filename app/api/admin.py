from __future__ import annotations

import html
import json
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db
from app.models import ImageGenerateRequest
from app.security import constant_equals, create_admin_session, verify_admin_session
from app.services.job_queue import GenerationQueueUnavailable


router = APIRouter()


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


@router.get("/", include_in_schema=False)
async def root(request: Request) -> RedirectResponse:
    return RedirectResponse(_url(request, "/admin"), status_code=303)


@router.get("/admin/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request):
    if _admin_user(request):
        return RedirectResponse(_url(request, "/admin"), status_code=303)
    return HTMLResponse(_layout("Admin Login", _login_form(_prefix(request))))


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
            _layout(
                "Admin Login",
                _login_form(_prefix(request), error="Invalid username or password"),
            ),
            status_code=401,
        )

    response = RedirectResponse(_url(request, "/admin"), status_code=303)
    response.set_cookie(
        "admin_session",
        create_admin_session(username, settings.admin_session_secret),
        httponly=True,
        samesite="lax",
        max_age=86400,
    )
    return response


@router.post("/admin/logout", include_in_schema=False)
async def logout(request: Request) -> RedirectResponse:
    response = RedirectResponse(_url(request, "/admin/login"), status_code=303)
    response.delete_cookie("admin_session")
    return response


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request):
    if not _admin_user(request):
        return _redirect_login(request)
    settings = request.app.state.settings
    return HTMLResponse(_dashboard(settings, _prefix(request)))


@router.post("/admin/api-keys", response_class=HTMLResponse, include_in_schema=False)
async def create_api_key(request: Request):
    if not _admin_user(request):
        return _redirect_login(request)
    settings = request.app.state.settings
    form = await request.form()
    name = str(form.get("name", ""))
    _, raw_key = db.create_api_key(settings, name)
    return HTMLResponse(_dashboard(settings, _prefix(request), new_api_key=raw_key))


@router.post("/admin/api-keys/{key_id}/disable", include_in_schema=False)
async def disable_api_key(request: Request, key_id: str) -> RedirectResponse:
    if not _admin_user(request):
        return _redirect_login(request)
    db.disable_api_key(request.app.state.settings, key_id)
    return RedirectResponse(_url(request, "/admin"), status_code=303)


@router.post("/admin/api-keys/{key_id}/delete", include_in_schema=False)
async def delete_api_key(request: Request, key_id: str) -> RedirectResponse:
    if not _admin_user(request):
        return _redirect_login(request)
    db.delete_api_key(request.app.state.settings, key_id)
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
    return RedirectResponse(_url(request, "/admin"), status_code=303)


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
                    "Refresh in 1-3 minutes to see the result below."
                )
            except GenerationQueueUnavailable as exc:
                error = str(exc)
    return HTMLResponse(
        _dashboard(settings, _prefix(request), test_notice=notice, test_error=error)
    )


@router.post("/admin/cleanup", response_class=HTMLResponse, include_in_schema=False)
async def cleanup(request: Request):
    if not _admin_user(request):
        return _redirect_login(request)
    result = await request.app.state.cleanup.run_once()
    return HTMLResponse(
        _dashboard(request.app.state.settings, _prefix(request), cleanup_result=result)
    )


def _dashboard(
    settings: Any,
    prefix: str,
    new_api_key: str | None = None,
    cleanup_result: Any | None = None,
    test_notice: str | None = None,
    test_error: str | None = None,
) -> str:
    keys = db.list_api_keys(settings)
    requests = db.list_image_requests(settings, limit=100)
    stats = db.dashboard_stats(settings)

    notices = []
    if new_api_key:
        notices.append(
            "<div class='notice'><strong>New API key:</strong> "
            f"<code>{html.escape(new_api_key)}</code><br>"
            "Copy it now. It will not be shown again.</div>"
        )
    if cleanup_result:
        errors = cleanup_result.errors or []
        notice = (
            f"Cleanup complete: expired requests={cleanup_result.expired_requests}, "
            f"deleted files={cleanup_result.deleted_files}, "
            f"deleted workdirs={cleanup_result.deleted_workdirs}."
        )
        if errors:
            notice += " Errors: " + html.escape("; ".join(errors))
        notices.append(f"<div class='notice'>{notice}</div>")
    if test_notice:
        notices.append(f"<div class='notice'>{test_notice}</div>")
    if test_error:
        notices.append(f"<div class='error'>{html.escape(test_error)}</div>")

    body = f"""
    <div class="topbar">
      <h1>Codex Image Service</h1>
      <form method="post" action="{prefix}/admin/logout"><button type="submit">Logout</button></form>
    </div>
    {''.join(notices)}
    <section class="stats">
      <div><strong>{stats['api_key_count']}</strong><span>Total keys</span></div>
      <div><strong>{stats['active_key_count']}</strong><span>Active keys</span></div>
      <div><strong>{stats['request_count']}</strong><span>Requests</span></div>
      <div><strong>{stats['queued_count']}</strong><span>Queued/running</span></div>
    </section>
    <section>
      <h2>Create API Key</h2>
      <form class="inline" method="post" action="{prefix}/admin/api-keys">
        <input name="name" placeholder="Customer or project name" required>
        <button type="submit">Create key</button>
      </form>
    </section>
    <section>
      <h2>API Keys</h2>
      {_api_keys_table(keys, prefix)}
    </section>
    <section>
      <h2>Test image generation</h2>
      <p style="color:#5f6877;margin:0 0 12px">Picks an existing key and runs one job through the queue.
      Tracked under that key's usage stats. Generation takes 1-3 minutes — refresh the page to see status.</p>
      {_test_generate_form(keys, prefix)}
    </section>
    <section>
      <div class="section-title">
        <h2>Image Requests</h2>
        <form method="post" action="{prefix}/admin/cleanup"><button type="submit">Run cleanup</button></form>
      </div>
      {_requests_table(requests, settings)}
    </section>
    """
    return _layout("Codex Image Service", body)


def _test_generate_form(keys: list[dict[str, Any]], prefix: str) -> str:
    enabled_keys = [k for k in keys if k["enabled"]]
    if not enabled_keys:
        return "<p><em>Create an enabled API key first.</em></p>"
    options = "".join(
        f"<option value='{html.escape(k['id'])}'>{html.escape(k['name'])} ({html.escape(k['id'])})</option>"
        for k in enabled_keys
    )
    return f"""
      <form method="post" action="{prefix}/admin/test-generate" style="display:grid;gap:10px;max-width:640px">
        <label>API key
          <select name="api_key_id" required>{options}</select>
        </label>
        <label>Prompt
          <textarea name="prompt" rows="3" required placeholder="A minimalist orange tabby cat clock face on white"></textarea>
        </label>
        <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px">
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
        <button type="submit" style="justify-self:start">Queue test job</button>
      </form>
    """


def _api_keys_table(keys: list[dict[str, Any]], prefix: str) -> str:
    rows = []
    for key in keys:
        enabled = "enabled" if key["enabled"] else "disabled"
        action_forms = []
        if key["enabled"]:
            action_forms.append(
                f"<form method='post' action='{prefix}/admin/api-keys/{html.escape(key['id'])}/disable' style='display:inline'>"
                "<button type='submit'>Disable</button></form>"
            )
        action_forms.append(
            f"<form method='post' action='{prefix}/admin/api-keys/{html.escape(key['id'])}/delete' style='display:inline;margin-left:6px'"
            " onsubmit=\"return confirm('Delete this API key permanently? History rows stay but the key can no longer authenticate.');\">"
            "<button type='submit' style='background:#c0392b;border-color:#c0392b'>Delete</button></form>"
        )
        action = "".join(action_forms)
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(key['id'])}</code></td>"
            f"<td>{html.escape(key['name'])}</td>"
            f"<td>{enabled}</td>"
            f"<td>{html.escape(str(key['requests_count']))}</td>"
            f"<td>{html.escape(str(key['last_used_at'] or ''))}</td>"
            f"<td>{action}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='6'>No API keys yet.</td></tr>")
    return (
        "<table><thead><tr><th>ID</th><th>Name</th><th>Status</th><th>Requests</th>"
        "<th>Last used</th><th>Action</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
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
        delete_form = (
            f"<form method='post' action='{prefix}/admin/image-requests/"
            f"{html.escape(item['id'])}/delete' style='display:inline'"
            " onsubmit=\"return confirm('Delete this image, workdir, and history row?');\">"
            "<button type='submit' style='background:#c0392b;border-color:#c0392b'>Delete</button>"
            "</form>"
        )
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(item['id'])}</code></td>"
            f"<td>{html.escape(item['status'])}</td>"
            f"<td>{html.escape(item.get('api_key_name') or '')}</td>"
            f"<td>{html.escape(item['created_at'])}</td>"
            f"<td>{html.escape(item['expires_at'])}</td>"
            f"<td>{', '.join(links)}</td>"
            f"<td><details><summary>Prompt</summary><pre>{html.escape(item['prompt'])}</pre></details></td>"
            f"<td><details><summary>Error</summary><pre>{html.escape(error)}</pre></details></td>"
            f"<td>{delete_form}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='9'>No image requests yet.</td></tr>")
    return (
        "<table><thead><tr><th>ID</th><th>Status</th><th>Key</th><th>Created</th>"
        "<th title='Auto-deleted after this time by the scheduled cleanup'>Expires (auto-delete)</th>"
        "<th>Images</th><th>Prompt</th><th>Error</th><th>Action</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _login_form(prefix: str, error: str | None = None) -> str:
    error_html = f"<div class='error'>{html.escape(error)}</div>" if error else ""
    return f"""
    <div class="login">
      <h1>Admin Login</h1>
      {error_html}
      <form method="post" action="{prefix}/admin/login">
        <label>Username<input name="username" autocomplete="username" required></label>
        <label>Password<input type="password" name="password" autocomplete="current-password" required></label>
        <button type="submit">Login</button>
      </form>
    </div>
    """


def _layout(title: str, body: str) -> str:
    return f"""
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>{html.escape(title)}</title>
      <style>
        :root {{ color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
        body {{ margin: 0; background: #f6f7f9; color: #16181d; }}
        h1 {{ font-size: 24px; margin: 0; }}
        h2 {{ font-size: 18px; margin: 0 0 12px; }}
        section, .login {{ max-width: 1180px; margin: 20px auto; padding: 20px; background: #fff; border: 1px solid #dfe3ea; border-radius: 8px; }}
        .topbar {{ max-width: 1180px; margin: 24px auto 0; display: flex; justify-content: space-between; align-items: center; }}
        .stats {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; background: transparent; border: 0; padding: 0; }}
        .stats div {{ background: #fff; border: 1px solid #dfe3ea; border-radius: 8px; padding: 16px; }}
        .stats strong {{ display: block; font-size: 24px; }}
        .stats span {{ color: #5f6877; }}
        .section-title {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; }}
        .inline {{ display: flex; gap: 8px; }}
        input {{ box-sizing: border-box; width: 100%; padding: 10px 12px; border: 1px solid #c9d0da; border-radius: 6px; font: inherit; }}
        label {{ display: grid; gap: 6px; margin: 10px 0; }}
        button {{ padding: 9px 12px; border: 1px solid #1f6feb; border-radius: 6px; background: #1f6feb; color: #fff; font: inherit; cursor: pointer; white-space: nowrap; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
        th, td {{ padding: 10px; border-bottom: 1px solid #e6e9ef; text-align: left; vertical-align: top; }}
        th {{ color: #5f6877; font-weight: 600; }}
        code, pre {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }}
        pre {{ white-space: pre-wrap; max-width: 420px; }}
        .notice {{ max-width: 1180px; margin: 16px auto 0; padding: 12px 16px; background: #eef6ff; border: 1px solid #b8d7ff; border-radius: 8px; }}
        .error {{ padding: 10px 12px; background: #fff0f0; border: 1px solid #ffc6c6; border-radius: 6px; color: #9b1c1c; }}
        @media (max-width: 760px) {{
          .stats {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
          .inline, .topbar, .section-title {{ align-items: stretch; flex-direction: column; }}
          table {{ display: block; overflow-x: auto; }}
        }}
      </style>
    </head>
    <body>{body}</body>
    </html>
    """
