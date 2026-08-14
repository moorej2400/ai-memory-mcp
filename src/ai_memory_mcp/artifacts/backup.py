from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from ai_memory_mcp.config import Settings

from .models import (
    ArtifactBackupResult,
    ArtifactIntegrityResult,
    ArtifactRestoreResult,
)
from .schema import connect_artifact_db, require_local_database_path

HASH_CHUNK_BYTES = 1024 * 1024


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)


def _private_file(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)


def _discard_generated_partial(path: Path) -> None:
    # SQLite can leave generated WAL sidecars after a backup connection closes.
    for candidate in (
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
        Path(f"{path}-journal"),
        path,
    ):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_no_overwrite(temporary: Path, destination: Path) -> None:
    # A same-directory hard link gives POSIX and Windows an exclusive final name.
    try:
        os.link(temporary, destination)
    except FileExistsError as exc:
        _discard_generated_partial(temporary)
        raise RuntimeError(
            "The artifact destination appeared during publication."
        ) from exc
    except BaseException:
        _discard_generated_partial(temporary)
        raise
    _discard_generated_partial(temporary)
    _sync_directory(destination.parent)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _read_only(path: Path) -> sqlite3.Connection:
    path = require_local_database_path(path)
    uri = f"file:{quote(path.as_posix(), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def _inspect(path: Path) -> ArtifactIntegrityResult:
    with _read_only(path) as connection:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        foreign_key_violations = len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        )
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM artifacts) AS artifacts,
                (SELECT count(*) FROM artifacts
                 WHERE deleted_at IS NULL AND redacted_at IS NULL)
                    AS active_artifacts,
                (SELECT count(*) FROM artifact_batches) AS batches,
                (SELECT count(*) FROM artifact_events) AS events,
                (SELECT count(*) FROM artifact_objects) AS objects
            """
        ).fetchone()
    return ArtifactIntegrityResult(
        path=path,
        ok=quick_check == "ok" and foreign_key_violations == 0,
        quick_check=quick_check,
        foreign_key_violations=foreign_key_violations,
        artifacts=int(counts["artifacts"]),
        active_artifacts=int(counts["active_artifacts"]),
        batches=int(counts["batches"]),
        events=int(counts["events"]),
        objects=int(counts["objects"]),
    )


def check_artifact_db(settings: Settings) -> ArtifactIntegrityResult:
    """Check canonical SQLite integrity without changing the database."""
    if not settings.artifact_db.is_file():
        raise FileNotFoundError("Artifact database is not available.")
    return _inspect(settings.artifact_db)


def backup_artifact_db(settings: Settings) -> ArtifactBackupResult:
    """Create and verify one transactionally consistent SQLite backup."""
    if not settings.artifact_db.is_file():
        raise FileNotFoundError("Artifact database is not available.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = require_local_database_path(
        settings.artifact_backup_dir / f"artifacts-{stamp}.sqlite3"
    )
    backup_dir = destination.parent
    _private_directory(backup_dir)
    temporary = backup_dir / (
        f".{destination.name}.partial-{os.getpid()}-{time.time_ns()}"
    )
    require_local_database_path(temporary)
    try:
        with (
            connect_artifact_db(
                settings.artifact_db,
                read_only=True,
            ) as source,
            sqlite3.connect(temporary) as target,
        ):
            source.backup(target)
        _private_file(temporary)
        integrity = _inspect(temporary)
        if not integrity.ok:
            raise RuntimeError(
                "Artifact backup failed integrity or foreign-key validation."
            )
        _publish_no_overwrite(temporary, destination)
    except BaseException:
        _discard_generated_partial(temporary)
        raise
    _private_file(destination)
    return ArtifactBackupResult(
        path=destination,
        byte_count=destination.stat().st_size,
        sha256=_sha256_file(destination),
        artifacts=integrity.artifacts,
        active_artifacts=integrity.active_artifacts,
        batches=integrity.batches,
        events=integrity.events,
        objects=integrity.objects,
    )


def restore_artifact_db(
    source_backup: Path,
    destination: Path,
) -> ArtifactRestoreResult:
    """Restore a verified backup to a new operator-selected database path."""
    source = source_backup.expanduser().resolve(strict=True)
    source = require_local_database_path(source)
    target = require_local_database_path(destination)
    if not source.is_file():
        raise ValueError("The artifact backup must be a regular file.")
    if target.exists():
        raise ValueError("The artifact restore destination already exists.")
    integrity = _inspect(source)
    if not integrity.ok:
        raise ValueError("The artifact backup failed integrity validation.")
    source_digest = _sha256_file(source)
    _private_directory(target.parent)
    temporary = target.parent / (
        f".{target.name}.partial-{os.getpid()}-{time.time_ns()}"
    )
    require_local_database_path(temporary)
    try:
        with _read_only(source) as backup, sqlite3.connect(temporary) as restored:
            backup.backup(restored)
        _private_file(temporary)
        restored_integrity = _inspect(temporary)
        if not restored_integrity.ok:
            raise RuntimeError("The restored database failed integrity validation.")
        if _sha256_file(source) != source_digest:
            raise RuntimeError("The source backup changed during restore.")
        _publish_no_overwrite(temporary, target)
    except BaseException:
        _discard_generated_partial(temporary)
        raise
    _private_file(target)
    return ArtifactRestoreResult(
        source=source,
        destination=target,
        source_sha256=source_digest,
        destination_sha256=_sha256_file(target),
        byte_count=target.stat().st_size,
        artifacts=restored_integrity.artifacts,
        batches=restored_integrity.batches,
    )
