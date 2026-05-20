# Multi-Image Edit Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let API callers pass 2–4 reference images per edit request so gpt-image-2 can do composition / outfit-swap / scene-combine, end-to-end through `codex exec --image A --image B -- <prompt>`.

**Architecture:** Add a plural field `reference_images_base64: list[str]` on `ImageGenerateRequest`; keep the existing singular `reference_image_base64` as a backwards-compat alias (resolved via a property on the model). `CodexImageGenerator.generate()` and `_run_codex_once()` shift to a `list[Path]` internally. The codex argv builder loops over the list to emit one `--image` per reference, then `--`, matching codex CLI's variadic flag. The `_instruction()` text enumerates `Image 1: <path1>`, `Image 2: <path2>`, …, which is the scaffolding the built-in `image_gen` tool keys off for multi-image edits.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, asyncio, unittest, codex CLI (variadic `-i, --image <FILE>...`).

---

## File Structure

**Modify:**
- `app/models.py` — add plural field + backwards-compat resolver property
- `app/services/codex_image.py` — `generate()`, `_run_codex_once()`, `_instruction()` switch to list semantics
- `app/services/job_queue.py:144-154` — pass resolved list through to `generate()`
- `tests/test_codex_image_edit.py` — add multi-image tests; update existing single-image tests to assert against the new internal list shape where needed

**No new files.** All changes localized to the existing service / model / queue trio.

---

## Task 1: Pydantic model accepts plural field with singular alias

**Files:**
- Modify: `app/models.py`
- Test: `tests/test_models_multi_image.py` (new — small focused test file)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_models_multi_image.py`:

```python
"""Pydantic-level checks for the multi-image request shape."""

from __future__ import annotations

import unittest

from app.models import ImageGenerateRequest


