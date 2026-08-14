from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence, TextIO

from pydantic import BaseModel, ValidationError

from ai_memory_mcp.config import Settings

from .ingest import ingest_artifact_batch, read_artifact_batch
from .models import ArtifactScope
from .schema import (
    ARTIFACT_SCHEMA_VERSION,
    artifact_database_status,
    connect_artifact_db,
)
from .search import ArtifactSearch


class CliValidationError(ValueError):
    pass


class CliIntakeError(RuntimeError):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-memory-artifact",
        description="Manage canonical raw artifacts.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="Initialize the artifact database.")

    ingest = commands.add_parser("ingest", help="Ingest one JSONL batch.")
    ingest.add_argument("--input", required=True, help="JSONL file or -.")

    commands.add_parser("status", help="Show artifact database status.")

    search = commands.add_parser("search", help="Search active raw artifacts.")
    search.add_argument("--query", required=True)
    search.add_argument("--source")
    search.add_argument("--source-instance")
    search.add_argument("--entity", action="append", default=[])
    search.add_argument("--parent")
    search.add_argument("--date-from")
    search.add_argument("--date-to")
    search.add_argument("--limit", type=int, default=20)

    read = commands.add_parser("read", help="Read ordered artifact context.")
    read.add_argument("--reference", required=True)
    read.add_argument("--cursor")
    read.add_argument(
        "--direction",
        choices=("around", "before", "after"),
        default="around",
    )
    read.add_argument("--limit", type=int, default=20)
    read.add_argument("--include-payload", action="store_true")

    pending = commands.add_parser(
        "pending",
        help="List artifacts that need Markdown distillation.",
    )
    pending.add_argument("--entity", choices=("meeting", "conversation"))
    pending.add_argument("--source")
    pending.add_argument("--source-instance")
    pending.add_argument("--limit", type=int, default=20)

    distilled = commands.add_parser(
        "mark-distilled",
        help="Confirm a current Markdown distillation.",
    )
    distilled.add_argument("--reference", required=True)
    distilled.add_argument("--memory-id", required=True)
    distilled.add_argument("--source-id", required=True)
    distilled.add_argument("--path", required=True)
    distilled.add_argument("--event-id", required=True)
    distilled.add_argument("--source-digest", required=True)

    no_memory = commands.add_parser(
        "mark-no-durable-memory",
        help="Confirm that a conversation has no durable content.",
    )
    no_memory.add_argument("--reference", required=True)
    no_memory.add_argument("--event-id", required=True)
    no_memory.add_argument("--source-digest", required=True)
    no_memory.add_argument("--reason", required=True)

    commands.add_parser("backup", help="Create an artifact database backup.")
    commands.add_parser("check", help="Check artifact database integrity.")
    restore = commands.add_parser(
        "restore",
        help="Restore a backup to a new database path.",
    )
    restore.add_argument("--backup", required=True)
    restore.add_argument("--destination", required=True)

    legacy = commands.add_parser(
        "migrate-legacy",
        help="Import legacy local artifact data.",
    )
    legacy.add_argument("--source", required=True)
    legacy.add_argument("--source-instance", required=True)
    legacy.add_argument("--sync-db", required=True)
    legacy.add_argument("--chat-notes")
    legacy.add_argument("--meeting-notes")
    legacy.add_argument(
        "--immutable",
        action="store_true",
        help="Read a copied database that has no active WAL file.",
    )
    legacy.add_argument("--dry-run", action="store_true")
    return parser


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Value is not JSON serializable: {type(value).__name__}")


