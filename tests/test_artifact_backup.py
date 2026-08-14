from __future__ import annotations

import hashlib
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import ai_memory_mcp.artifacts.schema as schema_module
from ai_memory_mcp.artifacts.backup import (
    backup_artifact_db,
    check_artifact_db,
    restore_artifact_db,
)
from ai_memory_mcp.artifacts.models import (
    ArtifactBatchManifest,
    ArtifactEvent,
    ArtifactPayload,
    ParsedArtifactBatch,
)
from ai_memory_mcp.artifacts.store import ArtifactStore
from ai_memory_mcp.config import Settings


def _batch(batch_id: str, external_id: str) -> ParsedArtifactBatch:
    event = ArtifactEvent.model_validate(
        {
            "schema": "ai-memory/artifact-event@1",
            "record": "event",
            "entity": "message",
            "operation": "upsert",
            "external_id": external_id,
            "source_updated_at": "2026-01-02T12:00:00Z",
            "payload": ArtifactPayload(
                text=f"Neutral content for {external_id}.",
                content_format="plain",
            ),
        }
    )
    return ParsedArtifactBatch(
        manifest=ArtifactBatchManifest.model_validate(
            {
                "schema": "ai-memory/artifact-batch@1",
                "record": "batch",
                "batch_id": batch_id,
                "source": "chat-source",
                "source_instance": "workspace",
                "observed_at": "2026-01-02T12:00:00Z",
                "event_count": 1,
            }
        ),
        events=[event],
        input_sha256=(batch_id * 64)[:64],
    )


def test_backup_is_consistent_while_another_batch_commits(
    artifact_settings: Settings,
) -> None:
    store = ArtifactStore(artifact_settings)
    store.apply_batch(_batch("backup-batch-1", "message-1"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        backup_future = pool.submit(backup_artifact_db, artifact_settings)
        write_future = pool.submit(
            ArtifactStore(artifact_settings).apply_batch,
            _batch("backup-batch-2", "message-2"),
        )
        backup = backup_future.result(timeout=10)
        write_future.result(timeout=10)

    assert backup.path.is_file()
    assert backup.byte_count == backup.path.stat().st_size
    assert backup.sha256 == hashlib.sha256(backup.path.read_bytes()).hexdigest()
    with sqlite3.connect(backup.path) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] in {
            1,
            2,
        }


def test_check_reports_counts_and_integrity(artifact_settings: Settings) -> None:
    ArtifactStore(artifact_settings).apply_batch(_batch("backup-batch-1", "message-1"))
    result = check_artifact_db(artifact_settings)
    assert result.ok is True
    assert result.quick_check == "ok"
    assert result.foreign_key_violations == 0
    assert result.artifacts == 1
    assert result.batches == 1


def test_restore_uses_a_new_destination_and_preserves_the_backup(
    artifact_settings: Settings,
    tmp_path: Path,
) -> None:
    ArtifactStore(artifact_settings).apply_batch(_batch("backup-batch-1", "message-1"))
    backup = backup_artifact_db(artifact_settings)
    source_digest = hashlib.sha256(backup.path.read_bytes()).hexdigest()
    destination = tmp_path / "restore" / "artifacts.sqlite3"

    restored = restore_artifact_db(backup.path, destination)

    assert restored.destination == destination
    assert destination.is_file()
    assert hashlib.sha256(backup.path.read_bytes()).hexdigest() == source_digest
    with sqlite3.connect(destination) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1


def test_restore_rejects_an_existing_destination(
    artifact_settings: Settings,
    tmp_path: Path,
) -> None:
    ArtifactStore(artifact_settings).apply_batch(_batch("backup-batch-1", "message-1"))
    backup = backup_artifact_db(artifact_settings)
    destination = tmp_path / "existing.sqlite3"
    destination.write_bytes(b"keep")
    with pytest.raises(ValueError, match="exists"):
        restore_artifact_db(backup.path, destination)
    assert destination.read_bytes() == b"keep"


def test_restore_does_not_overwrite_a_destination_created_during_publication(
    artifact_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ArtifactStore(artifact_settings).apply_batch(_batch("backup-batch-1", "message-1"))
    backup = backup_artifact_db(artifact_settings)
    destination = tmp_path / "restore-race" / "artifacts.sqlite3"
    real_link = os.link

    def racing_link(source: Path, target: Path) -> None:
        target.write_bytes(b"racing writer")
        real_link(source, target)

    monkeypatch.setattr(os, "link", racing_link)

    with pytest.raises(RuntimeError, match="appeared|exists"):
        restore_artifact_db(backup.path, destination)

    assert destination.read_bytes() == b"racing writer"
    assert not list(destination.parent.glob("*.partial-*"))


def test_backup_rejects_a_network_filesystem_destination(
    artifact_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ArtifactStore(artifact_settings).apply_batch(_batch("backup-network", "message-1"))
    backup_root = artifact_settings.artifact_backup_dir.resolve()
    monkeypatch.setattr(
        schema_module,
        "_network_filesystem_type",
        lambda path: "smbfs" if path.resolve().is_relative_to(backup_root) else None,
    )

    with pytest.raises(ValueError, match="network filesystem"):
        backup_artifact_db(artifact_settings)


@pytest.mark.parametrize("network_side", ["source", "destination"])
def test_restore_rejects_network_filesystem_database_paths(
    artifact_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    network_side: str,
) -> None:
    ArtifactStore(artifact_settings).apply_batch(_batch("restore-network", "message-1"))
    backup = backup_artifact_db(artifact_settings)
    destination = tmp_path / "network-restore" / "artifacts.sqlite3"
    selected = backup.path.resolve() if network_side == "source" else destination.resolve()
    monkeypatch.setattr(
        schema_module,
        "_network_filesystem_type",
        lambda path: "nfs" if path.resolve() == selected else None,
    )

    with pytest.raises(ValueError, match="network filesystem"):
        restore_artifact_db(backup.path, destination)
