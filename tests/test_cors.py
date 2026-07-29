"""CORS_ALLOW_ORIGINS 有設才掛 middleware;沒設維持原行為(無 CORS header)。"""
import importlib
import os
import unittest

from fastapi.testclient import TestClient


def _fresh_app():
    """app 在 module import 時就依 settings 組好,改 env 後要整組重載。"""
    from app import config, main
    config.get_settings.cache_clear() if hasattr(config.get_settings, "cache_clear") else None
    importlib.reload(config)
    return importlib.reload(main).app


class CorsTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("CORS_ALLOW_ORIGINS", None)
        from app import config, main
        importlib.reload(config)
        importlib.reload(main)

    def test_no_env_no_cors_header(self):
        os.environ.pop("CORS_ALLOW_ORIGINS", None)
        client = TestClient(_fresh_app())
        r = client.get("/health", headers={"Origin": "https://yazelin.github.io"})
        self.assertNotIn("access-control-allow-origin", r.headers)

    def test_env_enables_cors_for_listed_origin(self):
        os.environ["CORS_ALLOW_ORIGINS"] = "https://yazelin.github.io, http://localhost:8765"
        client = TestClient(_fresh_app())
        r = client.get("/health", headers={"Origin": "https://yazelin.github.io"})
        self.assertEqual(r.headers.get("access-control-allow-origin"), "https://yazelin.github.io")
        r2 = client.get("/health", headers={"Origin": "https://evil.example"})
        self.assertNotIn("access-control-allow-origin", r2.headers)


if __name__ == "__main__":
    unittest.main()
