"""Stale-image recovery guard.

Regression test for the duplicate-image bug: CODEX_HOMEs are shared and
persistent, so when a request produces no fresh file the session-scoped
recovery used to surface a *previous* cat's image and return it as success.
The ``min_mtime`` guard must reject anything not created by this request.
"""
import os
import time

from app.services.codex_image import (
    _find_generated_in_session,
    _find_image_in_rollout,
)

_SID = "019e5b1c-b379-77d1-84ad-be7100adb9e2"
_STDERR = f"some line\nsession id: {_SID}\nmore\n"

# 1x1 PNG + the PNG base64 magic so the rollout pre-filter matches.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYGAAAAAEAAH2FzhVAAAAAElFTkSuQmCC"
)


def _make_session_image(tmp_path, name, age_seconds):
    d = tmp_path / "generated_images" / _SID
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 32)
    t = time.time() - age_seconds
    os.utime(p, (t, t))
    return p


def test_session_rejects_stale_accepts_fresh(tmp_path):
    _make_session_image(tmp_path, "old.png", age_seconds=3600)   # 1h old (prior cat)
    fresh = _make_session_image(tmp_path, "new.png", age_seconds=0)
    cutoff = time.time() - 5  # this request started ~now

    got = _find_generated_in_session(_STDERR, codex_home=str(tmp_path), min_mtime=cutoff)
    assert got == fresh, "should return the freshly-created image"


def test_session_returns_none_when_only_stale(tmp_path):
    _make_session_image(tmp_path, "old.png", age_seconds=3600)
    cutoff = time.time() - 5
    got = _find_generated_in_session(_STDERR, codex_home=str(tmp_path), min_mtime=cutoff)
    assert got is None, "a stale-only session must yield no image (caller falls back)"


def test_session_unfiltered_still_finds(tmp_path):
    """min_mtime=0 keeps the original behaviour (newest wins)."""
    _make_session_image(tmp_path, "old.png", age_seconds=3600)
    newest = _make_session_image(tmp_path, "new.png", age_seconds=10)
    got = _find_generated_in_session(_STDERR, codex_home=str(tmp_path))
    assert got == newest


def test_rollout_rejects_stale(tmp_path):
    sessions = tmp_path / "sessions" / "2026" / "06"
    sessions.mkdir(parents=True, exist_ok=True)
    roll = sessions / f"rollout-{_SID}.jsonl"
    line = '{"payload": {"type": "image_generation_end", "result": "%s"}}\n' % _PNG_B64
    roll.write_text(line, encoding="utf-8")
    old = time.time() - 3600
    os.utime(roll, (old, old))

    cutoff = time.time() - 5
    assert _find_image_in_rollout(_STDERR, codex_home=str(tmp_path), min_mtime=cutoff) is None
    # unfiltered still recovers the bytes
    assert _find_image_in_rollout(_STDERR, codex_home=str(tmp_path)) is not None
