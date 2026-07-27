from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import Settings
from .models import MemoryChunk, MemoryDocument, ScopeFilter
from .text import chunk_document, parse_document

SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS documents (
            memory_id TEXT PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT NOT NULL,
            root_scope TEXT NOT NULL,
            scope_kind TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            updated TEXT NOT NULL,
            review_after TEXT NOT NULL,
            related_json TEXT NOT NULL,
            identifiers_json TEXT NOT NULL,
            projects_json TEXT NOT NULL,
            repos_json TEXT NOT NULL,
            tools_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            mtime_ns INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chunks (
            chunk_id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL REFERENCES documents(memory_id) ON DELETE CASCADE,
            path TEXT NOT NULL,
            title TEXT NOT NULL,
            heading TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            text TEXT NOT NULL,
            vector_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_memory ON chunks(memory_id, ordinal);
        CREATE INDEX IF NOT EXISTS idx_documents_scope
            ON documents(root_scope, status, scope_kind, scope_id);
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            chunk_id UNINDEXED,
            title,
            heading,
            text,
            identifiers,
            tokenize='unicode61 remove_diacritics 2 tokenchars ''_-:/#'''
        );
        """
    )


@contextmanager
def _index_lock(state_dir: Path) -> Iterator[None]:
    """Serialize index publication while keeping the lock file recoverable."""
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "index.lock"
    handle = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        yield
    finally:
        if os.name == "nt":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def _eligible_markdown(root: Path) -> list[Path]:
    paths: list[Path] = []
    for path in root.rglob("*.md"):
        relative = path.relative_to(root)
        parts = {part.casefold() for part in relative.parts}
        if any(part.startswith(".") for part in relative.parts):
            continue
        if "restricted" in parts or ".trash" in parts:
            continue
        paths.append(path)
    return sorted(paths, key=lambda item: item.as_posix().casefold())


def _insert_document(
    connection: sqlite3.Connection,
    document: MemoryDocument,
    chunks: list[MemoryChunk],
) -> None:
    values = asdict(document)
    connection.execute(
        """
        INSERT INTO documents VALUES (
            :memory_id, :path, :title, :body, :status, :root_scope,
            :scope_kind, :scope_id, :updated, :review_after, :related_json,
            :identifiers_json, :projects_json, :repos_json, :tools_json,
            :content_hash, :mtime_ns
        )
        """,
        {
            **values,
            "related_json": json.dumps(document.related),
            "identifiers_json": json.dumps(document.identifiers),
            "projects_json": json.dumps(document.projects),
            "repos_json": json.dumps(document.repos),
            "tools_json": json.dumps(document.tools),
        },
    )
    identifiers = " ".join(document.identifiers)
    for chunk in chunks:
        connection.execute(
            "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chunk.chunk_id,
                chunk.memory_id,
                chunk.path,
                chunk.title,
                chunk.heading,
                chunk.ordinal,
                chunk.text,
                json.dumps(chunk.vector, separators=(",", ":")),
            ),
        )
        connection.execute(
            "INSERT INTO chunks_fts VALUES (?, ?, ?, ?, ?)",
            (
                chunk.chunk_id,
                chunk.title,
                chunk.heading,
                chunk.text,
                identifiers,
            ),
        )


def _remove_document(connection: sqlite3.Connection, memory_id: str) -> None:
    chunk_ids = [
        row["chunk_id"]
        for row in connection.execute(
            "SELECT chunk_id FROM chunks WHERE memory_id = ?", (memory_id,)
        )
    ]
    for chunk_id in chunk_ids:
        connection.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (chunk_id,))
    connection.execute("DELETE FROM documents WHERE memory_id = ?", (memory_id,))


def current_index_path(settings: Settings) -> Path | None:
    if settings.pointer_path.exists():
        try:
            payload = json.loads(settings.pointer_path.read_text(encoding="utf-8"))
            candidate = settings.state_dir / payload["snapshot"]
            if candidate.exists():
                return candidate
        except (json.JSONDecodeError, KeyError, OSError):
            pass
    snapshots = sorted(settings.state_dir.glob("index-*.sqlite"), reverse=True)
    return snapshots[0] if snapshots else None


def build_index(settings: Settings, *, force: bool = False) -> dict[str, object]:
    started = time.perf_counter()
    if not settings.memory_root.is_dir():
        raise FileNotFoundError(f"Memory root not found: {settings.memory_root}")
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    with _index_lock(settings.state_dir):
        current = current_index_path(settings)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        snapshot = settings.state_dir / f"index-{stamp}-{os.getpid()}.sqlite"
        if current and not force:
            # SQLite backup produces a consistent new snapshot while preserving
            # every previously published snapshot for recovery and comparison.
            with _connect(current, read_only=True) as source, _connect(snapshot) as target:
                source.backup(target)
        with _connect(snapshot) as connection:
            _create_schema(connection)
            existing = {
                row["path"]: (row["memory_id"], row["content_hash"])
                for row in connection.execute(
                    "SELECT path, memory_id, content_hash FROM documents"
                )
            }
            seen: set[str] = set()
            added = changed = unchanged = removed = 0
            parse_errors: list[dict[str, str]] = []
            for path in _eligible_markdown(settings.memory_root):
                relative = path.relative_to(settings.memory_root).as_posix()
                seen.add(relative)
                try:
                    document = parse_document(path, settings.memory_root)
                except (OSError, UnicodeError, ValueError) as exc:
                    parse_errors.append({"path": relative, "error": str(exc)})
                    continue
                old = existing.get(relative)
                if old and old[1] == document.content_hash and not force:
                    unchanged += 1
                    continue
                if old:
                    _remove_document(connection, old[0])
                    changed += 1
                else:
                    # A memory_id can move paths; replace the old derived row
                    # rather than letting the unique identity split in two.
                    row = connection.execute(
                        "SELECT memory_id FROM documents WHERE memory_id = ?",
                        (document.memory_id,),
                    ).fetchone()
                    if row:
                        _remove_document(connection, row["memory_id"])
                        changed += 1
                    else:
                        added += 1
                _insert_document(
                    connection,
                    document,
                    chunk_document(document, settings.semantic_dimensions),
                )
            for path, (memory_id, _) in existing.items():
                if path not in seen:
                    _remove_document(connection, memory_id)
                    removed += 1
            counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM documents) AS documents,
                    (SELECT COUNT(*) FROM chunks) AS chunks
                """
            ).fetchone()
            metadata = {
                "schema_version": str(SCHEMA_VERSION),
                "built_at": _utc_now(),
                "memory_root": str(settings.memory_root.resolve()),
                "semantic_dimensions": str(settings.semantic_dimensions),
                "documents": str(counts["documents"]),
                "chunks": str(counts["chunks"]),
            }
            connection.executemany(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
                metadata.items(),
            )
            connection.commit()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"SQLite integrity check failed: {integrity}")
        pointer = {
            "snapshot": snapshot.name,
            "published_at": _utc_now(),
            "schema_version": SCHEMA_VERSION,
        }
        settings.pointer_path.write_text(
            json.dumps(pointer, indent=2), encoding="utf-8"
        )
    return {
        "snapshot": str(snapshot),
        "documents": int(counts["documents"]),
        "chunks": int(counts["chunks"]),
        "added": added,
        "changed": changed,
        "unchanged": unchanged,
        "removed": removed,
        "parse_errors": parse_errors,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
    }


