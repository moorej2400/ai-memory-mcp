from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from ai_memory_mcp.artifacts.schema import (
    ARTIFACT_SCHEMA_VERSION,
    artifact_database_status,
    connect_artifact_db,
    migrate_artifact_db,
)
from ai_memory_mcp.config import Settings


def test_migration_creates_schema_and_fts(artifact_settings: Settings) -> None:
    result = migrate_artifact_db(artifact_settings)
    assert result.from_version == 0
    assert result.to_version == ARTIFACT_SCHEMA_VERSION == 1
    assert result.applied == [1]

    with connect_artifact_db(artifact_settings.artifact_db) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        counter = connection.execute(
            "SELECT value FROM artifact_metadata WHERE key = 'change_counter'"
        ).fetchone()[0]

    assert {
        "artifacts",
        "artifact_events",
        "artifact_batches",
        "artifact_batch_events",
        "artifact_aliases",
        "artifact_links",
        "artifact_objects",
        "artifact_object_links",
        "artifact_coverage",
        "distillation_state",
        "artifacts_fts",
    } <= names
    assert counter == "0"


def test_repeated_migration_is_a_no_op(artifact_settings: Settings) -> None:
    migrate_artifact_db(artifact_settings)
    result = migrate_artifact_db(artifact_settings)
    assert result.from_version == 1
    assert result.to_version == 1
    assert result.applied == []
    assert result.backup_path is None


def test_status_reports_database_health(artifact_settings: Settings) -> None:
    absent = artifact_database_status(artifact_settings)
    assert absent.exists is False
    assert absent.schema_version == 0

    migrate_artifact_db(artifact_settings)
    status = artifact_database_status(artifact_settings)
    assert status.exists is True
    assert status.schema_version == 1
    assert status.integrity == "ok"
    assert status.change_counter == 0


def test_read_only_connection_does_not_create_a_database(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        connect_artifact_db(path, read_only=True)
    assert not path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode test")
def test_migration_uses_private_file_modes(artifact_settings: Settings) -> None:
    migrate_artifact_db(artifact_settings)
    assert artifact_settings.artifact_db.parent.stat().st_mode & 0o777 == 0o700
    assert artifact_settings.artifact_db.stat().st_mode & 0o777 == 0o600


def test_nonempty_legacy_database_gets_a_backup(
    artifact_settings: Settings,
) -> None:
    artifact_settings.artifact_db.parent.mkdir(parents=True)
    with sqlite3.connect(artifact_settings.artifact_db) as connection:
        connection.execute("CREATE TABLE legacy_marker(value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_marker VALUES ('keep')")

    result = migrate_artifact_db(artifact_settings)
    assert result.backup_path is not None
    assert result.backup_path.is_file()
    with sqlite3.connect(result.backup_path) as backup:
        assert backup.execute("SELECT value FROM legacy_marker").fetchone()[0] == "keep"


def test_fts_triggers_exclude_deleted_and_redacted_rows(
    artifact_settings: Settings,
) -> None:
    migrate_artifact_db(artifact_settings)
    values = (
        "artifact_v1_example",
        "connector",
        "default",
        "message",
        "external-1",
        "Useful title",
        "actor-1",
        "Example Person",
        "stable",
        "plain",
        "The useful searchable text",
        "{}",
        "0" * 64,
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:00:00+00:00",
        "event_v1_example",
    )
    with connect_artifact_db(artifact_settings.artifact_db) as connection:
        connection.execute(
            """
            INSERT INTO artifacts(
                artifact_id, source, source_instance, entity, external_id,
                title, author_id, author_name, author_id_confidence,
                content_format, text_content, payload_json, payload_sha256,
                first_observed_at, last_observed_at, last_event_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        assert connection.execute(
            "SELECT count(*) FROM artifacts_fts WHERE artifacts_fts MATCH 'useful'"
        ).fetchone()[0] == 1

        connection.execute(
            "UPDATE artifacts SET deleted_at = ? WHERE artifact_id = ?",
            ("2026-01-02T00:00:00+00:00", values[0]),
        )
        assert connection.execute(
            "SELECT count(*) FROM artifacts_fts WHERE artifacts_fts MATCH 'useful'"
        ).fetchone()[0] == 0

        connection.execute(
            "UPDATE artifacts SET deleted_at = NULL, redacted_at = ? "
            "WHERE artifact_id = ?",
            ("2026-01-03T00:00:00+00:00", values[0]),
        )
        assert connection.execute(
            "SELECT count(*) FROM artifacts_fts WHERE artifacts_fts MATCH 'useful'"
        ).fetchone()[0] == 0
