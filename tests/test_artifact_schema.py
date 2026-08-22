from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

import ai_memory_mcp.artifacts.schema as schema_module
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
    assert result.to_version == ARTIFACT_SCHEMA_VERSION == 4
    assert result.applied == [1, 2, 3, 4]

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
    assert result.from_version == 4
    assert result.to_version == 4
    assert result.applied == []
    assert result.backup_path is None


def test_status_reports_database_health(artifact_settings: Settings) -> None:
    absent = artifact_database_status(artifact_settings)
    assert absent.exists is False
    assert absent.schema_version == 0

    migrate_artifact_db(artifact_settings)
    status = artifact_database_status(artifact_settings)
    assert status.exists is True
    assert status.schema_version == 4
    assert status.integrity == "ok"
    assert status.change_counter == 0


def test_read_only_connection_does_not_create_a_database(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        connect_artifact_db(path, read_only=True)
    assert not path.exists()


def test_connection_rejects_a_known_network_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ai_memory_mcp.artifacts.schema._network_filesystem_type",
        lambda _path: "smbfs",
    )
    with pytest.raises(ValueError, match="network filesystem"):
        connect_artifact_db(tmp_path / "artifacts.sqlite3")


def test_network_filesystem_detection_refreshes_after_the_cache_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mount = tmp_path.resolve()
    snapshots = iter(
        (
            ((mount, "apfs"),),
            ((mount, "smbfs"),),
        )
    )
    clock = iter((1000.0, 1002.0))
    monkeypatch.setattr(schema_module, "_read_mounted_filesystems", lambda: next(snapshots))
    monkeypatch.setattr(schema_module.time, "monotonic", lambda: next(clock))

    database = mount / "artifacts.sqlite3"
    assert schema_module._network_filesystem_type(database) is None
    assert schema_module._network_filesystem_type(database) == "smbfs"


def test_connection_rejects_a_windows_mapped_network_drive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schema_module, "_is_windows", lambda: True)
    monkeypatch.setattr(schema_module, "_windows_drive_type", lambda _root: 4)

    assert schema_module._network_filesystem_type(Path(r"Z:\artifact\store.sqlite3")) == "remote"


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


def test_schema_migration_backup_does_not_overwrite_a_name_collision(
    artifact_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_settings.artifact_db.parent.mkdir(parents=True)
    with sqlite3.connect(artifact_settings.artifact_db) as connection:
        connection.execute("CREATE TABLE legacy_marker(value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_marker VALUES ('keep')")
    fixed = schema_module.datetime(2026, 1, 2, tzinfo=schema_module.timezone.utc)

    class FixedDateTime:
        @staticmethod
        def now(_timezone: object) -> schema_module.datetime:
            return fixed

    monkeypatch.setattr(schema_module, "datetime", FixedDateTime)
    with sqlite3.connect(artifact_settings.artifact_db) as connection:
        first = schema_module._backup_database(connection, artifact_settings, 0)
    before = first.read_bytes()

    with sqlite3.connect(artifact_settings.artifact_db) as connection:
        with pytest.raises(FileExistsError):
            schema_module._backup_database(connection, artifact_settings, 0)

    assert first.read_bytes() == before


def test_schema_migration_backup_failure_leaves_no_final_recovery_point(
    artifact_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed = schema_module.datetime(2026, 1, 2, tzinfo=schema_module.timezone.utc)

    class FixedDateTime:
        @staticmethod
        def now(_timezone: object) -> schema_module.datetime:
            return fixed

    class FailingConnection:
        @staticmethod
        def backup(_target: sqlite3.Connection) -> None:
            raise RuntimeError("synthetic backup failure")

    monkeypatch.setattr(schema_module, "datetime", FixedDateTime)
    with pytest.raises(RuntimeError, match="synthetic backup failure"):
        schema_module._backup_database(  # type: ignore[arg-type]
            FailingConnection(),
            artifact_settings,
            0,
        )

    assert list(artifact_settings.artifact_backup_dir.glob("*.sqlite3")) == []
    assert list(artifact_settings.artifact_backup_dir.glob(".*.partial-*")) == []


def test_schema_migration_rejects_a_network_backup_path(
    artifact_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_settings.artifact_db.parent.mkdir(parents=True)
    with sqlite3.connect(artifact_settings.artifact_db) as connection:
        connection.execute("CREATE TABLE legacy_marker(value TEXT NOT NULL)")
    backup_root = artifact_settings.artifact_backup_dir.resolve()
    monkeypatch.setattr(
        schema_module,
        "_network_filesystem_type",
        lambda path: "smbfs" if path.resolve().is_relative_to(backup_root) else None,
    )

    with pytest.raises(ValueError, match="network filesystem"):
        migrate_artifact_db(artifact_settings)


def test_migration_3_preserves_aliases_and_allows_shared_alias_values(
    artifact_settings: Settings,
) -> None:
    with connect_artifact_db(artifact_settings.artifact_db) as connection:
        schema_module._apply_migration_1(connection)
        schema_module._apply_migration_2(connection)
        rows = [
            (
                f"artifact-v2-{index}",
                f"meeting-{index}",
                f"event-v2-{index}",
            )
            for index in (1, 2)
        ]
        connection.executemany(
            """
            INSERT INTO artifacts(
                artifact_id, source, source_instance, entity, external_id,
                payload_json, payload_sha256, first_observed_at,
                last_observed_at, last_event_id
            ) VALUES (?, 'chat-source', 'workspace', 'meeting', ?, '{}', ?,
                '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00', ?)
            """,
            [
                (artifact, external, str(index) * 64, event)
                for index, (artifact, external, event) in enumerate(rows, start=1)
            ],
        )
        connection.execute(
            """
            INSERT INTO artifact_aliases(
                artifact_id, source, source_instance, alias_kind, alias_value
            ) VALUES (?, 'chat-source', 'workspace', 'conversation-id', 'series-chat')
            """,
            (rows[0][0],),
        )

    result = migrate_artifact_db(artifact_settings)

    assert result.from_version == 2
    assert result.applied == [3, 4]
    with connect_artifact_db(artifact_settings.artifact_db) as connection:
        connection.execute(
            """
            INSERT INTO artifact_aliases(
                artifact_id, source, source_instance, alias_kind, alias_value
            ) VALUES (?, 'chat-source', 'workspace', 'conversation-id', 'series-chat')
            """,
            (rows[1][0],),
        )
        aliases = connection.execute(
            "SELECT artifact_id FROM artifact_aliases WHERE alias_value = ? "
            "ORDER BY artifact_id",
            ("series-chat",),
        ).fetchall()
    assert [row[0] for row in aliases] == [rows[0][0], rows[1][0]]


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
