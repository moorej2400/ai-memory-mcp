from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from ai_memory_mcp import __version__
from ai_memory_mcp.config import Settings

ARTIFACT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class MigrationResult:
    from_version: int
    to_version: int
    applied: list[int]
    backup_path: Path | None = None


@dataclass(frozen=True, slots=True)
class ArtifactDatabaseStatus:
    path: Path
    exists: bool
    schema_version: int
    integrity: str
    change_counter: int
    byte_count: int


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)


def _private_file(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)


def connect_artifact_db(
    path: Path,
    *,
    read_only: bool = False,
) -> sqlite3.Connection:
    """Open the artifact database with its durability and isolation rules."""
    path = path.expanduser().resolve()
    if read_only:
        uri = f"file:{quote(path.as_posix(), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10.0)
    else:
        _private_directory(path.parent)
        connection = sqlite3.connect(path, timeout=10.0)

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    if not read_only:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        _private_file(path)
    return connection


def _migration_version(connection: sqlite3.Connection) -> int:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'artifact_schema_migrations'"
    ).fetchone()
    if exists is None:
        return 0
    row = connection.execute(
        "SELECT COALESCE(MAX(version), 0) FROM artifact_schema_migrations"
    ).fetchone()
    return int(row[0])


MIGRATION_1_STATEMENTS = (
    """
    CREATE TABLE artifact_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE artifact_schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL,
        application_version TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE artifact_batches (
        batch_id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        source_instance TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        input_sha256 TEXT NOT NULL,
        expected_events INTEGER NOT NULL CHECK(expected_events >= 0),
        accepted_events INTEGER NOT NULL DEFAULT 0 CHECK(accepted_events >= 0),
        unchanged_events INTEGER NOT NULL DEFAULT 0 CHECK(unchanged_events >= 0),
        stale_events INTEGER NOT NULL DEFAULT 0 CHECK(stale_events >= 0),
        conflict_events INTEGER NOT NULL DEFAULT 0 CHECK(conflict_events >= 0),
        tombstones INTEGER NOT NULL DEFAULT 0 CHECK(tombstones >= 0),
        redactions INTEGER NOT NULL DEFAULT 0 CHECK(redactions >= 0),
        status TEXT NOT NULL CHECK(status IN ('processing', 'ok', 'error')),
        error TEXT,
        started_at TEXT NOT NULL,
        completed_at TEXT
    )
    """,
    """
    CREATE TABLE artifacts (
        rowid INTEGER PRIMARY KEY,
        artifact_id TEXT NOT NULL UNIQUE,
        source TEXT NOT NULL,
        source_instance TEXT NOT NULL,
        entity TEXT NOT NULL,
        external_id TEXT NOT NULL,
        parent_artifact_id TEXT REFERENCES artifacts(artifact_id),
        title TEXT NOT NULL DEFAULT '',
        author_id TEXT NOT NULL DEFAULT '',
        author_name TEXT NOT NULL DEFAULT '',
        author_id_confidence TEXT NOT NULL DEFAULT '',
        occurred_at TEXT,
        source_updated_at TEXT,
        source_version TEXT,
        source_sequence INTEGER CHECK(
            source_sequence IS NULL OR source_sequence >= 0
        ),
        text_content TEXT NOT NULL DEFAULT '',
        content_format TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        first_observed_at TEXT NOT NULL,
        last_observed_at TEXT NOT NULL,
        last_event_id TEXT NOT NULL,
        deleted_at TEXT,
        redacted_at TEXT,
        UNIQUE(source, source_instance, entity, external_id)
    )
    """,
    """
    CREATE TABLE artifact_events (
        event_id TEXT PRIMARY KEY,
        first_batch_id TEXT NOT NULL REFERENCES artifact_batches(batch_id),
        artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
        source TEXT NOT NULL,
        source_instance TEXT NOT NULL,
        entity TEXT NOT NULL,
        external_id TEXT NOT NULL,
        operation TEXT NOT NULL CHECK(
            operation IN ('upsert', 'delete', 'redact')
        ),
        source_version TEXT,
        source_sequence INTEGER CHECK(
            source_sequence IS NULL OR source_sequence >= 0
        ),
        source_updated_at TEXT,
        observed_at TEXT NOT NULL,
        payload_json TEXT,
        payload_sha256 TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE artifact_batch_events (
        batch_id TEXT NOT NULL REFERENCES artifact_batches(batch_id),
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        event_id TEXT NOT NULL REFERENCES artifact_events(event_id),
        disposition TEXT NOT NULL CHECK(
            disposition IN (
                'accepted', 'unchanged', 'stale', 'conflict',
                'tombstone', 'redacted'
            )
        ),
        PRIMARY KEY(batch_id, ordinal)
    )
    """,
    """
    CREATE TABLE artifact_aliases (
        artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
        source TEXT NOT NULL,
        source_instance TEXT NOT NULL,
        alias_kind TEXT NOT NULL,
        alias_value TEXT NOT NULL,
        PRIMARY KEY(
            artifact_id, source, source_instance, alias_kind, alias_value
        ),
        UNIQUE(source, source_instance, alias_kind, alias_value)
    )
    """,
    """
    CREATE TABLE artifact_links (
        source_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
        relation TEXT NOT NULL,
        target_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
        created_at TEXT NOT NULL,
        PRIMARY KEY(source_artifact_id, relation, target_artifact_id)
    )
    """,
    """
    CREATE TABLE artifact_objects (
        sha256 TEXT PRIMARY KEY,
        byte_count INTEGER NOT NULL CHECK(byte_count >= 0),
        media_type TEXT NOT NULL DEFAULT '',
        relative_path TEXT NOT NULL UNIQUE,
        first_observed_at TEXT NOT NULL,
        last_verified_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE artifact_object_links (
        artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
        sha256 TEXT NOT NULL REFERENCES artifact_objects(sha256),
        relation TEXT NOT NULL,
        original_name TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(artifact_id, sha256, relation)
    )
    """,
    """
    CREATE TABLE artifact_coverage (
        batch_id TEXT NOT NULL REFERENCES artifact_batches(batch_id),
        parent_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
        entity TEXT NOT NULL,
        covered_from TEXT NOT NULL DEFAULT '',
        covered_to TEXT NOT NULL DEFAULT '',
        complete INTEGER NOT NULL CHECK(complete IN (0, 1)),
        PRIMARY KEY(
            batch_id, parent_artifact_id, entity, covered_from, covered_to
        )
    )
    """,
    """
    CREATE TABLE distillation_state (
        artifact_id TEXT PRIMARY KEY REFERENCES artifacts(artifact_id),
        status TEXT NOT NULL CHECK(
            status IN (
                'pending', 'distilled', 'no-durable-memory', 'needs-review'
            )
        ),
        latest_event_id TEXT NOT NULL,
        latest_source_digest TEXT NOT NULL,
        distilled_through_event_id TEXT,
        distilled_source_digest TEXT,
        memory_id TEXT,
        memory_source_id TEXT,
        memory_path TEXT,
        outcome_reason TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX artifacts_scope_idx
        ON artifacts(
            source, source_instance, entity, deleted_at, occurred_at
        )
    """,
    """
    CREATE INDEX artifacts_parent_idx
        ON artifacts(parent_artifact_id, entity, occurred_at, artifact_id)
    """,
    """
    CREATE INDEX artifact_events_artifact_idx
        ON artifact_events(artifact_id, observed_at, event_id)
    """,
    """
    CREATE INDEX artifact_batch_events_event_idx
        ON artifact_batch_events(event_id, batch_id)
    """,
    """
    CREATE INDEX distillation_pending_idx
        ON distillation_state(status, updated_at, artifact_id)
    """,
    """
    CREATE VIRTUAL TABLE artifacts_fts USING fts5(
        title,
        author_name,
        text_content,
        external_id,
        content='artifacts',
        content_rowid='rowid',
        tokenize='unicode61 remove_diacritics 2 tokenchars ''_-:/#'''
    )
    """,
    """
    CREATE TRIGGER artifacts_fts_insert
    AFTER INSERT ON artifacts
    WHEN new.deleted_at IS NULL AND new.redacted_at IS NULL
    BEGIN
        INSERT INTO artifacts_fts(
            rowid, title, author_name, text_content, external_id
        ) VALUES (
            new.rowid, new.title, new.author_name,
            new.text_content, new.external_id
        );
    END
    """,
    """
    CREATE TRIGGER artifacts_fts_delete
    AFTER DELETE ON artifacts
    WHEN old.deleted_at IS NULL AND old.redacted_at IS NULL
    BEGIN
        INSERT INTO artifacts_fts(
            artifacts_fts, rowid, title, author_name,
            text_content, external_id
        ) VALUES (
            'delete', old.rowid, old.title, old.author_name,
            old.text_content, old.external_id
        );
    END
    """,
    """
    CREATE TRIGGER artifacts_fts_update_delete
    AFTER UPDATE ON artifacts
    WHEN old.deleted_at IS NULL AND old.redacted_at IS NULL
    BEGIN
        INSERT INTO artifacts_fts(
            artifacts_fts, rowid, title, author_name,
            text_content, external_id
        ) VALUES (
            'delete', old.rowid, old.title, old.author_name,
            old.text_content, old.external_id
        );
    END
    """,
    """
    CREATE TRIGGER artifacts_fts_update_insert
    AFTER UPDATE ON artifacts
    WHEN new.deleted_at IS NULL AND new.redacted_at IS NULL
    BEGIN
        INSERT INTO artifacts_fts(
            rowid, title, author_name, text_content, external_id
        ) VALUES (
            new.rowid, new.title, new.author_name,
            new.text_content, new.external_id
        );
    END
    """,
    """
    INSERT INTO artifact_metadata(key, value)
    VALUES ('change_counter', '0')
    """,
)


