from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.services import storage


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


class CodexImageGenerator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def generate(
        self,
        *,
        request_id: str,
        prompt: str,
        size: str,
        quality: str,
        count: int,
    ) -> CodexGenerationResult:
        storage.ensure_storage(self.settings)
        run_dir = self.settings.codex_workdir / request_id
        run_dir.mkdir(parents=True, exist_ok=True)

        image_paths: list[Path] = []
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        command_display = ""
        started = time.monotonic()

        try:
            for index in range(count):
                output_path = storage.generated_image_path(self.settings, request_id, index, count)
                command, stdout, stderr = await self._run_codex_once(
                    run_dir=run_dir,
                    output_path=output_path,
                    prompt=prompt,
                    size=size,
                    quality=quality,
                    index=index,
                    count=count,
                )
                command_display = command
                stdout_parts.append(stdout)
                stderr_parts.append(stderr)
                if not output_path.exists():
                    fallback = self._find_generated_image(run_dir)
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
        )

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
    ) -> tuple[str, str, str]:
        instruction = self._instruction(
            prompt=prompt,
            size=size,
            quality=quality,
            output_path=output_path,
            index=index,
            count=count,
        )
        command = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            str(run_dir),
            instruction,
        ]
        command_display = " ".join(command[:-1]) + " <imagegen prompt>"
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=self.settings.codex_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            stdout_bytes, stderr_bytes = await process.communicate()
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
    ) -> str:
        image_label = f"image {index + 1} of {count}" if count > 1 else "the image"
        return (
            "Use Codex image generation directly to create an image.\n"
            "$imagegen\n"
            f"User prompt for {image_label}: {prompt}\n"
            f"Size: {size}\n"
            f"Quality: {quality}\n"
            f"Save the final PNG image exactly at this path: {output_path.resolve()}\n"
            "Do not create or modify any other project files. "
            "If a generated file is placed elsewhere first, copy it to the exact path above. "
            "Final answer should only contain the saved image path."
        )

    def _find_generated_image(self, run_dir: Path) -> Path | None:
        candidates = [
            path
            for path in run_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)

