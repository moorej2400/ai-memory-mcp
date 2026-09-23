from __future__ import annotations

import hashlib
import multiprocessing
import os
from pathlib import Path
from typing import Any

import pytest

from ai_memory_mcp.capture import _replace_file, safe_memory_path, upsert_memory


def _hold_capture_lock(
    root: str,
    ready: Any,
    release: Any,
) -> None:
    from ai_memory_mcp.audit import file_lock

    with file_lock(Path(root) / ".ai-memory" / "capture.lock", 5.0):
        ready.set()
        release.wait(5.0)


def _record(memory_id: str, title: str, summary: str = "A durable summary.") -> str:
    return f"""---
schema_version: 2
memory_id: {memory_id}
title: {title}
type: memory
record_type: note
domain: general
status: active
created: 2026-09-18
updated: 2026-09-18
provenance:
  - manual:test
---
# {title}

{summary}
"""


def test_upsert_creates_and_repeats_without_change(tmp_path: Path) -> None:
    raw = _record("mem_example", "Example")
    created = upsert_memory(
        tmp_path,
        relative_path="Notes/Example.md",
        markdown=raw,
    )
    repeated = upsert_memory(
        tmp_path,
        relative_path="Notes/Example.md",
        markdown=raw,
    )

    assert created["created"] is True
    assert created["changed"] is True
    assert repeated["created"] is False
    assert repeated["changed"] is False


def test_update_requires_matching_digest(tmp_path: Path) -> None:
    original = _record("mem_example", "Example")
    upsert_memory(tmp_path, relative_path="Notes/Example.md", markdown=original)
    changed = _record("mem_example", "Example", "A revised durable summary.")

    with pytest.raises(ValueError, match="expected_sha256 is required"):
        upsert_memory(tmp_path, relative_path="Notes/Example.md", markdown=changed)
    with pytest.raises(ValueError, match="changed before this update"):
        upsert_memory(
            tmp_path,
            relative_path="Notes/Example.md",
            markdown=changed,
            expected_sha256="0" * 64,
        )

    result = upsert_memory(
        tmp_path,
        relative_path="Notes/Example.md",
        markdown=changed,
        expected_sha256=hashlib.sha256(original.encode()).hexdigest(),
    )
    assert result["changed"] is True


def test_upsert_rejects_duplicate_identity_and_unsafe_path(tmp_path: Path) -> None:
    raw = _record("mem_example", "Example")
    upsert_memory(tmp_path, relative_path="Notes/Example.md", markdown=raw)

    with pytest.raises(ValueError, match="already exists"):
        upsert_memory(tmp_path, relative_path="Other/Example.md", markdown=raw)
    with pytest.raises(ValueError, match="unsafe segment"):
        safe_memory_path(tmp_path, "../Example.md")


def test_upsert_requires_collection_metadata_for_a_collection_path(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="collection: People"):
        upsert_memory(
            tmp_path,
            relative_path="Collections/People/Records/Example.md",
            markdown=_record("mem_example", "Example"),
        )


def test_upsert_waits_for_the_cross_process_capture_lock(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_capture_lock,
        args=(str(tmp_path), ready, release),
    )
    process.start()
    try:
        assert ready.wait(5.0)
        with pytest.raises(TimeoutError, match="capture.lock"):
            upsert_memory(
                tmp_path,
                relative_path="Notes/Example.md",
                markdown=_record("mem_example", "Example"),
                lock_timeout_seconds=0.05,
            )
    finally:
        release.set()
        process.join(5.0)
        if process.is_alive():
            process.terminate()
            process.join(5.0)
    assert process.exitcode == 0


def test_atomic_replace_retries_a_transient_file_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.pending"
    target = tmp_path / "target.md"
    source.write_text("new", encoding="utf-8")
    target.write_text("old", encoding="utf-8")
    original_replace = os.replace
    attempts = 0

    def replace_once_locked(source_path: Path, target_path: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("synthetic sync-client lock")
        original_replace(source_path, target_path)

    monkeypatch.setattr("ai_memory_mcp.capture.os.replace", replace_once_locked)
    monkeypatch.setattr("ai_memory_mcp.capture.time.sleep", lambda _: None)

    _replace_file(source, target)

    assert attempts == 2
    assert target.read_text(encoding="utf-8") == "new"
