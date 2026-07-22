"""Tests for /admin/login — password check and the "remember me" session TTL."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import admin
from app.config import Settings


def _settings_for(tmp_path: Path) -> Settings:
    return Settings(
        admin_username="admin",
        admin_password="test-pass",
        admin_session_secret="test-secret",
        admin_url_prefix="",
        database_url="sqlite:///" + str(tmp_path / "app.db"),
        generated_dir=tmp_path / "generated",
        public_base_url="http://localhost:8000",
        codex_timeout_seconds=5,
        codex_workdir=tmp_path / "runs",
        codex_worker_concurrency=1,
        codex_homes=(),
        generation_queue_max_size=10,
        request_wait_timeout_seconds=5,
        image_retention_days=7,
        cleanup_interval_hours=6,
    )


class AdminLoginTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = _settings_for(Path(tmp.name))
        app = FastAPI()
        app.state.settings = settings
        app.include_router(admin.router)
        self.client = TestClient(app)

    def test_wrong_password_rejected(self):
        resp = self.client.post(
            "/admin/login",
            data={"username": "admin", "password": "nope"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_correct_password_sets_session_cookie(self):
        resp = self.client.post(
            "/admin/login",
            data={"username": "admin", "password": "test-pass"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("admin_session", resp.cookies)

    def test_remember_me_extends_cookie_max_age(self):
        default_resp = self.client.post(
            "/admin/login",
            data={"username": "admin", "password": "test-pass"},
            follow_redirects=False,
        )
        remember_resp = self.client.post(
            "/admin/login",
            data={"username": "admin", "password": "test-pass", "remember": "on"},
            follow_redirects=False,
        )
        default_max_age = _cookie_max_age(_session_set_cookie(default_resp.headers))
        remember_max_age = _cookie_max_age(_session_set_cookie(remember_resp.headers))
        self.assertEqual(default_max_age, 86400)
        self.assertEqual(remember_max_age, 30 * 86400)
        self.assertGreater(remember_max_age, default_max_age)

    def test_session_cookie_scoped_to_url_prefix(self):
        """Regression: without an explicit Path, the cookie defaults to "/"
        and collides with any other admin webui sharing the same domain
        (e.g. gemini-web's) — both use the cookie name "admin_session"."""
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = _settings_for(Path(tmp.name))
        settings = settings.__class__(**{**settings.__dict__, "admin_url_prefix": "/codex-image"})
        app = FastAPI()
        app.state.settings = settings
        app.include_router(admin.router)
        client = TestClient(app)

        resp = client.post(
            "/admin/login",
            data={"username": "admin", "password": "test-pass"},
            follow_redirects=False,
        )
        self.assertEqual(_cookie_path(_session_set_cookie(resp.headers)), "/codex-image")

    def test_login_clears_stale_root_path_cookie(self):
        """Login must also delete any pre-fix, unscoped (Path=/) admin_session
        cookie still sitting in the browser — otherwise the browser sends both
        and the server can end up reading the stale one."""
        resp = self.client.post(
            "/admin/login",
            data={"username": "admin", "password": "test-pass"},
            follow_redirects=False,
        )
        set_cookie_headers = resp.headers.get_list("set-cookie")
        root_clears = [h for h in set_cookie_headers if _cookie_path(h) == "/" and 'admin_session=""' in h]
        self.assertEqual(len(root_clears), 1)


def _session_set_cookie(headers) -> str:
    """Pick the Set-Cookie header that actually carries the session token
    (non-empty value) — login also emits a same-named deletion cookie for
    the old root path, see test_login_clears_stale_root_path_cookie."""
    for h in headers.get_list("set-cookie"):
        if h.startswith("admin_session=") and not h.startswith('admin_session=""'):
            return h
    raise AssertionError(f"no session-bearing Set-Cookie found: {headers.get_list('set-cookie')}")


def _cookie_path(set_cookie_header: str) -> str:
    for part in set_cookie_header.split(";"):
        key, _, value = part.strip().partition("=")
        if key.lower() == "path":
            return value
    raise AssertionError(f"no Path in Set-Cookie: {set_cookie_header}")


def _cookie_max_age(set_cookie_header: str) -> int:
    for part in set_cookie_header.split(";"):
        key, _, value = part.strip().partition("=")
        if key.lower() == "max-age":
            return int(value)
    raise AssertionError(f"no Max-Age in Set-Cookie: {set_cookie_header}")


if __name__ == "__main__":
    unittest.main()