def _write_json(value: Any, stream: TextIO | None = None) -> None:
    output = stream or sys.stdout
    output.write(
        json.dumps(
            value,
            default=_json_default,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def _ingest(settings: Settings, args: argparse.Namespace) -> Any:
    try:
        if args.input == "-":
            batch = read_artifact_batch(
                sys.stdin.buffer,
                max_bytes=settings.artifact_batch_max_bytes,
            )
        else:
            with Path(args.input).open("rb") as stream:
                batch = read_artifact_batch(
                    stream,
                    max_bytes=settings.artifact_batch_max_bytes,
                )
    except (ValueError, ValidationError) as exc:
        raise CliValidationError(str(exc)) from exc
    except OSError as exc:
        raise CliIntakeError(str(exc)) from exc
    try:
        return ingest_artifact_batch(settings, batch)
    except Exception as exc:
        raise CliIntakeError(str(exc)) from exc


def _status(settings: Settings) -> dict[str, Any]:
    health = artifact_database_status(settings)
    common = {
        "available": False,
        "schema_version": health.schema_version,
        "database_path": str(settings.artifact_db),
        "journal_mode": None,
        "artifacts": 0,
        "active_artifacts": 0,
        "batches": 0,
        "pending_distillations": 0,
        "last_batch_at": None,
    }
    if not health.exists:
        return {**common, "integrity": health.integrity}
    if health.schema_version != ARTIFACT_SCHEMA_VERSION:
        return {
            **common,
            "integrity": health.integrity,
            "error": (
                "The artifact database schema is not current. "
                "Run an artifact intake or migration command."
            ),
        }
    with connect_artifact_db(
        settings.artifact_db,
        read_only=True,
    ) as connection:
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        artifacts = int(
            connection.execute("SELECT count(*) FROM artifacts").fetchone()[0]
        )
        active = int(
            connection.execute(
                "SELECT count(*) FROM artifacts "
                "WHERE deleted_at IS NULL AND redacted_at IS NULL"
            ).fetchone()[0]
        )
        batches = int(
            connection.execute("SELECT count(*) FROM artifact_batches").fetchone()[0]
        )
        pending = int(
            connection.execute(
                "SELECT count(*) FROM distillation_state "
                "WHERE status = 'pending'"
            ).fetchone()[0]
        )
        last = connection.execute(
            "SELECT MAX(completed_at) FROM artifact_batches WHERE status = 'ok'"
        ).fetchone()[0]
    return {
        "available": health.integrity == "ok",
        "schema_version": health.schema_version,
        "database_path": str(settings.artifact_db),
        "journal_mode": journal_mode,
        "artifacts": artifacts,
        "active_artifacts": active,
        "batches": batches,
        "pending_distillations": pending,
        "last_batch_at": last,
        "integrity": health.integrity,
    }


def _search(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    scope = ArtifactScope(
        source=args.source,
        source_instance=args.source_instance,
        entities=tuple(args.entity),
        parent=args.parent,
        date_from=args.date_from,
        date_to=args.date_to,
    )
    hits = ArtifactSearch(settings).search(args.query, scope, args.limit)
    return {"results": hits}


def _read(settings: Settings, args: argparse.Namespace) -> Any:
    return ArtifactSearch(settings).read(
        args.reference,
        cursor=args.cursor,
        direction=args.direction,
        limit=args.limit,
        include_payload=args.include_payload,
    )


def _pending(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    from .distillation import list_pending_distillations

    scope = ArtifactScope(
        source=args.source,
        source_instance=args.source_instance,
        entities=(args.entity,) if args.entity else (),
    )
    return {
        "candidates": list_pending_distillations(
            settings,
            scope=scope,
            limit=args.limit,
        )
    }


def _mark_distilled(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    from .distillation import mark_distilled

    mark_distilled(
        settings,
        artifact_uri=args.reference,
        memory_id=args.memory_id,
        memory_source_id=args.source_id,
        memory_path=args.path,
        event_id=args.event_id,
        source_digest=args.source_digest,
    )
    return {"reference": args.reference, "status": "distilled"}


def _mark_no_memory(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    from .distillation import mark_no_durable_memory

    mark_no_durable_memory(
        settings,
        artifact_uri=args.reference,
        event_id=args.event_id,
        source_digest=args.source_digest,
        reason=args.reason,
    )
    return {"reference": args.reference, "status": "no-durable-memory"}


def _backup(settings: Settings, args: argparse.Namespace) -> Any:
    from .backup import backup_artifact_db

    return backup_artifact_db(settings)


def _init(settings: Settings, args: argparse.Namespace) -> Any:
    from .schema import migrate_artifact_db

    return migrate_artifact_db(settings)


def _check(settings: Settings, args: argparse.Namespace) -> Any:
    from .backup import check_artifact_db

    return check_artifact_db(settings)


def _restore(settings: Settings, args: argparse.Namespace) -> Any:
    from .backup import restore_artifact_db

    return restore_artifact_db(
        Path(args.backup),
        Path(args.destination),
    )


def _migrate_legacy(settings: Settings, args: argparse.Namespace) -> Any:
    from .migrate_legacy import plan_legacy_migration, run_legacy_migration

    arguments = {
        "source": args.source,
        "source_instance": args.source_instance,
        "sync_db": Path(args.sync_db),
        "chat_notes": Path(args.chat_notes) if args.chat_notes else None,
        "meeting_notes": Path(args.meeting_notes) if args.meeting_notes else None,
        "immutable": args.immutable,
    }
    if args.dry_run:
        return plan_legacy_migration(**arguments)
    return run_legacy_migration(settings, **arguments)


HANDLERS = {
    "init": _init,
    "ingest": _ingest,
    "status": lambda settings, args: _status(settings),
    "search": _search,
    "read": _read,
    "pending": _pending,
    "mark-distilled": _mark_distilled,
    "mark-no-durable-memory": _mark_no_memory,
    "backup": _backup,
    "check": _check,
    "restore": _restore,
    "migrate-legacy": _migrate_legacy,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env()
        result = HANDLERS[args.command](settings, args)
    except (CliValidationError, ValidationError) as exc:
        sys.stderr.write(f"Validation error: {exc}\n")
        return 2
    except CliIntakeError as exc:
        sys.stderr.write(f"Intake error: {exc}\n")
        return 1
    except (ValueError, KeyError) as exc:
        sys.stderr.write(f"Validation error: {exc}\n")
        return 2
    except Exception as exc:
        sys.stderr.write(f"Artifact command failed: {exc}\n")
        return 1
    _write_json(result)
    return 0
