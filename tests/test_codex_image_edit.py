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
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, AsyncMock

from app import db
from app.config import Settings
from app.services.codex_image import CodexImageGenerator, _detect_image_ext


@contextmanager
def _rollout_returns_image():
    """讓 generate() 拿得到圖。

    PR#12 之後 generate() 只認 session rollout 裡的 base64（不再相信 codex 自己
    複製到 output_path 的檔案，那是重複圖 bug 的來源），rollout 沒圖就一律當
    真失敗。下面這些測試測的是 argv／instruction／round-robin，不是回收路徑，
    所以直接讓 rollout 交回一張合法 PNG。
    """
    with patch(
        "app.services.codex_image._find_image_in_rollout",
        return_value=_make_png_bytes(),
    ):
        yield


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


def _settings_for(workdir: Path, generated: Path, homes: tuple[str, ...] = ()) -> Settings:
    """generate() 收尾會查內容雜湊去重（PR#9），那要求 image_requests 表存在，
    所以這裡順手把 schema 建起來 —— 否則測 argv 的用例會死在 sqlite
    'no such table'，跟它想測的東西毫無關係。"""
    settings = Settings(
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
        codex_homes=homes,
        generation_queue_max_size=1,
        request_wait_timeout_seconds=5,
        image_retention_days=7,
        cleanup_interval_hours=6,
    )
    db.init_db(settings)
    return settings


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

            async def fake_run(self_, *, run_dir, output_path, prompt, size, quality, index, count, reference_paths, codex_home=None):
                captured["run_dir"] = run_dir
                captured["reference_paths"] = list(reference_paths)
                captured["instruction_kwargs"] = dict(
                    prompt=prompt, size=size, quality=quality,
                    output_path=output_path, index=index, count=count,
                    reference_paths=reference_paths,
                )
                # Also capture the rendered instruction text so the test can
                # assert against it.
                captured["instruction"] = gen._instruction(**captured["instruction_kwargs"])
                return ("fake-cmd", "fake-stdout", "fake-stderr")

            with patch.object(CodexImageGenerator, "_run_codex_once", new=fake_run), _rollout_returns_image():
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
            paths = captured.get("reference_paths") or []
            captured["reference_bytes"] = paths[0].read_bytes() if paths else None
            captured["reference_parent_name"] = paths[0].parent.name if paths else None
            captured["reference_suffix"] = paths[0].suffix if paths else None
            return captured, workdir / request_id

    def test_reference_saved_with_correct_extension(self):
        png_bytes = _make_png_bytes()
        captured, run_dir = self._run(
            reference_b64=base64.b64encode(png_bytes).decode("ascii"),
            request_id="img_test_ext",
        )
        self.assertTrue(len(captured["reference_paths"]) > 0)
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
        self.assertIn("Image 1:", text)
        self.assertIn("Input images:", text)
        self.assertIn("Primary request:", text)
        # And references the saved reference file by absolute path
        self.assertIn(str(captured["reference_paths"][0].resolve()), text)
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

            async def fake_run(self_, *, run_dir, output_path, prompt, size, quality, index, count, reference_paths, codex_home=None):
                captured["instruction"] = gen._instruction(
                    prompt=prompt, size=size, quality=quality,
                    output_path=output_path, index=index, count=count,
                    reference_paths=reference_paths,
                )
                captured["reference_paths"] = list(reference_paths)
                return ("fake-cmd", "", "")

            with patch.object(CodexImageGenerator, "_run_codex_once", new=fake_run), _rollout_returns_image():
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

        self.assertEqual(captured["reference_paths"], [])
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


