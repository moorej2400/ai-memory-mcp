from __future__ import annotations

from pathlib import Path

import pytest

from ai_memory_mcp.config import Settings


def test_internal_paths_follow_the_memory_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vault"
    monkeypatch.setenv("AI_MEMORY_WORK_DIR", str(root))
    for name in (
        "AI_MEMORY_MCP_STATE_DIR",
        "AI_MEMORY_GRAPHIFY_STATE_DIR",
        "AI_MEMORY_GRAPH_PATH",
        "AI_MEMORY_LOG_DIR",
        "AI_MEMORY_ARTIFACT_DB",
        "AI_MEMORY_ARTIFACT_OBJECTS_DIR",
        "AI_MEMORY_ARTIFACT_BACKUP_DIR",
    ):
        monkeypatch.setenv(name, "")
    settings = Settings.from_env()
    data_root = root / ".ai-memory"
    assert settings.data_root == data_root
    assert settings.state_dir == data_root / "indexes"
    assert settings.resolved_log_dir == data_root / "logs"
    assert settings.artifact_db == data_root / "raw" / "artifacts.sqlite3"
    assert settings.artifact_objects_dir == data_root / "raw" / "objects"
    assert settings.artifact_backup_dir == data_root / "backups"
    assert settings.migration_dir == data_root / "migration"
    assert settings.provider_state_dir == data_root / "provider-state"
    assert settings.graph_path == (
        data_root
        / "provider-state"
        / "graphify"
        / "corpora"
        / "ai-memory"
        / "graphify-out"
        / "graph.json"
    )
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
