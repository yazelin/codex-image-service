import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from app import db
from app.config import Settings
from app.services.storage import cleanup_expired_storage


def make_test_settings(root: Path) -> Settings:
    return Settings(
        admin_username="admin",
        admin_password="password",
        admin_session_secret="secret",
        database_url=f"sqlite:///{root / 'app.db'}",
        generated_dir=root / "generated",
        public_base_url="http://testserver",
        codex_timeout_seconds=1,
        codex_workdir=root / "codex-runs",
        codex_worker_concurrency=1,
        generation_queue_max_size=2,
        request_wait_timeout_seconds=3,
        image_retention_days=7,
        cleanup_interval_hours=6,
    )


class CleanupTests(unittest.TestCase):
    def test_expired_request_files_and_workdir_are_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_test_settings(root)
            db.init_db(settings)
            settings.generated_dir.mkdir(parents=True)
            settings.codex_workdir.mkdir(parents=True)

            image_path = settings.generated_dir / "img_test.png"
            image_path.write_bytes(b"fake-png")
            workdir = settings.codex_workdir / "img_test"
            workdir.mkdir()
            (workdir / "scratch.txt").write_text("work")

            api_key, _ = db.create_api_key(settings, "customer")
            expires_at = (db.utc_now() - timedelta(days=1)).isoformat()
            db.insert_image_request(
                settings,
                request_id="img_test",
                api_key_id=api_key["id"],
                prompt="test",
                size="1024x1024",
                quality="medium",
                count=1,
                expires_at=expires_at,
            )
            db.mark_image_request_succeeded(
                settings,
                request_id="img_test",
                image_paths=[image_path],
                stdout="",
                stderr="",
                duration_seconds=1.0,
                workdir=workdir,
                codex_command="codex exec <imagegen prompt>",
            )

            result = cleanup_expired_storage(settings)
            request = db.get_image_request(settings, "img_test")

            self.assertEqual(result.expired_requests, 1)
            self.assertEqual(result.deleted_files, 1)
            self.assertEqual(result.deleted_workdirs, 1)
            self.assertFalse(image_path.exists())
            self.assertFalse(workdir.exists())
            self.assertEqual(request["status"], "expired")

    def test_old_orphan_image_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_test_settings(root)
            db.init_db(settings)
            settings.generated_dir.mkdir(parents=True)
            settings.codex_workdir.mkdir(parents=True)

            image_path = settings.generated_dir / "orphan.png"
            image_path.write_bytes(b"fake")
            old = (db.utc_now() - timedelta(days=10)).timestamp()
            os.utime(image_path, (old, old))

            result = cleanup_expired_storage(settings)

            self.assertEqual(result.deleted_files, 1)
            self.assertFalse(image_path.exists())


if __name__ == "__main__":
    unittest.main()