class RoundRobinHomes(unittest.TestCase):
    """CODEX_HOMES round-robin + cross-account retry."""

    def _setup(self, homes, fake_run_side_effect):
        """Returns (captured, generator) ready for a generate() call.

        fake_run_side_effect: list of return values / exceptions per call.
        """
        from app.services.codex_image import CodexGenerationError, CodexImageGenerator

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_path = Path(tmp.name)
        workdir = tmp_path / "runs"
        generated = tmp_path / "generated"
        workdir.mkdir()
        generated.mkdir()
        settings = _settings_for(workdir, generated, homes=homes)
        gen = CodexImageGenerator(settings)
        target = generated / "img_rr.png"
        target.write_bytes(_make_png_bytes())

        captured = {"homes_used": [], "instructions": []}
        call_idx = [0]
        png_bytes = _make_png_bytes()

        async def fake_run(self_, *, run_dir, output_path, prompt, size, quality, index, count, reference_paths=(), codex_home=None):
            captured["homes_used"].append(codex_home)
            captured["instructions"].append(
                self_._instruction(
                    prompt=prompt, size=size, quality=quality,
                    output_path=output_path, index=index, count=count,
                    reference_paths=reference_paths,
                )
            )
            i = call_idx[0]
            call_idx[0] += 1
            side = fake_run_side_effect[i]
            if isinstance(side, Exception):
                raise side
            # On success, write a fake PNG to output_path so the
            # generate() loop's existence check passes.
            output_path.write_bytes(png_bytes)
            return side

        return captured, gen, fake_run

    def test_round_robin_advances_per_request(self):
        from app.services.codex_image import CodexImageGenerator
        captured, gen, fake_run = self._setup(
            homes=("/h/a", "/h/b"),
            fake_run_side_effect=[("cmd", "out", "err")] * 3,
        )
        with patch.object(CodexImageGenerator, "_run_codex_once", new=fake_run), _rollout_returns_image():
            for _ in range(3):
                asyncio.run(gen.generate(
                    request_id=f"img_rr_{_}",
                    prompt="x", size="1024x1024", quality="medium", count=1,
                ))
        # /h/a, /h/b, /h/a — round-robin
        self.assertEqual(captured["homes_used"], ["/h/a", "/h/b", "/h/a"])

    def test_empty_homes_passes_none(self):
        from app.services.codex_image import CodexImageGenerator
        captured, gen, fake_run = self._setup(
            homes=(),
            fake_run_side_effect=[("cmd", "out", "err")],
        )
        with patch.object(CodexImageGenerator, "_run_codex_once", new=fake_run), _rollout_returns_image():
            asyncio.run(gen.generate(
                request_id="img_none", prompt="x",
                size="1024x1024", quality="medium", count=1,
            ))
        # No CODEX_HOMES configured → codex_home=None (container default)
        self.assertEqual(captured["homes_used"], [None])

    def test_first_home_fails_falls_back_to_second(self):
        from app.services.codex_image import CodexImageGenerator, CodexGenerationError
        captured, gen, fake_run = self._setup(
            homes=("/h/a", "/h/b"),
            fake_run_side_effect=[
                CodexGenerationError("A timeout", stderr="A failed"),
                ("cmd", "out-b", "err-b"),  # B succeeds
            ],
        )
        with patch.object(CodexImageGenerator, "_run_codex_once", new=fake_run), _rollout_returns_image():
            result = asyncio.run(gen.generate(
                request_id="img_retry", prompt="x",
                size="1024x1024", quality="medium", count=1,
            ))
        # First attempt /h/a failed, retried with /h/b
        self.assertEqual(captured["homes_used"], ["/h/a", "/h/b"])
        # Final result reports /h/b as the home that succeeded
        self.assertEqual(result.codex_home, "/h/b")

    def test_both_homes_fail_raises(self):
        from app.services.codex_image import CodexImageGenerator, CodexGenerationError
        captured, gen, fake_run = self._setup(
            homes=("/h/a", "/h/b"),
            fake_run_side_effect=[
                CodexGenerationError("A timeout", stderr="A failed"),
                CodexGenerationError("B 401", stderr="B failed"),
            ],
        )
        with patch.object(CodexImageGenerator, "_run_codex_once", new=fake_run), _rollout_returns_image():
            with self.assertRaises(CodexGenerationError):
                asyncio.run(gen.generate(
                    request_id="img_double_fail", prompt="x",
                    size="1024x1024", quality="medium", count=1,
                ))
        self.assertEqual(captured["homes_used"], ["/h/a", "/h/b"])

    def test_single_home_no_retry(self):
        from app.services.codex_image import CodexImageGenerator, CodexGenerationError
        captured, gen, fake_run = self._setup(
            homes=("/h/only",),
            fake_run_side_effect=[
                CodexGenerationError("only one fails", stderr="alone"),
            ],
        )
        with patch.object(CodexImageGenerator, "_run_codex_once", new=fake_run), _rollout_returns_image():
            with self.assertRaises(CodexGenerationError):
                asyncio.run(gen.generate(
                    request_id="img_single", prompt="x",
                    size="1024x1024", quality="medium", count=1,
                ))
        # Only one attempt — no retry when only one home configured
        self.assertEqual(captured["homes_used"], ["/h/only"])


