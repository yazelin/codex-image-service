"""Tests for the async job API: POST /v1/images/jobs + GET /v1/images/jobs/{id}.

Uses TestClient against a minimal app (public router only) with a real
ImageJobQueue whose workers are never started, so submitted jobs stay
queued and nothing shells out to codex.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db
from app.api import public
from app.config import Settings
from app.services.job_queue import ImageJobQueue


def _settings_for(tmp_path: Path, *, queue_max_size: int = 10) -> Settings:
    return Settings(
        admin_username="admin",
        admin_password="x",
        admin_session_secret="x",
        admin_url_prefix="",
        database_url="sqlite:///" + str(tmp_path / "app.db"),
        generated_dir=tmp_path / "generated",
        public_base_url="http://localhost:8000",
        codex_timeout_seconds=5,
        codex_workdir=tmp_path / "runs",
        codex_worker_concurrency=1,
        codex_homes=(),
        generation_queue_max_size=queue_max_size,
        request_wait_timeout_seconds=5,
        image_retention_days=7,
        cleanup_interval_hours=6,
    )


class JobsApiTests(unittest.TestCase):
    def _make_env(self, *, queue_max_size: int = 10):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_path = Path(tmp.name)
        settings = _settings_for(tmp_path, queue_max_size=queue_max_size)
        db.init_db(settings)
        _, token_a = db.create_api_key(settings, "key-a")
        _, token_b = db.create_api_key(settings, "key-b")

        queue = ImageJobQueue(settings)
        # Mark started without launching workers: jobs stay queued.
        queue.started = True

        app = FastAPI()
        app.include_router(public.router)
        app.state.settings = settings
        app.state.job_queue = queue
        client = TestClient(app)
        return settings, client, token_a, token_b

    @staticmethod
    def _auth(token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _insert_job(self, settings: Settings, request_id: str, api_key_id: str) -> None:
        db.insert_image_request(
            settings,
            request_id=request_id,
            api_key_id=api_key_id,
            prompt="test prompt",
            size="1024x1024",
            quality="medium",
            count=1,
            expires_at="2099-01-01T00:00:00+00:00",
        )

    def _key_id(self, settings: Settings, token: str) -> str:
        return db.get_api_key_by_token(settings, token)["id"]

    def test_submit_returns_202_with_queued_job(self):
        settings, client, token_a, _ = self._make_env()

        response = client.post(
            "/v1/images/jobs",
            json={"prompt": "a calm mountain lake"},
            headers=self._auth(token_a),
        )

        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertTrue(body["id"].startswith("img_"))
        self.assertEqual(body["status"], "queued")
        row = db.get_image_request(settings, body["id"])
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["api_key_id"], self._key_id(settings, token_a))

    def test_submit_returns_503_when_queue_full(self):
        _, client, token_a, _ = self._make_env(queue_max_size=1)

        first = client.post(
            "/v1/images/jobs", json={"prompt": "one"}, headers=self._auth(token_a)
        )
        self.assertEqual(first.status_code, 202)

        second = client.post(
            "/v1/images/jobs", json={"prompt": "two"}, headers=self._auth(token_a)
        )
        self.assertEqual(second.status_code, 503)

    def test_poll_queued_job(self):
        _, client, token_a, _ = self._make_env()
        submitted = client.post(
            "/v1/images/jobs", json={"prompt": "queued job"}, headers=self._auth(token_a)
        ).json()

        response = client.get(
            f"/v1/images/jobs/{submitted['id']}", headers=self._auth(token_a)
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["id"], submitted["id"])
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["images"], [])
        self.assertIsNone(body["error"])

    def test_poll_running_job(self):
        settings, client, token_a, _ = self._make_env()
        self._insert_job(settings, "img_running", self._key_id(settings, token_a))
        db.mark_image_request_running(settings, "img_running")

        response = client.get("/v1/images/jobs/img_running", headers=self._auth(token_a))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "running")

    def test_poll_succeeded_job_includes_image_urls(self):
        settings, client, token_a, _ = self._make_env()
        self._insert_job(settings, "img_done", self._key_id(settings, token_a))
        db.mark_image_request_succeeded(
            settings,
            request_id="img_done",
            image_paths=[settings.generated_dir / "img_done.png"],
            stdout="",
            stderr="",
            duration_seconds=1.0,
            workdir=settings.codex_workdir,
            codex_command="codex exec",
        )

        response = client.get("/v1/images/jobs/img_done", headers=self._auth(token_a))

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "succeeded")
        self.assertEqual(len(body["images"]), 1)
        self.assertEqual(
            body["images"][0]["url"], "http://localhost:8000/generated/img_done.png"
        )
        self.assertEqual(body["images"][0]["expires_at"], body["expires_at"])

    def test_poll_failed_job_includes_error(self):
        settings, client, token_a, _ = self._make_env()
        self._insert_job(settings, "img_bad", self._key_id(settings, token_a))
        db.mark_image_request_failed(
            settings, request_id="img_bad", error="codex exploded"
        )

        response = client.get("/v1/images/jobs/img_bad", headers=self._auth(token_a))

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error"], "codex exploded")
        self.assertEqual(body["images"], [])

    def test_poll_other_keys_job_returns_404(self):
        settings, client, token_a, token_b = self._make_env()
        self._insert_job(settings, "img_owned_by_a", self._key_id(settings, token_a))

        response = client.get(
            "/v1/images/jobs/img_owned_by_a", headers=self._auth(token_b)
        )

        self.assertEqual(response.status_code, 404)

    def test_poll_unknown_id_returns_404(self):
        _, client, token_a, _ = self._make_env()

        response = client.get("/v1/images/jobs/img_nope", headers=self._auth(token_a))

        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
