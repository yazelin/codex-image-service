"""額度見底的帳號不再被輪到。

存在理由:2026-08-06 第一次讓 pipeline 自動出一整話,輪到週配額剩 0% 的帳號時
那筆判讀直接失敗(回 502)。原本的輪替完全不看用量,只會在失敗之後換下一個
帳號重試——等於一定要先撞一次牆。
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.config import Settings
from app.services import chatgpt_usage
from app.services.codex_image import CodexImageGenerator

A, B, C = "/homes/a", "/homes/b", "/homes/c"


def _settings(tmp_path: Path, *, threshold: int = 5) -> Settings:
    return Settings(
        admin_username="admin", admin_password="x", admin_session_secret="x",
        admin_url_prefix="", database_url="sqlite:///" + str(tmp_path / "app.db"),
        generated_dir=tmp_path / "generated", public_base_url="http://localhost:8000",
        codex_timeout_seconds=5, codex_workdir=tmp_path / "runs",
        codex_worker_concurrency=1, codex_homes=(A, B, C),
        generation_queue_max_size=10, request_wait_timeout_seconds=5,
        image_retention_days=7, cleanup_interval_hours=6,
        codex_min_quota_percent=threshold,
    )


def _usage(**by_home):
    """{home: 週剩餘 %} → fetch_many 的回傳形狀。None = 查不到。"""
    out = {}
    for home, left in by_home.items():
        if left is None:
            continue
        out[home] = {"plan": "team", "limit_reached": False,
                     "windows": [{"label": "Weekly", "remaining_percent": left,
                                  "reset_at": None}]}
    return out


class RotationQuotaTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = _settings(Path(tmp.name))
        self.gen = CodexImageGenerator(self.settings)

    def _rotation_with(self, usage):
        async def fake_fetch(homes):
            return {h: usage[h] for h in homes if h in usage}
        original = chatgpt_usage.fetch_many
        chatgpt_usage.fetch_many = fake_fetch
        try:
            return asyncio.run(self.gen._rotation())
        finally:
            chatgpt_usage.fetch_many = original

    def test_見底的帳號被排除(self):
        rotation = self._rotation_with(_usage(**{A: 40, B: 0, C: 20}))
        self.assertNotIn(B, rotation)
        self.assertEqual(sorted(rotation), sorted([A, C]))

    def test_剛好在門檻上還能用(self):
        # 門檻是 5,剩 5% 不算見底;4% 才算
        self.assertIn(A, self._rotation_with(_usage(**{A: 5, B: 50, C: 50})))
        self.assertNotIn(A, self._rotation_with(_usage(**{A: 4, B: 50, C: 50})))

    def test_查不到用量的帳號視為可用(self):
        # 一次網路問題就讓整池縮水,比偶爾撞一次牆糟
        rotation = self._rotation_with(_usage(**{A: 40, C: 20}))  # B 查不到
        self.assertIn(B, rotation)

    def test_全部見底就不跳過任何一個(self):
        # 寧可撞牆讓既有的 retry 處理,也不要整條線罷工
        rotation = self._rotation_with(_usage(**{A: 0, B: 1, C: 2}))
        self.assertEqual(sorted(rotation), sorted([A, B, C]))

    def test_門檻設0等於關掉這個機制(self):
        self.gen.settings = _settings(self.settings.codex_workdir.parent, threshold=0)
        rotation = self._rotation_with(_usage(**{A: 0, B: 0, C: 0}))
        self.assertEqual(sorted(rotation), sorted([A, B, C]))

    def test_查用量整個壞掉不影響出圖(self):
        async def boom(homes):
            raise RuntimeError("usage endpoint down")
        original = chatgpt_usage.fetch_many
        chatgpt_usage.fetch_many = boom
        try:
            rotation = asyncio.run(self.gen._rotation())
        finally:
            chatgpt_usage.fetch_many = original
        self.assertEqual(sorted(rotation), sorted([A, B, C]))

    def test_沒設多帳號時回單一預設(self):
        self.gen.settings = Settings(
            **{**self.settings.__dict__, "codex_homes": ()}
        )
        self.assertEqual(self._rotation_with({}), [None])


if __name__ == "__main__":
    unittest.main()