class MultiImageRouting(unittest.TestCase):
    """Verify multi-image edit plumbing: every reference is saved, list flows down."""

    def _run(self, *, reference_b64_list, request_id="img_multi"):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "runs"
            generated = tmp_path / "generated"
            workdir.mkdir()
            generated.mkdir()
            settings = _settings_for(workdir, generated)
            gen = CodexImageGenerator(settings)

            target = generated / f"{request_id}.png"
            target.write_bytes(_make_png_bytes())

            captured = {}

            async def fake_run(
                self_,
                *,
                run_dir,
                output_path,
                prompt,
                size,
                quality,
                index,
                count,
                reference_paths,
                codex_home=None,
            ):
                captured["reference_paths"] = list(reference_paths)
                captured["instruction"] = gen._instruction(
                    prompt=prompt,
                    size=size,
                    quality=quality,
                    output_path=output_path,
                    index=index,
                    count=count,
                    reference_paths=reference_paths,
                )
                return ("fake-cmd", "fake-stdout", "fake-stderr")

            with patch.object(CodexImageGenerator, "_run_codex_once", new=fake_run), _rollout_returns_image():
                asyncio.run(
                    gen.generate(
                        request_id=request_id,
                        prompt="把第一張人物放到第二張的場景",
                        size="1024x1024",
                        quality="medium",
                        count=3,  # edit mode still clamps to 1
                        reference_images_base64=reference_b64_list,
                    )
                )

            snapshots = [(p.name, p.read_bytes()) for p in captured["reference_paths"]]
            captured["snapshots"] = snapshots
            return captured

    def test_two_references_saved_with_indexed_names(self):
        png = _make_png_bytes()
        captured = self._run(
            reference_b64_list=[
                base64.b64encode(png).decode("ascii"),
                base64.b64encode(png).decode("ascii"),
            ],
            request_id="img_two_refs",
        )
        names = [name for name, _ in captured["snapshots"]]
        self.assertEqual(names, ["reference_1.png", "reference_2.png"])
        for _, blob in captured["snapshots"]:
            self.assertEqual(blob, png)

    def test_single_reference_via_plural_field_uses_indexed_name(self):
        png = _make_png_bytes()
        captured = self._run(
            reference_b64_list=[base64.b64encode(png).decode("ascii")],
            request_id="img_one_ref_plural",
        )
        names = [name for name, _ in captured["snapshots"]]
        self.assertEqual(names, ["reference_1.png"])

    def test_instruction_enumerates_each_image(self):
        png = _make_png_bytes()
        captured = self._run(
            reference_b64_list=[
                base64.b64encode(png).decode("ascii"),
                base64.b64encode(png).decode("ascii"),
                base64.b64encode(png).decode("ascii"),
            ],
            request_id="img_three_refs",
        )
        text = captured["instruction"]
        # The scaffolding lists every input image explicitly so image_gen
        # knows which is which when the prompt references them by number.
        self.assertIn("Image 1:", text)
        self.assertIn("Image 2:", text)
        self.assertIn("Image 3:", text)
        # The constraint line should generalize to all input images, not
        # name only Image 1.
        self.assertNotIn("geometry of Image 1 except", text)
        # All three reference paths should be embedded
        for ref in captured["reference_paths"]:
            self.assertIn(str(ref.resolve()), text)