class MemoryIndex:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._vector_cache: dict[
            tuple[str | None, ...], list[tuple[sqlite3.Row, dict[int, float]]]
        ] = {}
        self.path = current_index_path(settings)
        if self.path is None:
            build_index(settings)
            self.path = current_index_path(settings)
        if self.path is None:
            raise RuntimeError("Index publication completed without a snapshot")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with _connect(self.path, read_only=True) as connection:
            yield connection

    def metadata(self) -> dict[str, str]:
        with self.connection() as connection:
            return {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key, value FROM metadata")
            }

    def document(self, identity: str) -> dict[str, object] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM documents
                WHERE memory_id = ? OR path = ? OR path LIKE ?
                ORDER BY
                    CASE WHEN memory_id = ? THEN 0 WHEN path = ? THEN 1 ELSE 2 END,
                    length(path)
                LIMIT 1
                """,
                (identity, identity, f"%{identity}%", identity, identity),
            ).fetchone()
        return decode_document(row) if row else None

    def all_vectors(
        self, scope: ScopeFilter
    ) -> list[tuple[sqlite3.Row, dict[int, float]]]:
        cache_key = (
            scope.root_scope,
            scope.repository,
            scope.project,
            scope.ticket,
            scope.status,
            scope.path_prefix,
        )
        cached = self._vector_cache.get(cache_key)
        if cached is not None:
            return cached
        where, parameters = scope_sql(scope)
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT c.*, d.status, d.root_scope, d.scope_kind, d.scope_id,
                       d.projects_json, d.repos_json
                FROM chunks c JOIN documents d USING(memory_id)
                {where}
                """,
                parameters,
            ).fetchall()
        decoded = [
            (row, {int(key): value for key, value in json.loads(row["vector_json"]).items()})
            for row in rows
        ]
        # A published snapshot is immutable, so decoded vectors remain valid
        # until MemoryService replaces the engine after the next refresh.
        self._vector_cache[cache_key] = decoded
        return decoded

    def chunks_for_memory(self, memory_id: str) -> list[sqlite3.Row]:
        with self.connection() as connection:
            return connection.execute(
                """
                SELECT * FROM chunks WHERE memory_id = ?
                ORDER BY ordinal
                """,
                (memory_id,),
            ).fetchall()


def decode_document(row: sqlite3.Row) -> dict[str, object]:
    result = dict(row)
    for key in (
        "related_json",
        "identifiers_json",
        "projects_json",
        "repos_json",
        "tools_json",
    ):
        result[key.removesuffix("_json")] = json.loads(result.pop(key))
    return result


def scope_sql(scope: ScopeFilter) -> tuple[str, list[str]]:
    clauses: list[str] = []
    parameters: list[str] = []
    if scope.root_scope:
        clauses.append("d.root_scope = ?")
        parameters.append(scope.root_scope)
    if scope.status:
        clauses.append("d.status = ?")
        parameters.append(scope.status)
    if scope.path_prefix:
        clauses.append("d.path LIKE ?")
        parameters.append(f"{scope.path_prefix.rstrip('/')}%")
    if scope.repository:
        clauses.append(
            "(d.repos_json LIKE ? OR d.scope_id LIKE ? OR d.path LIKE ?)"
        )
        pattern = f"%{scope.repository}%"
        parameters.extend((pattern, pattern, pattern))
    if scope.project:
        clauses.append(
            "(d.projects_json LIKE ? OR d.scope_id LIKE ? OR d.path LIKE ?)"
        )
        pattern = f"%{scope.project}%"
        parameters.extend((pattern, pattern, pattern))
    if scope.ticket:
        clauses.append(
            "(d.identifiers_json LIKE ? OR d.scope_id LIKE ? OR d.path LIKE ?)"
        )
        pattern = f"%{scope.ticket}%"
        parameters.extend((pattern, pattern, pattern))
    return (f"WHERE {' AND '.join(clauses)}" if clauses else "", parameters)