def _backup_database(
    connection: sqlite3.Connection,
    settings: Settings,
    version: int,
) -> Path:
    _private_directory(settings.artifact_backup_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = settings.artifact_backup_dir / (
        f"{settings.artifact_db.name}.v{version}.{stamp}.sqlite3"
    )
    with sqlite3.connect(backup_path) as backup:
        connection.backup(backup)
    _private_file(backup_path)
    return backup_path


def _apply_migration_1(connection: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc).isoformat()
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in MIGRATION_1_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            """
            INSERT INTO artifact_schema_migrations(
                version, applied_at, application_version
            ) VALUES (?, ?, ?)
            """,
            (1, now, __version__),
        )
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def migrate_artifact_db(settings: Settings) -> MigrationResult:
    """Move the canonical artifact database to the current schema."""
    path = settings.artifact_db.expanduser()
    existed_nonempty = path.is_file() and path.stat().st_size > 0
    backup_path: Path | None = None
    with connect_artifact_db(path) as connection:
        from_version = _migration_version(connection)
        if from_version > ARTIFACT_SCHEMA_VERSION:
            raise RuntimeError(
                "The artifact database schema is newer than this application."
            )
        if from_version == ARTIFACT_SCHEMA_VERSION:
            return MigrationResult(
                from_version=from_version,
                to_version=from_version,
                applied=[],
            )
        if existed_nonempty:
            backup_path = _backup_database(connection, settings, from_version)

        applied: list[int] = []
        if from_version < 1:
            _apply_migration_1(connection)
            applied.append(1)

    _private_file(path)
    return MigrationResult(
        from_version=from_version,
        to_version=ARTIFACT_SCHEMA_VERSION,
        applied=applied,
        backup_path=backup_path,
    )


def artifact_database_status(settings: Settings) -> ArtifactDatabaseStatus:
    """Return a read-only health summary for the canonical artifact database."""
    path = settings.artifact_db.expanduser()
    if not path.is_file():
        return ArtifactDatabaseStatus(
            path=path,
            exists=False,
            schema_version=0,
            integrity="missing",
            change_counter=0,
            byte_count=0,
        )

    with connect_artifact_db(path, read_only=True) as connection:
        version = _migration_version(connection)
        integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        counter = 0
        metadata_exists = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'artifact_metadata'"
        ).fetchone()
        if metadata_exists:
            row = connection.execute(
                "SELECT value FROM artifact_metadata "
                "WHERE key = 'change_counter'"
            ).fetchone()
            if row is not None:
                counter = int(row[0])
    return ArtifactDatabaseStatus(
        path=path,
        exists=True,
        schema_version=version,
        integrity=integrity,
        change_counter=counter,
        byte_count=path.stat().st_size,
    )
