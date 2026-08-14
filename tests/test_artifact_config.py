from __future__ import annotations

from pathlib import Path

import pytest

from ai_memory_mcp.config import Settings


def test_artifact_paths_follow_the_state_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("AI_MEMORY_MCP_STATE_DIR", str(state))
    monkeypatch.delenv("AI_MEMORY_ARTIFACT_DB", raising=False)
    monkeypatch.delenv("AI_MEMORY_ARTIFACT_OBJECTS_DIR", raising=False)
    monkeypatch.delenv("AI_MEMORY_ARTIFACT_BACKUP_DIR", raising=False)
    settings = Settings.from_env()
    assert settings.artifact_db == Path.home() / ".ai-memory" / "artifacts.sqlite3"
    assert settings.artifact_objects_dir == Path.home() / ".ai-memory" / "objects"
    assert settings.artifact_backup_dir == Path.home() / ".ai-memory" / "backups"
    assert settings.artifact_batch_max_bytes == 268_435_456


def test_artifact_paths_accept_process_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_MEMORY_ARTIFACT_DB", str(tmp_path / "raw.sqlite3"))
    monkeypatch.setenv("AI_MEMORY_ARTIFACT_OBJECTS_DIR", str(tmp_path / "objects"))
    monkeypatch.setenv("AI_MEMORY_ARTIFACT_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("AI_MEMORY_ARTIFACT_BATCH_MAX_BYTES", "1024")
    settings = Settings.from_env()
    assert settings.artifact_db == tmp_path / "raw.sqlite3"
    assert settings.artifact_objects_dir == tmp_path / "objects"
    assert settings.artifact_backup_dir == tmp_path / "backups"
    assert settings.artifact_batch_max_bytes == 1024


def test_artifact_batch_limit_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_MEMORY_ARTIFACT_BATCH_MAX_BYTES", "0")
    with pytest.raises(ValueError, match="positive"):
        Settings.from_env()
