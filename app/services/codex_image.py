from __future__ import annotations

import asyncio
import base64
import binascii
import os
import re
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.services import storage


_SESSION_ID_RE = re.compile(r"^session id:\s*([0-9a-fA-F-]+)\s*$", re.MULTILINE)


def _codex_home(explicit: str | None = None) -> Path:
    """Where Codex CLI stashes its generated_images output.

    Each subprocess run uses its own CODEX_HOME (round-robin or per-account
    routing). Pass the home the subprocess was given so we look in the
    matching tree, not the service process's own env.
    """
    if explicit:
        return Path(explicit)
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def _find_generated_in_session(stderr: str, codex_home: str | None = None) -> Path | None:
    """Extract Codex's session id from stderr and locate the image_gen output.

    The built-in image_gen tool writes to
    ``$CODEX_HOME/generated_images/<session_id>/ig_<hex>.png`` on every call.
    In edit mode the model often skips the follow-up copy step, so we have to
    fish the file out ourselves. Pass the per-attempt codex_home — without it,
    multi-account runs look in the wrong tree and recovery silently fails.
    """
    match = _SESSION_ID_RE.search(stderr or "")
    if not match:
        return None
    session_id = match.group(1).strip()
    session_dir = _codex_home(codex_home) / "generated_images" / session_id
    if not session_dir.is_dir():
        return None
    candidates = sorted(
        (
            p
            for p in session_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _detect_image_ext(data: bytes) -> str:
    """Pick a sensible filename extension from magic bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ".gif"
    return ".bin"


class CodexGenerationError(Exception):
    def __init__(
        self,
        message: str,
        *,
        stdout: str = "",
        stderr: str = "",
        command: str = "",
        workdir: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.command = command
        self.workdir = workdir


@dataclass
class CodexGenerationResult:
    image_paths: list[Path]
    stdout: str
    stderr: str
    duration_seconds: float
    workdir: Path
    command: str
    # The CODEX_HOME used for the final successful attempt. None when no
    # multi-home rotation was configured (fall back to container default).
    codex_home: str | None = None


class CodexImageGenerator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # Round-robin counter across configured CODEX_HOME accounts.
        # Empty tuple = use container default ($HOME/.codex), no rotation.
        self._home_cursor = 0
        self._home_lock = asyncio.Lock()

    async def _claim_primary_base(self) -> int:
        """Reserve the next round-robin slot for one request.

        Returns the index this request's PRIMARY attempt should use; the
        cursor is bumped exactly once per request, regardless of how many
        retry attempts follow. Empty homes → returns 0 (caller ignores it).
        """
        homes = self.settings.codex_homes
        if not homes:
            return 0
        async with self._home_lock:
            base = self._home_cursor
            self._home_cursor = (self._home_cursor + 1) % len(homes)
        return base

    def _home_for_attempt(self, base: int, attempt_index: int) -> str | None:
        """Look up the CODEX_HOME path for attempt N of a request whose
        primary slot was `base`. attempt_index=0 = primary; 1+ = retry
        on the next account in the rotation (when more than one is set)."""
        homes = self.settings.codex_homes
        if not homes:
            return None
        return homes[(base + attempt_index) % len(homes)]

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

    async def _run_codex_with_retry(
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
    ) -> tuple[str, str, str, str | None]:
        """Wrap _run_codex_once with cross-account retry.

        Picks the next CODEX_HOME via round-robin for the primary attempt.
        If it fails (CodexGenerationError) AND there's at least one other
        configured home, retry once on the next account. Returns the
        final (command_display, stdout, stderr, codex_home_used) tuple.
        """
        homes = self.settings.codex_homes
        max_attempts = 1 if len(homes) <= 1 else 2
        # Reserve a primary slot once; retries step from there without
        # double-advancing the global cursor.
        primary_base = await self._claim_primary_base()
        last_error: CodexGenerationError | None = None
        for attempt in range(max_attempts):
            picked_home = self._home_for_attempt(primary_base, attempt)
            try:
                command, stdout, stderr = await self._run_codex_once(
                    run_dir=run_dir,
                    output_path=output_path,
                    prompt=prompt,
                    size=size,
                    quality=quality,
                    index=index,
                    count=count,
                    reference_paths=reference_paths,
                    codex_home=picked_home,
                )
                return command, stdout, stderr, picked_home
            except CodexGenerationError as exc:
                last_error = exc
                if attempt + 1 >= max_attempts:
                    raise
                # Don't bail — try the next home. Surface the prior failure
                # as a stderr breadcrumb so the DB row tells the story.
                logger_prefix = (
                    f"[cross-account retry] CODEX_HOME={picked_home or 'default'}"
                    f" failed: {exc}\n"
                )
                # Mutate the existing exception's bookkeeping so the next
                # round_once writes into a clean slate (workdir is shared
                # across attempts; that's intentional, the reference image
                # stays put).
                exc.stderr = logger_prefix + (exc.stderr or "")
                # Pre-pend that breadcrumb into the retry's stderr aggregate
                # via mutation of self (cheap shim): stash on the run_dir.
                _crumb_path = run_dir / "_retry_crumbs.log"
                try:
                    with _crumb_path.open("a", encoding="utf-8") as fh:
                        fh.write(logger_prefix)
                except OSError:
                    pass
                continue
        # unreachable, but keep mypy happy
        assert last_error is not None
        raise last_error

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
        # start_new_session=True puts codex in its own process group so we can
        # kill its bash / python descendants on timeout. Without this,
        # `process.kill()` only stops the codex binary and orphaned children
        # (e.g. PIL hand-drawing scripts) keep stdout/stderr open, blocking
        # process.communicate() forever and wedging the worker.
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

