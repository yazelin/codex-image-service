"""Unit tests for the image-edit path in CodexImageGenerator.

These tests do not actually invoke `codex exec`; they patch the
subprocess so we can assert on the instruction text + on-disk
reference layout without burning a real ChatGPT quota.
"""

from __future__ import annotations

import asyncio
import base64
import struct
import unittest
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, AsyncMock

from app.config import Settings
from app.services.codex_image import CodexImageGenerator, _detect_image_ext


def _make_png_bytes() -> bytes:
    """Build a minimal valid 1x1 PNG so the magic-byte detector picks 'png'."""
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff"))
    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


def _settings_for(workdir: Path, generated: Path) -> Settings:
    return Settings(
        admin_username="admin",
        admin_password="x",
        admin_session_secret="x",
        admin_url_prefix="",
        database_url="sqlite:///" + str(workdir / "app.db"),
        generated_dir=generated,
        public_base_url="http://localhost",
        codex_timeout_seconds=5,
        codex_workdir=workdir,
        codex_worker_concurrency=1,
        generation_queue_max_size=1,
        request_wait_timeout_seconds=5,
        image_retention_days=7,
        cleanup_interval_hours=6,
    )


class DetectExt(unittest.TestCase):
    def test_png(self):
        self.assertEqual(_detect_image_ext(b"\x89PNG\r\n\x1a\n..."), ".png")

    def test_jpg(self):
        self.assertEqual(_detect_image_ext(b"\xff\xd8\xff\xe0..."), ".jpg")

    def test_webp(self):
        self.assertEqual(_detect_image_ext(b"RIFF\x00\x00\x00\x00WEBP..."), ".webp")

    def test_unknown(self):
        self.assertEqual(_detect_image_ext(b"garbage"), ".bin")


class EditModeRouting(unittest.TestCase):
    """Verify the edit-mode plumbing: reference saved, instruction reframed."""

    def _run(self, *, reference_b64, request_id="img_test"):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "runs"
            generated = tmp_path / "generated"
            workdir.mkdir()
            generated.mkdir()
            settings = _settings_for(workdir, generated)
            gen = CodexImageGenerator(settings)

            # The generator's loop checks the output path after subprocess
            # returns; create the file so the post-run existence check passes.
            target = generated / f"{request_id}.png"
            target.write_bytes(_make_png_bytes())

            captured = {}

            async def fake_run(self_, *, run_dir, output_path, prompt, size, quality, index, count, reference_path=None):
                captured["run_dir"] = run_dir
                captured["reference_path"] = reference_path
                captured["instruction_kwargs"] = dict(
                    prompt=prompt, size=size, quality=quality,
                    output_path=output_path, index=index, count=count,
                    reference_path=reference_path,
                )
                # Also capture the rendered instruction text so the test can
                # assert against it.
                captured["instruction"] = gen._instruction(**captured["instruction_kwargs"])
                return ("fake-cmd", "fake-stdout", "fake-stderr")

            with patch.object(CodexImageGenerator, "_run_codex_once", new=fake_run):
                asyncio.run(
                    gen.generate(
                        request_id=request_id,
                        prompt="把背景換成夜晚海邊",
                        size="1024x1024",
                        quality="medium",
                        count=3,  # should be clamped to 1 in edit mode
                        reference_image_base64=reference_b64,
                    )
                )
            # snapshot file state while the TemporaryDirectory is still alive
            ref = captured.get("reference_path")
            captured["reference_bytes"] = ref.read_bytes() if ref and ref.exists() else None
            captured["reference_parent_name"] = ref.parent.name if ref else None
            captured["reference_suffix"] = ref.suffix if ref else None
            return captured, workdir / request_id

    def test_reference_saved_with_correct_extension(self):
        png_bytes = _make_png_bytes()
        captured, run_dir = self._run(
            reference_b64=base64.b64encode(png_bytes).decode("ascii"),
            request_id="img_test_ext",
        )
        self.assertIsNotNone(captured["reference_path"])
        self.assertEqual(captured["reference_suffix"], ".png")
        self.assertEqual(captured["reference_bytes"], png_bytes)
        self.assertEqual(captured["reference_parent_name"], "img_test_ext")

    def test_count_clamped_to_one(self):
        captured, _ = self._run(
            reference_b64=base64.b64encode(_make_png_bytes()).decode("ascii")
        )
        # count was passed as 3 but should be 1 by the time _run_codex_once runs
        self.assertEqual(captured["instruction_kwargs"]["count"], 1)

    def test_instruction_uses_edit_phrasing(self):
        captured, _ = self._run(
            reference_b64=base64.b64encode(_make_png_bytes()).decode("ascii")
        )
        text = captured["instruction"]
        # Canonical scaffolding from sample-prompts.md so gpt-5.5 routes to
        # the image_gen tool instead of hand-rolling PIL code
        self.assertIn("Use case: image-edit", text)
        self.assertIn("Input images: Image 1:", text)
        self.assertIn("Primary request:", text)
        # And references the saved reference file by absolute path
        self.assertIn(str(captured["reference_path"].resolve()), text)
        # Belt-and-braces guard against the model writing code
        self.assertIn("Do NOT write Python", text)

    def test_no_reference_keeps_text_to_image_phrasing(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "runs"
            generated = tmp_path / "generated"
            workdir.mkdir()
            generated.mkdir()
            settings = _settings_for(workdir, generated)
            gen = CodexImageGenerator(settings)
            target = generated / "img_text.png"
            target.write_bytes(_make_png_bytes())

            captured = {}

            async def fake_run(self_, *, run_dir, output_path, prompt, size, quality, index, count, reference_path=None):
                captured["instruction"] = gen._instruction(
                    prompt=prompt, size=size, quality=quality,
                    output_path=output_path, index=index, count=count,
                    reference_path=reference_path,
                )
                captured["reference_path"] = reference_path
                return ("fake-cmd", "", "")

            with patch.object(CodexImageGenerator, "_run_codex_once", new=fake_run):
                asyncio.run(
                    gen.generate(
                        request_id="img_text",
                        prompt="a cat",
                        size="1024x1024",
                        quality="medium",
                        count=1,
                        reference_image_base64=None,
                    )
                )

        self.assertIsNone(captured["reference_path"])
        self.assertIn("create an image", captured["instruction"])
        self.assertNotIn("edit", captured["instruction"].lower().split("an")[0])

    def test_invalid_base64_raises(self):
        from app.services.codex_image import CodexGenerationError
        with self.assertRaises(CodexGenerationError):
            asyncio.run(self._raises_bad_b64())

    async def _raises_bad_b64(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "runs"
            generated = tmp_path / "generated"
            workdir.mkdir()
            generated.mkdir()
            settings = _settings_for(workdir, generated)
            gen = CodexImageGenerator(settings)
            await gen.generate(
                request_id="img_bad",
                prompt="x",
                size="1024x1024",
                quality="medium",
                count=1,
                reference_image_base64="!!!not_valid_base64!!!",
            )


if __name__ == "__main__":
    unittest.main()
