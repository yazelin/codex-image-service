import os
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest import mock

from app import db
from app.config import Settings
from app.services.storage import cleanup_expired_storage


def make_test_settings(root: Path) -> Settings:
    return Settings(
        admin_username="admin",
        admin_password="password",
        admin_session_secret="secret",
        admin_url_prefix="",
        database_url=f"sqlite:///{root / 'app.db'}",
        generated_dir=root / "generated",
        public_base_url="http://testserver",
        codex_timeout_seconds=1,
        codex_workdir=root / "codex-runs",
        codex_worker_concurrency=1,
        codex_homes=(),
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


class SessionRetentionTests(unittest.TestCase):
    """Codex 把圖以 base64 塞在 session rollout 裡，這些檔會無限長大——見 storage.py。

    刪檔的邏輯一定要有負控制：不只驗「該刪的刪掉了」，也要驗「該留的還在」。
    """

    def _make_rollout(self, home: Path, name: str, age_days: float) -> Path:
        d = home / "sessions" / "2026" / "08" / "01"
        d.mkdir(parents=True, exist_ok=True)
        f = d / name
        f.write_text('{"payload":{"type":"image"}}\n', encoding="utf-8")
        old = (db.utc_now() - timedelta(days=age_days)).timestamp()
        os.utime(f, (old, old))
        return f

    def test_old_rollouts_deleted_recent_ones_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home_a, home_b = root / "home-a", root / "home-b"
            settings = make_test_settings(root)
            settings = replace(settings, codex_homes=(str(home_a), str(home_b)))
            db.init_db(settings)
            settings.generated_dir.mkdir(parents=True)
            settings.codex_workdir.mkdir(parents=True)

            old_a = self._make_rollout(home_a, "rollout-old.jsonl", age_days=10)
            old_b = self._make_rollout(home_b, "rollout-old.jsonl", age_days=4)
            fresh = self._make_rollout(home_a, "rollout-fresh.jsonl", age_days=1)

            result = cleanup_expired_storage(settings)

            # 該刪的：兩個 home 都要掃到，不能只清第一個
            self.assertFalse(old_a.exists(), "10 天前的 rollout 應該被刪")
            self.assertFalse(old_b.exists(), "第二個 CODEX_HOME 也要被掃到")
            # 負控制：保留期內的絕對不能動
            self.assertTrue(fresh.exists(), "1 天前的 rollout 不該被刪")
            self.assertEqual(result.deleted_sessions, 2)
            self.assertGreater(result.freed_session_bytes, 0)

    def test_retention_zero_disables_sweep(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            settings = make_test_settings(root)
            settings = replace(settings, codex_homes=(str(home),), session_retention_days=0)
            db.init_db(settings)
            settings.generated_dir.mkdir(parents=True)
            settings.codex_workdir.mkdir(parents=True)

            ancient = self._make_rollout(home, "rollout-ancient.jsonl", age_days=999)
            result = cleanup_expired_storage(settings)

            self.assertTrue(ancient.exists(), "設 0 時應完全不動 session")
            self.assertEqual(result.deleted_sessions, 0)

    def test_falls_back_to_container_home_when_no_codex_homes(self):
        """CODEX_HOMES 沒設時要清容器自己的 ~/.codex，不能整個跳過。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_test_settings(root)   # codex_homes=()
            db.init_db(settings)
            settings.generated_dir.mkdir(parents=True)
            settings.codex_workdir.mkdir(parents=True)

            fake_home = root / "fake-home"
            old = self._make_rollout(fake_home, "rollout-old.jsonl", age_days=10)
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(fake_home)}):
                result = cleanup_expired_storage(settings)

            self.assertFalse(old.exists(), "CODEX_HOMES 空時應改掃 ~/.codex")
            self.assertEqual(result.deleted_sessions, 1)
