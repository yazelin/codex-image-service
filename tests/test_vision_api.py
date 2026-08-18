"""看圖回文字端點:POST /v1/vision。

存在理由:底層 Codex CLI 本來就會視覺判讀,這個服務原本只是把出口寫死成圖
(找 rollout 裡的 image、找不到就算失敗)。消費端(neko-tensei 的出圖驗收)
跑在 CI 上,那裡沒有登入態的 Codex CLI,只有這個服務有。

跟 test_jobs_api 同一套:自己組最小 app,佇列的 worker 不啟動,inspect 一律
換成假的,不 shell out 到 codex。
"""

from __future__ import annotations

import inspect as inspect_mod
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db
from app.api import public
from app.config import Settings
from app.services.codex_image import CodexGenerationError
from app.services.job_queue import ImageJobQueue

# 1x1 PNG。內容不重要,inspect 一律被換掉,這裡只是要一個合法的 base64。
PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAE"
       "hQGAhKmMIQAAAABJRU5ErkJggg==")


def _settings_for(tmp_path: Path) -> Settings:
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
        generation_queue_max_size=10,
        request_wait_timeout_seconds=5,
        image_retention_days=7,
        cleanup_interval_hours=6,
    )


class VisionApiTests(unittest.TestCase):
    def _make_env(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_path = Path(tmp.name)
        settings = _settings_for(tmp_path)
        db.init_db(settings)
        _, token = db.create_api_key(settings, "key-a")

        queue = ImageJobQueue(settings)
        queue.started = True  # 不啟動 worker

        app = FastAPI()
        app.include_router(public.router)
        app.state.settings = settings
        app.state.job_queue = queue
        return settings, TestClient(app), queue, token

    @staticmethod
    def _auth(token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def test_回傳模型的最後一則訊息(self):
        _, client, queue, token = self._make_env()
        seen = {}

        async def fake_inspect(*, request_id, prompt, images_base64):
            seen.update(prompt=prompt, images=len(images_base64))
            return "A：合格\nVERDICT: PASS"

        queue.generator.inspect = fake_inspect
        response = client.post(
            "/v1/vision",
            json={"prompt": "這頁有幾隻貓?", "images_base64": [PNG]},
            headers=self._auth(token),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("VERDICT: PASS", body["text"])
        self.assertTrue(body["id"].startswith("vis_"))
        self.assertEqual(seen, {"prompt": "這頁有幾隻貓?", "images": 1})

    def test_上游沒回東西是502不是空字串(self):
        # 判讀失敗若回 200 加空字串,呼叫端會當成「這張圖沒問題」——正好相反。
        _, client, queue, token = self._make_env()

        async def boom(*, request_id, prompt, images_base64):
            raise CodexGenerationError("Codex returned no final message")

        queue.generator.inspect = boom
        response = client.post(
            "/v1/vision",
            json={"prompt": "看圖", "images_base64": [PNG]},
            headers=self._auth(token),
        )
        self.assertEqual(response.status_code, 502)

    def test_沒有金鑰打不進來(self):
        _, client, _, _ = self._make_env()
        response = client.post(
            "/v1/vision", json={"prompt": "看圖", "images_base64": [PNG]}
        )
        self.assertEqual(response.status_code, 401)

    def test_至少要一張圖最多十六張(self):
        _, client, _, token = self._make_env()
        for images in ([], [PNG] * 17):
            response = client.post(
                "/v1/vision",
                json={"prompt": "看圖", "images_base64": images},
                headers=self._auth(token),
            )
            self.assertEqual(response.status_code, 422, f"{len(images)} 張應該被擋")

    def test_用佇列持有的那個實例(self):
        # 各建各的 CodexImageGenerator = per-CODEX_HOME 的 exec lock 形同虛設,
        # 判讀與產圖會同時動同一個 auth.json,踩 refresh-token reuse 撤銷。
        source = inspect_mod.getsource(public.inspect_images)
        self.assertIn("job_queue.generator", source)


if __name__ == "__main__":
    unittest.main()
