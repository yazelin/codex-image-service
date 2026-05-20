from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.security import generate_api_key, hash_api_key


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def connect(settings: Settings | None = None) -> sqlite3.Connection:
    settings = settings or get_settings()
    db_path = settings.database_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def init_db(settings: Settings | None = None) -> None:
    with connect(settings) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                key_hash TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                requests_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS image_requests (
                id TEXT PRIMARY KEY,
                api_key_id TEXT,
                prompt TEXT NOT NULL,
                size TEXT NOT NULL,
                quality TEXT NOT NULL,
                count INTEGER NOT NULL,
                status TEXT NOT NULL,
                image_paths TEXT NOT NULL DEFAULT '[]',
                stdout TEXT,
                stderr TEXT,
                error TEXT,
                duration_seconds REAL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                expires_at TEXT NOT NULL,
                workdir TEXT,
                codex_command TEXT,
                FOREIGN KEY(api_key_id) REFERENCES api_keys(id)
            );

            CREATE INDEX IF NOT EXISTS idx_image_requests_created_at
                ON image_requests(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_image_requests_expires_at
                ON image_requests(expires_at);
            CREATE INDEX IF NOT EXISTS idx_image_requests_status
                ON image_requests(status);
            """
        )
        # codex_home tracks which CODEX_HOME account ran the request
        # (multi-account round-robin). Older DBs may not have this column.
        existing_cols = {
            row[1] for row in connection.execute("PRAGMA table_info(image_requests)").fetchall()
        }
        if "codex_home" not in existing_cols:
            connection.execute("ALTER TABLE image_requests ADD COLUMN codex_home TEXT")


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def create_api_key(settings: Settings, name: str) -> tuple[dict[str, Any], str]:
    api_key = generate_api_key()
    key_hash = hash_api_key(api_key)
    key_id = f"key_{api_key[-12:]}"
    now = iso_now()
    with connect(settings) as connection:
        connection.execute(
            """
            INSERT INTO api_keys (id, name, key_hash, enabled, created_at)
            VALUES (?, ?, ?, 1, ?)
            """,
            (key_id, name.strip() or "Unnamed key", key_hash, now),
        )
        row = connection.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
    return row_to_dict(row) or {}, api_key


def get_api_key_by_token(settings: Settings, token: str) -> dict[str, Any] | None:
    key_hash = hash_api_key(token)
    with connect(settings) as connection:
        row = connection.execute("SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,)).fetchone()
    return row_to_dict(row)


def mark_api_key_used(settings: Settings, key_id: str) -> None:
    with connect(settings) as connection:
        connection.execute(
            """
            UPDATE api_keys
            SET last_used_at = ?, requests_count = requests_count + 1
            WHERE id = ?
            """,
            (iso_now(), key_id),
        )


def list_api_keys(settings: Settings) -> list[dict[str, Any]]:
    with connect(settings) as connection:
        rows = connection.execute(
            "SELECT * FROM api_keys ORDER BY created_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def disable_api_key(settings: Settings, key_id: str) -> None:
    with connect(settings) as connection:
        connection.execute("UPDATE api_keys SET enabled = 0 WHERE id = ?", (key_id,))


def delete_api_key(settings: Settings, key_id: str) -> None:
    # FK constraint (PRAGMA foreign_keys=ON) blocks deleting a key that has
    # image_requests rows. Unlink those rows first by setting api_key_id NULL
    # so history survives — list_image_requests uses LEFT JOIN, so the Key
    # column just becomes "—" for orphaned rows. Both statements run in one
    # transaction.
    with connect(settings) as connection:
        connection.execute(
            "UPDATE image_requests SET api_key_id = NULL WHERE api_key_id = ?",
            (key_id,),
        )
        connection.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))


def insert_image_request(
    settings: Settings,
    *,
    request_id: str,
    api_key_id: str,
    prompt: str,
    size: str,
    quality: str,
    count: int,
    expires_at: str,
) -> None:
    with connect(settings) as connection:
        connection.execute(
            """
            INSERT INTO image_requests (
                id, api_key_id, prompt, size, quality, count, status, created_at, expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
            (request_id, api_key_id, prompt, size, quality, count, iso_now(), expires_at),
        )


def mark_image_request_running(settings: Settings, request_id: str) -> None:
    with connect(settings) as connection:
        connection.execute(
            "UPDATE image_requests SET status = 'running', started_at = ? WHERE id = ?",
            (iso_now(), request_id),
        )


def mark_image_request_succeeded(
    settings: Settings,
    *,
    request_id: str,
    image_paths: list[Path],
    stdout: str,
    stderr: str,
    duration_seconds: float,
    workdir: Path,
    codex_command: str,
    codex_home: str | None = None,
) -> None:
    with connect(settings) as connection:
        connection.execute(
            """
            UPDATE image_requests
            SET status = 'succeeded',
                image_paths = ?,
                stdout = ?,
                stderr = ?,
                duration_seconds = ?,
                finished_at = ?,
                workdir = ?,
                codex_command = ?,
                codex_home = ?
            WHERE id = ?
            """,
            (
                json.dumps([str(path) for path in image_paths]),
                stdout,
                stderr,
                duration_seconds,
                iso_now(),
                str(workdir),
                codex_command,
                codex_home,
                request_id,
            ),
        )


def mark_image_request_failed(
    settings: Settings,
    *,
    request_id: str,
    error: str,
    stdout: str = "",
    stderr: str = "",
    duration_seconds: float | None = None,
    workdir: Path | None = None,
    codex_command: str = "",
) -> None:
    with connect(settings) as connection:
        connection.execute(
            """
            UPDATE image_requests
            SET status = 'failed',
                error = ?,
                stdout = ?,
                stderr = ?,
                duration_seconds = ?,
                finished_at = ?,
                workdir = COALESCE(?, workdir),
                codex_command = ?
            WHERE id = ?
            """,
            (
                error,
                stdout,
                stderr,
                duration_seconds,
                iso_now(),
                str(workdir) if workdir else None,
                codex_command,
                request_id,
            ),
        )


def mark_image_request_expired(settings: Settings, request_id: str) -> None:
    with connect(settings) as connection:
        connection.execute(
            "UPDATE image_requests SET status = 'expired' WHERE id = ?",
            (request_id,),
        )


def list_image_requests(settings: Settings, limit: int = 100) -> list[dict[str, Any]]:
    with connect(settings) as connection:
        rows = connection.execute(
            """
            SELECT image_requests.*, api_keys.name AS api_key_name
            FROM image_requests
            LEFT JOIN api_keys ON api_keys.id = image_requests.api_key_id
            ORDER BY image_requests.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_decode_request_row(row) for row in rows]


def get_image_request(settings: Settings, request_id: str) -> dict[str, Any] | None:
    with connect(settings) as connection:
        row = connection.execute(
            "SELECT * FROM image_requests WHERE id = ?",
            (request_id,),
        ).fetchone()
    return _decode_request_row(row) if row else None


def delete_image_request(settings: Settings, request_id: str) -> None:
    with connect(settings) as connection:
        connection.execute("DELETE FROM image_requests WHERE id = ?", (request_id,))


def list_expired_image_requests(
    settings: Settings, *, before_iso: str, limit: int = 500
) -> list[dict[str, Any]]:
    with connect(settings) as connection:
        rows = connection.execute(
            """
            SELECT * FROM image_requests
            WHERE expires_at <= ? AND status != 'expired'
            ORDER BY expires_at ASC
            LIMIT ?
            """,
            (before_iso, limit),
        ).fetchall()
    return [_decode_request_row(row) for row in rows]


def dashboard_stats(settings: Settings) -> dict[str, int]:
    with connect(settings) as connection:
        api_key_count = connection.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]
        active_key_count = connection.execute(
            "SELECT COUNT(*) FROM api_keys WHERE enabled = 1"
        ).fetchone()[0]
        request_count = connection.execute("SELECT COUNT(*) FROM image_requests").fetchone()[0]
        queued_count = connection.execute(
            "SELECT COUNT(*) FROM image_requests WHERE status IN ('queued', 'running')"
        ).fetchone()[0]
    return {
        "api_key_count": int(api_key_count),
        "active_key_count": int(active_key_count),
        "request_count": int(request_count),
        "queued_count": int(queued_count),
    }


def _decode_request_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    try:
        item["image_paths"] = json.loads(item.get("image_paths") or "[]")
    except json.JSONDecodeError:
        item["image_paths"] = []
    return item

