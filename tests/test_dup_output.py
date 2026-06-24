"""Content-hash duplicate detection.

The stale/duplicate-image bug shows up as two different requests producing the
*same* image content (Codex handing back a prior image as if new). file-mtime
guards miss it (fresh file / resumed-session rollout, old content); a content
hash catches it. These tests cover the db layer that backs that rejection.
"""
import tempfile
import unittest
from pathlib import Path

from app import db
from app.config import Settings


def make_settings(root: Path) -> Settings:
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


def _insert(settings, request_id):
    now = db.iso_now()
    with db.connect(settings) as c:
        c.execute(
            "INSERT INTO image_requests (id, prompt, size, quality, count, status, "
            "created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
            (request_id, "p", "1024x1024", "high", 1, "succeeded", now, now),
        )


class DupOutputTests(unittest.TestCase):
    def test_detects_duplicate_across_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = make_settings(Path(tmp))
            db.init_db(s)
            _insert(s, "req1")
            _insert(s, "req2")
            db.record_output_sha(s, "req1", "deadbeef")

            # req2 producing the same content as req1 -> flagged, points at req1
            self.assertEqual(
                db.output_sha_seen_before(s, "deadbeef", exclude_request_id="req2"),
                "req1",
            )
            # excluding itself -> not seen before
            self.assertIsNone(
                db.output_sha_seen_before(s, "deadbeef", exclude_request_id="req1")
            )
            # a never-seen hash -> None
            self.assertIsNone(db.output_sha_seen_before(s, "feedface"))


if __name__ == "__main__":
    unittest.main()