class MultiImageRequestModel(unittest.TestCase):
    def test_singular_field_still_accepted(self):
        req = ImageGenerateRequest(prompt="x", reference_image_base64="AAA")
        self.assertEqual(req.resolved_reference_images, ["AAA"])

    def test_plural_field_accepted(self):
        req = ImageGenerateRequest(
            prompt="x",
            reference_images_base64=["AAA", "BBB"],
        )
        self.assertEqual(req.resolved_reference_images, ["AAA", "BBB"])

    def test_plural_takes_precedence_when_both_set(self):
        # Defensive: if a buggy caller sends both, the explicit plural wins.
        req = ImageGenerateRequest(
            prompt="x",
            reference_image_base64="OLD",
            reference_images_base64=["NEW1", "NEW2"],
        )
        self.assertEqual(req.resolved_reference_images, ["NEW1", "NEW2"])

    def test_neither_field_returns_empty_list(self):
        req = ImageGenerateRequest(prompt="x")
        self.assertEqual(req.resolved_reference_images, [])

    def test_plural_max_4_images(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            ImageGenerateRequest(
                prompt="x",
                reference_images_base64=["a", "b", "c", "d", "e"],
            )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/ct/codex/codex-image-service && python -m pytest tests/test_models_multi_image.py -v`
Expected: FAIL with `AttributeError: 'ImageGenerateRequest' object has no attribute 'resolved_reference_images'` (and the plural field unknown).

- [ ] **Step 3: Implement the model change**

Replace the body of `app/models.py` with:

```python
from __future__ import annotations

from pydantic import BaseModel, Field


class ImageGenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    size: str = Field(default="1024x1024", pattern=r"^\d{3,4}x\d{3,4}$")
    quality: str = Field(default="medium", pattern=r"^(low|medium|high|auto)$")
    count: int = Field(default=1, ge=1, le=4)
    # Deprecated: kept so existing callers (ctos-lite, catime) keep working.
    # Resolved through resolved_reference_images alongside the plural field.
    reference_image_base64: str | None = Field(default=None, max_length=20_000_000)
    # Up to 4 source images; passed straight to codex CLI as repeated --image
    # flags so gpt-image-2 edit can compose / outfit-swap / scene-merge.
    reference_images_base64: list[str] | None = Field(
        default=None,
        max_length=4,
    )

    @property
    def resolved_reference_images(self) -> list[str]:
        """Unify singular + plural inputs into one list the service consumes.

        Plural wins when both are set — explicit beats legacy.
        """
        if self.reference_images_base64:
            return list(self.reference_images_base64)
        if self.reference_image_base64:
            return [self.reference_image_base64]
        return []


class GeneratedImage(BaseModel):
    url: str
    expires_at: str


class ImageGenerateResponse(BaseModel):
    id: str
    status: str
    images: list[GeneratedImage]
    created_at: str
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/ct/codex/codex-image-service && python -m pytest tests/test_models_multi_image.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
cd /home/ct/codex/codex-image-service
git add app/models.py tests/test_models_multi_image.py
git commit -m "feat(models): accept reference_images_base64 list with singular alias

Add plural reference_images_base64 (max 4) alongside the existing
reference_image_base64. resolved_reference_images consolidates both
into a single list the service layer reads from, so callers can adopt
the plural form without breaking the singular contract."
```

---

## Task 2: Generator stores each reference under reference_N.<ext>

**Files:**
- Modify: `app/services/codex_image.py` (the `generate()` method, lines ~136–237)
- Test: `tests/test_codex_image_edit.py` — add a new test class `MultiImageRouting`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_codex_image_edit.py`:

```python
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

            with patch.object(CodexImageGenerator, "_run_codex_once", new=fake_run):
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

            # snapshot while temp dir is still alive
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
        # When the caller adopts the plural API with one item, naming stays
        # consistent — reference_1.png, not reference.png.
        png = _make_png_bytes()
        captured = self._run(
            reference_b64_list=[base64.b64encode(png).decode("ascii")],
            request_id="img_one_ref_plural",
        )
        names = [name for name, _ in captured["snapshots"]]
        self.assertEqual(names, ["reference_1.png"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/ct/codex/codex-image-service && python -m pytest tests/test_codex_image_edit.py::MultiImageRouting -v`
Expected: FAIL with `TypeError: generate() got an unexpected keyword argument 'reference_images_base64'`.

- [ ] **Step 3: Refactor `generate()` to accept the list**

In `app/services/codex_image.py`, replace the `generate()` method (currently lines ~136–237) with:

```python
    async def generate(
        self,
        *,
        request_id: str,
        prompt: str,
        size: str,
        quality: str,
        count: int,
        reference_image_base64: str | None = None,  # backwards-compat alias
        reference_images_base64: list[str] | None = None,
    ) -> CodexGenerationResult:
        storage.ensure_storage(self.settings)
        run_dir = self.settings.codex_workdir / request_id
        run_dir.mkdir(parents=True, exist_ok=True)

        # Normalize inputs into a single list. Plural wins; singular is a
        # legacy alias for callers that haven't migrated yet.
        if reference_images_base64:
            references_b64 = list(reference_images_base64)
        elif reference_image_base64:
            references_b64 = [reference_image_base64]
        else:
            references_b64 = []

        # Edit mode: decode each base64 reference once and persist it inside
        # the per-job workdir so codex CLI can read each from a stable path.
        # count is clamped to 1 because gpt-image-2 edit returns one image
        # regardless of how many input references the model is given.
        reference_paths: list[Path] = []
        for idx, b64 in enumerate(references_b64, start=1):
            try:
                ref_bytes = base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise CodexGenerationError(
                    f"reference_images_base64[{idx - 1}] is not valid base64: {exc}",
                    workdir=run_dir,
                ) from exc
            if not ref_bytes:
                raise CodexGenerationError(
                    f"reference_images_base64[{idx - 1}] decoded to zero bytes",
                    workdir=run_dir,
                )
            ext = _detect_image_ext(ref_bytes)
            ref_path = run_dir / f"reference_{idx}{ext}"
            ref_path.write_bytes(ref_bytes)
            reference_paths.append(ref_path)
        if reference_paths:
            count = 1  # edit mode is single-output

        image_paths: list[Path] = []
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        command_display = ""
        codex_home_used: str | None = None
        started = time.monotonic()

        try:
            for index in range(count):
                output_path = storage.generated_image_path(self.settings, request_id, index, count)
                command, stdout, stderr, picked_home = await self._run_codex_with_retry(
                    run_dir=run_dir,
                    output_path=output_path,
                    prompt=prompt,
                    size=size,
                    quality=quality,
                    index=index,
                    count=count,
                    reference_paths=reference_paths,
                )
                command_display = command
                codex_home_used = picked_home
                stdout_parts.append(stdout)
                stderr_parts.append(stderr)
                if not output_path.exists():
                    fallback = self._find_generated_image(
                        run_dir,
                        exclude=set(reference_paths),
                    )
                    if not fallback:
                        fallback = _find_generated_in_session(
                            stderr, codex_home=codex_home_used
                        )
                    if fallback:
                        shutil.copy2(fallback, output_path)
                if not output_path.exists():
                    raise CodexGenerationError(
                        f"Codex completed but did not create {output_path}",
                        stdout="\n".join(stdout_parts),
                        stderr="\n".join(stderr_parts),
                        command=command_display,
                        workdir=run_dir,
                    )
                image_paths.append(output_path)
        except Exception:
            for image_path in image_paths:
                if image_path.exists():
                    image_path.unlink(missing_ok=True)
            raise

        return CodexGenerationResult(
            image_paths=image_paths,
            stdout="\n".join(stdout_parts),
            stderr="\n".join(stderr_parts),
            duration_seconds=time.monotonic() - started,
            workdir=run_dir,
            command=command_display,
            codex_home=codex_home_used,
        )
```

Also update `_find_generated_image()` signature — its `exclude` is now `set[Path]` (no `None` ever), so simplify its body. Replace it with:

```python
    def _find_generated_image(
        self, run_dir: Path, exclude: set[Path] | None = None
    ) -> Path | None:
        excluded = {p.resolve() for p in (exclude or set())}
        candidates = [
            path
            for path in run_dir.rglob("*")
            if path.is_file()
            and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
            and path.resolve() not in excluded
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)
```

- [ ] **Step 4: Run tests to verify the new ones pass**

Run: `cd /home/ct/codex/codex-image-service && python -m pytest tests/test_codex_image_edit.py::MultiImageRouting -v`
Expected: 2 passed.

The existing `EditModeRouting` tests will now fail because `_run_codex_once`'s signature changed (`reference_path` → `reference_paths`). That's Task 3's territory — leave them red for now.

- [ ] **Step 5: Commit**

```bash
cd /home/ct/codex/codex-image-service
git add app/services/codex_image.py tests/test_codex_image_edit.py
git commit -m "feat(generator): accept reference_images_base64 list internally

generate() now normalizes singular + plural inputs into one list,
writes each reference as reference_N.<ext>, and threads the list
through to _run_codex_once. Wiring of --image flags and instruction
text comes in the next commit."
```

---

## Task 3: `_run_codex_once()` emits one `--image` per reference

**Files:**
- Modify: `app/services/codex_image.py` — `_run_codex_with_retry()` and `_run_codex_once()` signatures + argv builder
- Test: `tests/test_codex_image_edit.py` — update `EditModeRouting` to the new signature; add a multi-image argv test

- [ ] **Step 1: Update existing `EditModeRouting` tests + add multi-image argv assertion**

In `tests/test_codex_image_edit.py`, change the `EditModeRouting._run` fake to accept `reference_paths` (plural) instead of `reference_path` (singular):

```python
    def _run(self, *, reference_b64, request_id="img_test"):
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
                self_, *, run_dir, output_path, prompt, size, quality, index, count, reference_paths, codex_home=None
            ):
                captured["run_dir"] = run_dir
                captured["reference_paths"] = list(reference_paths)
                captured["instruction_kwargs"] = dict(
                    prompt=prompt, size=size, quality=quality,
                    output_path=output_path, index=index, count=count,
                    reference_paths=reference_paths,
                )
                captured["instruction"] = gen._instruction(**captured["instruction_kwargs"])
                return ("fake-cmd", "fake-stdout", "fake-stderr")

            with patch.object(CodexImageGenerator, "_run_codex_once", new=fake_run):
                asyncio.run(
                    gen.generate(
                        request_id=request_id,
                        prompt="把背景換成夜晚海邊",
                        size="1024x1024",
                        quality="medium",
                        count=3,
                        reference_image_base64=reference_b64,
                    )
                )
            paths = captured.get("reference_paths") or []
            captured["reference_bytes"] = paths[0].read_bytes() if paths else None
            captured["reference_parent_name"] = paths[0].parent.name if paths else None
            captured["reference_suffix"] = paths[0].suffix if paths else None
            return captured, workdir / request_id
```

Update the assertions in `test_instruction_uses_edit_phrasing` and `test_no_reference_keeps_text_to_image_phrasing` to read from `reference_paths` (the new field). Specifically:

- `test_no_reference_keeps_text_to_image_phrasing`: change `captured["reference_path"]` → `captured["reference_paths"]` (and assert it's `[]`).
- `test_instruction_uses_edit_phrasing`: change `captured["reference_path"].resolve()` → `captured["reference_paths"][0].resolve()`.

Also update both `RoundRobinHomes._setup` fake_run sigs to use `reference_paths=()` instead of `reference_path=None`:

```python
        async def fake_run(
            self_, *, run_dir, output_path, prompt, size, quality, index, count, reference_paths=(), codex_home=None
        ):
            captured["homes_used"].append(codex_home)
            captured["instructions"].append(
                self_._instruction(
                    prompt=prompt, size=size, quality=quality,
                    output_path=output_path, index=index, count=count,
                    reference_paths=reference_paths,
                )
            )
            ...
```

Then add a new test class that exercises the real argv builder (not patched):

```python
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
                # Simulate codex writing the file
                # Find -C <workdir> and the output instruction
                target.write_bytes(_make_png_bytes())
                return FakeProc()

            png_b64 = base64.b64encode(_make_png_bytes()).decode("ascii")
            with patch("asyncio.create_subprocess_exec", new=fake_exec):
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
            # Two --image entries, each followed by an absolute path
            image_positions = [i for i, a in enumerate(argv) if a == "--image"]
            self.assertEqual(len(image_positions), 2)
            # The "--" separator must come AFTER the last --image / path pair,
            # before the positional prompt
            sep_pos = argv.index("--")
            self.assertGreater(sep_pos, image_positions[-1] + 1)
            # Last token is the instruction string (positional prompt)
            self.assertTrue(argv[-1].startswith("Call the built-in image_gen"))
```

- [ ] **Step 2: Run tests to verify the failures match expectations**

Run: `cd /home/ct/codex/codex-image-service && python -m pytest tests/test_codex_image_edit.py -v`
Expected: `EditModeRouting` tests now FAIL (because the production code still uses `reference_path` singular), and `CodexArgvBuilder.test_two_references_produce_two_image_flags` FAILS (because `_run_codex_once` rejects `reference_paths`).

- [ ] **Step 3: Update `_run_codex_with_retry()` and `_run_codex_once()` to plural**

In `app/services/codex_image.py`:

Change the signature of `_run_codex_with_retry` — replace `reference_path: Path | None = None` with `reference_paths: list[Path]`, and inside the call to `_run_codex_once` pass `reference_paths=reference_paths`.

Then replace `_run_codex_once` with:

```python
    async def _run_codex_once(
        self,
        *,
        run_dir: Path,
        output_path: Path,
        prompt: str,
        size: str,
        quality: str,
        index: int,
        count: int,
        reference_paths: list[Path],
        codex_home: str | None = None,
    ) -> tuple[str, str, str]:
        instruction = self._instruction(
            prompt=prompt,
            size=size,
            quality=quality,
            output_path=output_path,
            index=index,
            count=count,
            reference_paths=reference_paths,
        )
        command = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            str(run_dir),
        ]
        # Edit mode: attach each reference as its own --image so codex CLI
        # passes every one to the built-in image_gen tool. --image is
        # variadic in clap; the `--` separator before the positional
        # prompt stops the prompt being eaten as another image filename.
        if reference_paths:
            for ref in reference_paths:
                command.extend(["--image", str(ref.resolve())])
            command.append("--")
        command.append(instruction)
        command_display = " ".join(command[:-1]) + " <imagegen prompt>"
        env = os.environ.copy()
        if codex_home:
            env["CODEX_HOME"] = codex_home
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env=env,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=self.settings.codex_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=5
                )
            except asyncio.TimeoutError:
                stdout_bytes, stderr_bytes = b"", b""
            raise CodexGenerationError(
                f"Codex timed out after {self.settings.codex_timeout_seconds} seconds",
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
                command=command_display,
                workdir=run_dir,
            ) from exc

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise CodexGenerationError(
                f"Codex exited with code {process.returncode}",
                stdout=stdout,
                stderr=stderr,
                command=command_display,
                workdir=run_dir,
            )
        return command_display, stdout, stderr
```

The `_instruction()` change is in Task 4; for this task, change its signature to `reference_paths: list[Path]` and keep the body working for the single-reference path (it'll still hard-code `Image 1:` for now — Task 4 enumerates).

Apply this stub for `_instruction()` so this task's tests pass without doing Task 4's work:

```python
    def _instruction(
        self,
        *,
        prompt: str,
        size: str,
        quality: str,
        output_path: Path,
        index: int,
        count: int,
        reference_paths: list[Path],
    ) -> str:
        image_label = f"image {index + 1} of {count}" if count > 1 else "the image"
        if reference_paths:
            ref = reference_paths[0]
            return (
                "Call the built-in image_gen tool to edit the input image. "
                "Do NOT write Python, shell, or any code to transform the "
                "image yourself — the only correct action is one call to "
                "image_gen with the user's edit request.\n"
                "$imagegen\n"
                "Use case: image-edit\n"
                f"Input images: Image 1: {ref.resolve()}\n"
                f"Primary request: {prompt}\n"
                "Constraints: preserve the subject identity, framing, and "
                "geometry of Image 1 except where the request asks otherwise.\n"
                f"Output size: {size}\n"
                f"Quality: {quality}\n"
                f"Save the resulting PNG image exactly at this path: {output_path.resolve()}\n"
                "If image_gen writes the file elsewhere first, copy it to the "
                "exact path above. Do not create or modify any other project "
                "files. Final answer should only contain the saved image path."
            )
        return (
            "Call the built-in image_gen tool to create an image. "
            "Do NOT write Python, shell, or any code (PIL / Pillow, ImageMagick, "
            "matplotlib, manual SVG, etc.) to draw the image yourself — the only "
            "correct action is one call to image_gen with the user's prompt. "
            "If image_gen produces output that does not perfectly match the prompt "
            "(e.g. text rendering issues for non-Latin scripts), still return what "
            "image_gen gave us — do not 'fix' it with code.\n"
            "$imagegen\n"
            f"User prompt for {image_label}: {prompt}\n"
            f"Size: {size}\n"
            f"Quality: {quality}\n"
            f"Save the final PNG image exactly at this path: {output_path.resolve()}\n"
            "If image_gen writes the file elsewhere first, copy it to the exact "
            "path above. Do not create or modify any other project files. "
            "Final answer should only contain the saved image path."
        )
```

- [ ] **Step 4: Run all edit-mode tests**

Run: `cd /home/ct/codex/codex-image-service && python -m pytest tests/test_codex_image_edit.py -v`
Expected: All pass (`DetectExt` 4, `EditModeRouting` 5, `MultiImageRouting` 2, `CodexArgvBuilder` 1, `RoundRobinHomes` 5).

- [ ] **Step 5: Commit**

```bash
cd /home/ct/codex/codex-image-service
git add app/services/codex_image.py tests/test_codex_image_edit.py
git commit -m "feat(codex): emit one --image flag per reference path

_run_codex_once now loops over reference_paths to build the codex
argv: --image p1 --image p2 ... -- <prompt>. Existing single-image
behaviour preserved (one path → one --image flag). Multi-image
edit composition / outfit-swap / scene-merge now flow through
to gpt-image-2 edit."
```

---

## Task 4: `_instruction()` enumerates every input image

**Files:**
- Modify: `app/services/codex_image.py` — body of `_instruction()` only
- Test: `tests/test_codex_image_edit.py` — add a multi-image instruction test

- [ ] **Step 1: Write the failing test**

Append to `tests/test_codex_image_edit.py` (inside the existing `MultiImageRouting` class):

```python
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
```

- [ ] **Step 2: Run the new test to verify it fails**

Run: `cd /home/ct/codex/codex-image-service && python -m pytest tests/test_codex_image_edit.py::MultiImageRouting::test_instruction_enumerates_each_image -v`
Expected: FAIL — `Image 2:` and `Image 3:` are not in the instruction yet, and the `geometry of Image 1 except` substring is still there.

- [ ] **Step 3: Generalize `_instruction()` to enumerate every reference**

In `app/services/codex_image.py`, replace the edit branch of `_instruction()` with:

```python
        if reference_paths:
            input_lines = "\n".join(
                f"Image {i}: {p.resolve()}" for i, p in enumerate(reference_paths, start=1)
            )
            if len(reference_paths) == 1:
                constraint = (
                    "Constraints: preserve the subject identity, framing, and "
                    "geometry of the input image except where the request asks otherwise."
                )
            else:
                constraint = (
                    "Constraints: treat the input images as references the user is "
                    "composing with — preserve identity and content from each image "
                    "as the request implies (e.g. subject from Image 1, scene from "
                    "Image 2). The prompt below tells you how to combine them."
                )
            return (
                "Call the built-in image_gen tool to edit using the input image(s). "
                "Do NOT write Python, shell, or any code to transform the "
                "image yourself — the only correct action is one call to "
                "image_gen with the user's edit request.\n"
                "$imagegen\n"
                "Use case: image-edit\n"
                f"Input images:\n{input_lines}\n"
                f"Primary request: {prompt}\n"
                f"{constraint}\n"
                f"Output size: {size}\n"
                f"Quality: {quality}\n"
                f"Save the resulting PNG image exactly at this path: {output_path.resolve()}\n"
                "If image_gen writes the file elsewhere first, copy it to the "
                "exact path above. Do not create or modify any other project "
                "files. Final answer should only contain the saved image path."
            )
```

Note the single-image branch of `EditModeRouting.test_instruction_uses_edit_phrasing` asserts `"Input images: Image 1:"` — that substring still matches the new multi-line form (`"Input images:\nImage 1:"`) only if we don't break the `Input images:` literal. The test uses `assertIn("Input images: Image 1:", text)` — that **will break**. Update that test to:

```python
        self.assertIn("Image 1:", text)
        self.assertIn("Input images:", text)
```

- [ ] **Step 4: Run all edit-mode tests**

Run: `cd /home/ct/codex/codex-image-service && python -m pytest tests/test_codex_image_edit.py -v`
Expected: all pass (now including the new `test_instruction_enumerates_each_image`).

- [ ] **Step 5: Commit**

```bash
cd /home/ct/codex/codex-image-service
git add app/services/codex_image.py tests/test_codex_image_edit.py
git commit -m "feat(prompt): enumerate Input images: Image 1..N in edit instruction

When multiple references are attached, the prompt scaffolding now
lists each (Image 1, Image 2, ...) and the constraint line shifts
to composition language (subject from Image 1, scene from Image 2)
so gpt-image-2 knows how to combine them."
```

---

## Task 5: Wire `job_queue._run_job` to pass the resolved list

**Files:**
- Modify: `app/services/job_queue.py:144-154`
- Test: `tests/test_codex_image_edit.py` — small integration assertion via the request model

- [ ] **Step 1: Write the failing test**

Append a new class to `tests/test_codex_image_edit.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /home/ct/codex/codex-image-service && python -m pytest tests/test_codex_image_edit.py::JobQueueWiring -v`
Expected: FAIL — `captured.get("reference_images_base64")` is None because `_run_job` currently passes `reference_image_base64=job.payload.reference_image_base64` (singular only).

- [ ] **Step 3: Update `_run_job` to forward the resolved list**

In `app/services/job_queue.py`, change the call inside `_run_job` (around lines 147–154):

```python
            result = await self.generator.generate(
                request_id=job.request_id,
                prompt=job.payload.prompt,
                size=job.payload.size,
                quality=job.payload.quality,
                count=job.payload.count,
                reference_images_base64=job.payload.resolved_reference_images or None,
            )
```

(Pass `None` when the list is empty so the generator can keep treating "no references" as text-to-image cleanly.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd /home/ct/codex/codex-image-service && python -m pytest tests/test_codex_image_edit.py::JobQueueWiring -v`
Expected: PASS.

- [ ] **Step 5: Run the full test suite**

Run: `cd /home/ct/codex/codex-image-service && python -m pytest tests/ -v`
Expected: all tests pass (cleanup, security, codex_image_edit, models_multi_image).

- [ ] **Step 6: Commit**

```bash
cd /home/ct/codex/codex-image-service
git add app/services/job_queue.py tests/test_codex_image_edit.py
git commit -m "feat(queue): forward resolved_reference_images to generator

_run_job now reads the unified resolved_reference_images list off
the payload (handling both legacy singular and new plural fields)
and passes it as reference_images_base64 to the generator."
```

---

## Task 6: README + example payload bump

**Files:**
- Modify: `README.md` (if it documents the singular field; otherwise skip and note)
- Modify: `examples/` (if there are curl examples; otherwise skip)

- [ ] **Step 1: Check README and examples**

```bash
cd /home/ct/codex/codex-image-service
grep -n "reference_image_base64" README.md examples/ 2>/dev/null
```

If matches exist, update them to mention the plural form and explain it's an array of up to 4 base64 strings. If no matches, this task is a no-op — proceed to Step 2 with a doc-only commit or skip the commit.

- [ ] **Step 2: Update docs (if applicable)**

For each match in README.md, add an example like:

````markdown
For multi-image composition (e.g. "put person from image 1 into image 2's scene"):

```json
{
  "prompt": "put the person from image 1 into the kitchen in image 2",
  "reference_images_base64": ["<base64-png-1>", "<base64-png-2>"],
  "size": "1024x1024",
  "quality": "medium"
}
```

`reference_images_base64` accepts 1-4 base64-encoded images. The legacy
`reference_image_base64` (singular string) is still supported and treated
as a one-element list.
````

- [ ] **Step 3: Commit (if files changed)**

```bash
cd /home/ct/codex/codex-image-service
git add README.md examples/
git commit -m "docs: document reference_images_base64 multi-image input"
```

---

## Manual smoke test (post-merge, before tagging ctos-lite work)

Before starting PR 2 (ctos-lite), run a real codex call against the local service to confirm gpt-image-2 actually does multi-image composition. **This burns ChatGPT quota — one call only.**

```bash
cd /home/ct/codex/codex-image-service
# 1. Encode two real photos to base64
A=$(base64 -w0 < ~/some_person.png)
B=$(base64 -w0 < ~/some_kitchen.png)

# 2. Hit the local service
curl -s -X POST http://localhost:<port>/v1/images/generate \
  -H "Authorization: Bearer <cimg_dev_key>" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg a "$A" --arg b "$B" '{
    prompt: "put the person from image 1 into the kitchen in image 2, keep their face and outfit",
    reference_images_base64: [$a, $b],
    size: "1024x1024",
    quality: "medium"
  }')" | jq .

# 3. Open the returned URL — confirm output is a real composition,
#    not just a re-render of one of the inputs.
```

If the result looks like just image 1 or just image 2 (i.e. gpt-image-2 ignored one), the `_instruction()` constraint phrasing in Task 4 may need tweaking — that's the only knob.

---

## Out-of-scope for this plan (deferred to PR 2)

- `ctos-lite` MCP `generate_image_tool(reference_images=...)` and the `_detect_image_edit_target` change that returns a list. That work is its own plan (`docs/superpowers/plans/2026-05-20-ctos-lite-multi-image-edit.md`) because it lives in a different repo and crosses LINE-message-grouping concerns.
- Vertex / gemini-web / FLUX multi-image routing in ctos-lite — handled in PR 2.
- Size limit: total bytes across all references. Currently each item inherits `max_length=20_000_000` from the singular field's `Field(...)`, but Pydantic's `max_length` on a list constrains the count, not the bytes. If we hit memory pressure in production we can add a total-byte guard later.