class CodexArgvBuilder(unittest.TestCase):
    """Verify the real subprocess argv has one --image per reference and -- after."""

    def test_two_references_produce_two_image_flags(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "runs"
            generated = tmp_path / "generated"
            workdir.mkdir()
            generated.mkdir()
            settings = _settings_for(workdir, generated)
            gen = CodexImageGenerator(settings)
            target = generated / "img_argv.png"
            target.write_bytes(_make_png_bytes())

            captured_argv = {}

            class FakeProc:
                returncode = 0

                async def communicate(self):
                    return b"stdout", b"session id: 00000000-0000-0000-0000-000000000000\n"

            async def fake_exec(*args, **kwargs):
                captured_argv["argv"] = list(args)
                captured_argv["env"] = kwargs.get("env")
                target.write_bytes(_make_png_bytes())
                return FakeProc()

            png_b64 = base64.b64encode(_make_png_bytes()).decode("ascii")
            with patch("asyncio.create_subprocess_exec", new=fake_exec), _rollout_returns_image():
                asyncio.run(
                    gen.generate(
                        request_id="img_argv",
                        prompt="put person from image 1 into image 2",
                        size="1024x1024",
                        quality="medium",
                        count=1,
                        reference_images_base64=[png_b64, png_b64],
                    )
                )

            argv = captured_argv["argv"]
            image_positions = [i for i, a in enumerate(argv) if a == "--image"]
            self.assertEqual(len(image_positions), 2)
            sep_pos = argv.index("--")
            self.assertGreater(sep_pos, image_positions[-1] + 1)
            self.assertTrue(argv[-1].startswith("Call the built-in image_gen"))


class JobQueueWiring(unittest.TestCase):
    """End-to-end: ImageGenerateRequest → _run_job → generate() carries the list."""

    def test_run_job_forwards_resolved_reference_images(self):
        from datetime import timedelta
        from unittest.mock import patch
        from app import db
        from app.services.job_queue import ImageJobQueue, ImageJob
        from app.services.codex_image import CodexGenerationResult
        from app.models import ImageGenerateRequest

        async def scenario():
            with TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                workdir = tmp_path / "runs"
                generated = tmp_path / "generated"
                workdir.mkdir()
                generated.mkdir()
                settings = _settings_for(workdir, generated)
                db.init_db(settings)
                api_key, _ = db.create_api_key(settings, "wiring-test")
                payload = ImageGenerateRequest(
                    prompt="combine these two",
                    reference_images_base64=["AAAA", "BBBB"],
                )
                db.insert_image_request(
                    settings,
                    request_id="job_wiring",
                    api_key_id=api_key["id"],
                    prompt=payload.prompt,
                    size=payload.size,
                    quality=payload.quality,
                    count=payload.count,
                    expires_at=(db.utc_now() + timedelta(days=1)).isoformat(),
                )
                queue = ImageJobQueue(settings)
                loop = asyncio.get_running_loop()
                job = ImageJob(
                    request_id="job_wiring",
                    api_key_id=api_key["id"],
                    payload=payload,
                    expires_at=(db.utc_now() + timedelta(days=1)).isoformat(),
                    future=loop.create_future(),
                )

                captured = {}

                async def fake_generate(**kwargs):
                    captured.update(kwargs)
                    fake_image = generated / "job_wiring_0.png"
                    fake_image.write_bytes(_make_png_bytes())
                    return CodexGenerationResult(
                        image_paths=[fake_image],
                        stdout="",
                        stderr="",
                        duration_seconds=0.1,
                        workdir=workdir / "job_wiring",
                        command="fake",
                    )

                with patch.object(queue.generator, "generate", new=fake_generate):
                    await queue._run_job(job)
                return captured

        captured = asyncio.run(scenario())
        self.assertEqual(
            captured.get("reference_images_base64"),
            ["AAAA", "BBBB"],
        )


if __name__ == "__main__":
    unittest.main()
