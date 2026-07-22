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
        default_max_age = _cookie_max_age(default_resp.headers["set-cookie"])
        remember_max_age = _cookie_max_age(remember_resp.headers["set-cookie"])
        self.assertEqual(default_max_age, 86400)
        self.assertEqual(remember_max_age, 30 * 86400)
        self.assertGreater(remember_max_age, default_max_age)


def _cookie_max_age(set_cookie_header: str) -> int:
    for part in set_cookie_header.split(";"):
        key, _, value = part.strip().partition("=")
        if key.lower() == "max-age":
            return int(value)
    raise AssertionError(f"no Max-Age in Set-Cookie: {set_cookie_header}")


if __name__ == "__main__":
    unittest.main()
