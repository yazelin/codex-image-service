"""image_gen 429 usage_limit_reached → readable error with reset time.

Codex does NOT exit non-zero when the built-in image_gen tool is rate-limited;
it just logs the 429 to stderr and answers in prose. Without parsing it the job
fails with the generic "no image in the session rollout" message.
"""
import unittest

from app.services.codex_image import _image_gen_limit_message

STDERR_429 = (
    "codex\n我會使用 imagegen 技能\n"
    "2026-10-01T02:32:21.816180Z ERROR codex_core::tools::router: error=image generation failed: "
    'http 429 Too Many Requests: Some("{\\"error\\":{\\"type\\":\\"usage_limit_reached\\",'
    '\\"message\\":\\"The usage limit has been reached\\",\\"plan_type\\":\\"team\\",'
    '\\"resets_at\\":1790875165,\\"eligible_promo\\":null,\\"limit_window_minutes\\":null,'
    '\\"resets_in_seconds\\":53224}}")\n'
)


class ImageGenLimitMessageTest(unittest.TestCase):
    def test_429_reports_quota_and_reset_time_in_taipei(self):
        msg = _image_gen_limit_message(STDERR_429)
        self.assertIsNotNone(msg)
        self.assertIn("額度已用完", msg)
        # 1790875165 = 2026-10-01T17:19:25Z = 2026-10-02 01:19 (UTC+8)
        self.assertIn("2026-10-02 01:19", msg)
        self.assertIn("+08:00", msg)

    def test_unrelated_stderr_returns_none(self):
        self.assertIsNone(_image_gen_limit_message("ERROR: something else\n"))
        self.assertIsNone(_image_gen_limit_message(""))

    def test_429_without_resets_at_still_reports_quota(self):
        msg = _image_gen_limit_message(
            'image generation failed: http 429 Too Many Requests: {"type":"usage_limit_reached"}'
        )
        self.assertIsNotNone(msg)
        self.assertIn("額度已用完", msg)


if __name__ == "__main__":
    unittest.main()
