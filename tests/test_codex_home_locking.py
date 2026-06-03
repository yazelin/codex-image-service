"""Tests for per-CODEX_HOME exec serialization.

The image service routes requests across several ChatGPT accounts, each with
its own CODEX_HOME. ChatGPT uses refresh-token rotation with reuse-detection:
if two `codex` processes refresh the SAME home's auth.json concurrently, one
rotates the token and the other reuses the now-stale one, so OpenAI revokes the
whole token family (HTTP 401 token_invalidated) and every later run fails.

`CodexImageGenerator._home_exec_guard` must therefore guarantee that codex runs
on one home never overlap, while still letting different homes run in parallel.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.config import Settings
from app.services.codex_image import CodexImageGenerator, _EXEC_LOCK_NAME


def _settings(workdir: Path, homes: tuple[str, ...]) -> Settings:
    return Settings(
        admin_username="admin",
        admin_password="x",
        admin_session_secret="x",
        admin_url_prefix="",
        database_url="sqlite:///" + str(workdir / "app.db"),
        generated_dir=workdir / "generated",
        public_base_url="http://localhost",
        codex_timeout_seconds=5,
        codex_workdir=workdir,
        codex_worker_concurrency=2,
        codex_homes=homes,
        generation_queue_max_size=4,
        request_wait_timeout_seconds=5,
        image_retention_days=7,
        cleanup_interval_hours=6,
    )


class HomeExecGuard(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_same_home_runs_are_serialized(self):
        """Two concurrent guards on the same home must never overlap."""
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "home-a"
            home.mkdir()
            gen = CodexImageGenerator(_settings(Path(tmp), (str(home),)))

            active = 0
            max_active = 0

            async def worker():
                nonlocal active, max_active
                async with gen._home_exec_guard(str(home)):
                    active += 1
                    max_active = max(max_active, active)
                    await asyncio.sleep(0.05)
                    active -= 1

            async def drive():
                await asyncio.gather(worker(), worker(), worker())

            self._run(drive())
            self.assertEqual(max_active, 1, "same-home codex runs overlapped")
            # The advisory lockfile is created inside the home dir.
            self.assertTrue((home / _EXEC_LOCK_NAME).exists())

    def test_different_homes_run_concurrently(self):
        """Guards on distinct homes must be free to overlap."""
        with TemporaryDirectory() as tmp:
            home_a = Path(tmp) / "home-a"
            home_b = Path(tmp) / "home-b"
            home_a.mkdir()
            home_b.mkdir()
            gen = CodexImageGenerator(
                _settings(Path(tmp), (str(home_a), str(home_b)))
            )

            active = 0
            max_active = 0
            both_in = asyncio.Event()

            async def worker(home: Path):
                nonlocal active, max_active
                async with gen._home_exec_guard(str(home)):
                    active += 1
                    max_active = max(max_active, active)
                    if active >= 2:
                        both_in.set()
                    # Wait until the other home is also inside (or give up) so
                    # the overlap is observable rather than timing-dependent.
                    try:
                        await asyncio.wait_for(both_in.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
                    active -= 1

            async def drive():
                await asyncio.gather(worker(home_a), worker(home_b))

            self._run(drive())
            self.assertEqual(max_active, 2, "distinct-home runs failed to overlap")


if __name__ == "__main__":
    unittest.main()
