"""Tests for the auth-token audit trail and the graceful timeout kill.

2026-08-01 的事故裡三個 CODEX_HOME 的 refresh token 在 19 小時內接連被
OpenAI 撤銷(`refresh_token_invalidated`),事後查不出是哪一次 run 弄掉了
token rotation——因為服務對 auth.json 完全沒有留任何痕跡。這裡的測試釘住
兩件事:

1. 每次 codex run 前後都要對該 home 的 auth.json 取指紋並落審計檔,rotation
   發生在「被 timeout 殺掉的 run」裡時要留下明確警訊(那正是 token 可能只在
   伺服器端輪替、沒寫回本地的情境)。
2. timeout 不再直接 SIGKILL:先 SIGTERM 給 codex 機會把輪替後的 auth.json
   寫完,寬限期過了才 SIGKILL。
"""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.config import Settings
from app.services import codex_image
from app.services.codex_image import CodexImageGenerator


def _settings(workdir: Path, homes: tuple[str, ...] = ()) -> Settings:
    return Settings(
        admin_username="admin",
        admin_password="x",
        admin_session_secret="x",
        admin_url_prefix="",
        database_url="sqlite:///" + str(workdir / "app.db"),
        generated_dir=workdir / "generated",
        public_base_url="http://localhost",
        codex_timeout_seconds=5,
        codex_workdir=workdir / "codex-runs",
        codex_worker_concurrency=2,
        codex_homes=homes,
        generation_queue_max_size=4,
        request_wait_timeout_seconds=5,
        image_retention_days=7,
        cleanup_interval_hours=6,
    )


def _write_auth(home: Path, refresh_token: str, last_refresh: str) -> None:
    (home / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {"refresh_token": refresh_token, "id_token": "x.y.z"},
                "last_refresh": last_refresh,
            }
        ),
        encoding="utf-8",
    )


class AuthSnapshot(unittest.TestCase):
    def test_snapshot_fingerprints_refresh_token_without_leaking_it(self):
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "home-a"
            home.mkdir()
            _write_auth(home, "secret-refresh-token", "2026-08-03T00:00:00Z")

            snap = codex_image._auth_snapshot(str(home))

            self.assertIsNotNone(snap)
            self.assertEqual(snap["last_refresh"], "2026-08-03T00:00:00Z")
            self.assertEqual(len(snap["rt"]), 12)
            self.assertNotIn("secret-refresh-token", json.dumps(snap))

    def test_snapshot_changes_when_token_rotates(self):
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "home-a"
            home.mkdir()
            _write_auth(home, "token-1", "2026-08-03T00:00:00Z")
            before = codex_image._auth_snapshot(str(home))
            _write_auth(home, "token-2", "2026-08-03T01:00:00Z")
            after = codex_image._auth_snapshot(str(home))

            self.assertNotEqual(before["rt"], after["rt"])

    def test_snapshot_of_missing_auth_is_none(self):
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "home-a"
            home.mkdir()
            self.assertIsNone(codex_image._auth_snapshot(str(home)))
            self.assertIsNone(codex_image._auth_snapshot(None))


class AuditTrail(unittest.TestCase):
    def test_rotation_during_a_killed_run_is_recorded(self):
        """被殺掉的 run 期間發生 rotation = 最可疑的情境,必須留證。"""
        with TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            home = Path(tmp) / "home-a"
            home.mkdir()
            _write_auth(home, "token-1", "2026-08-03T00:00:00Z")
            gen = CodexImageGenerator(_settings(workdir, (str(home),)))

            before = codex_image._auth_snapshot(str(home))
            _write_auth(home, "token-2", "2026-08-03T01:00:00Z")
            gen._audit_token_state(
                event="timeout_kill",
                codex_home=str(home),
                before=before,
                after=codex_image._auth_snapshot(str(home)),
            )

            lines = [
                json.loads(line)
                for line in gen._audit_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["event"], "timeout_kill")
            self.assertTrue(lines[0]["rotated"])
            self.assertEqual(lines[0]["codex_home"], str(home))

    def test_unchanged_token_is_recorded_as_not_rotated(self):
        with TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            home = Path(tmp) / "home-a"
            home.mkdir()
            _write_auth(home, "token-1", "2026-08-03T00:00:00Z")
            gen = CodexImageGenerator(_settings(workdir, (str(home),)))
            snap = codex_image._auth_snapshot(str(home))

            gen._audit_token_state(
                event="run_finished", codex_home=str(home), before=snap, after=snap
            )

            line = json.loads(gen._audit_path.read_text(encoding="utf-8").strip())
            self.assertFalse(line["rotated"])


class FlockDegradation(unittest.TestCase):
    def test_failure_to_take_the_lock_is_logged(self):
        """降級成無鎖執行過去是靜默的——那正是最該吵的時候。"""
        with self.assertLogs("app.services.codex_image", level="WARNING") as captured:
            fd = codex_image._flock_acquire(
                Path("/nonexistent-dir-for-tests-xyz/.codex-exec.lock")
            )
        self.assertIsNone(fd)
        self.assertTrue(any("lock" in line.lower() for line in captured.output))


class _FakeProcess:
    """最小 asyncio subprocess 替身:記錄收到的訊號。"""

    def __init__(self, *, dies_on_term: bool) -> None:
        self.pid = 4242
        self.returncode = None
        self._dies_on_term = dies_on_term
        self._terminated = asyncio.Event()

    async def communicate(self):
        if self._dies_on_term:
            await self._terminated.wait()
            self.returncode = -15
            return b"", b""
        await asyncio.Event().wait()  # 永不結束,除非被 SIGKILL 收掉
        raise AssertionError("unreachable")

    def notify_terminated(self) -> None:
        self._terminated.set()


class GracefulTimeoutKill(unittest.TestCase):
    def _drive(self, *, dies_on_term: bool):
        signals: list[int] = []
        with TemporaryDirectory() as tmp:
            gen = CodexImageGenerator(_settings(Path(tmp)))
            process = _FakeProcess(dies_on_term=dies_on_term)

            def fake_killpg(pid, sig):
                signals.append(sig)
                if sig == 15 and dies_on_term:
                    process.notify_terminated()

            original = codex_image.os.killpg
            codex_image.os.killpg = fake_killpg
            try:
                asyncio.run(
                    gen._terminate_process_group(process, grace_seconds=0.05)
                )
            finally:
                codex_image.os.killpg = original
        return signals

    def test_sigterm_first_then_sigkill_when_it_ignores_term(self):
        self.assertEqual(self._drive(dies_on_term=False), [15, 9])

    def test_no_sigkill_when_codex_exits_on_sigterm(self):
        """codex 收到 SIGTERM 就收工時不再補刀——讓它有機會寫完 auth.json。"""
        self.assertEqual(self._drive(dies_on_term=True), [15])


if __name__ == "__main__":
    unittest.main()
