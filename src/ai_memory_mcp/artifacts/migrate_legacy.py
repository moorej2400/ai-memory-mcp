from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

import yaml

from ai_memory_mcp.config import Settings

from .identity import artifact_id, canonical_json, sha256_text
from .ingest import (
    AUTH_QUERY_KEY_TOKENS,
    HTTP_URL_RE,
    PROTOCOL_RELATIVE_URL_RE,
    SECRET_ASSIGNMENT_RE,
    SECRET_KEY_TOKENS,
    _decode_html_url_entities,
    _normalize_security_key,
    _reject_secret_material,
)
from .models import (
    ArtifactActor,
    ArtifactAlias,
    ArtifactBatchManifest,
    ArtifactEvent,
    ArtifactLink,
    ArtifactObjectInput,
    ArtifactPayload,
    ArtifactReference,
    LegacyMigrationPlan,
    LegacyMigrationReceipt,
    ParsedArtifactBatch,
)
from .objects import verify_object
from .schema import connect_artifact_db, require_local_database_path
from .store import ArtifactStore

REQUIRED_TABLES = {"conversations", "messages", "attachments", "meetings"}
CUE_RE = re.compile(
    r"^\s*\[?(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\]?\s+"
    r"(?P<speaker>[^:]{1,100}):\s*(?P<text>.*)$"
)
URL_QUERY_VALUE_RE = re.compile(
    r"(?P<delimiter>[?&#])(?P<html>amp;)?(?P<key>[A-Za-z0-9_.-]+)="
    r"(?P<value>[^&#\s<>\"']*)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class LegacyNote:
    path: Path
    name: str
    metadata: dict[str, Any]
    body: str
    sha256: str


@dataclass(frozen=True, slots=True)
class LegacyCue:
    ordinal: int
    speaker: str
    text: str
    occurred_at: datetime | None


@dataclass(frozen=True, slots=True)
class LegacyData:
    plan: LegacyMigrationPlan
    conversations: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    attachments: list[dict[str, Any]]
    meetings: list[dict[str, Any]]
    chat_notes: list[LegacyNote]
    meeting_notes: list[LegacyNote]
    chat_mapping: dict[str, LegacyNote]
    meeting_mapping: dict[str, LegacyNote]
    cues: dict[str, list[LegacyCue]]
    source_database_fingerprint: tuple[tuple[str, int, int, str], ...]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _source_database_paths(path: Path) -> tuple[Path, ...]:
    # SHM bytes describe reader locks, not logical rows, and can change on inspection.
    return (
        path,
        Path(f"{path}-wal"),
        Path(f"{path}-journal"),
    )


def _source_database_fingerprint(
    path: Path,
) -> tuple[tuple[str, int, int, str], ...]:
    values: list[tuple[str, int, int, str]] = []
    for candidate in _source_database_paths(path):
        try:
            before = candidate.stat()
            digest = _sha256_file(candidate)
            after = candidate.stat()
        except FileNotFoundError:
            continue
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise RuntimeError("The legacy database changed during inspection.")
        values.append(
            (
                candidate.name,
                after.st_size,
                after.st_mtime_ns,
                digest,
            )
        )
    if not values or values[0][0] != path.name:
        raise FileNotFoundError("The legacy database is not available.")
    return tuple(values)


def _legacy_connection(path: Path, *, immutable: bool) -> sqlite3.Connection:
    query = "mode=ro&immutable=1" if immutable else "mode=ro"
    uri = f"{path.resolve().as_uri()}?{query}"
    connection = sqlite3.connect(uri, uri=True, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def _logical_snapshot(
    path: Path,
    *,
    immutable: bool,
) -> tuple[sqlite3.Connection, str, tuple[tuple[str, int, int, str], ...]]:
    path = require_local_database_path(path)
    before = _source_database_fingerprint(path)
    has_wal = Path(f"{path}-wal").is_file()
    # SQLite immutable mode ignores WAL files. Use read-only mode when a WAL exists.
    source = _legacy_connection(path, immutable=immutable and not has_wal)
    snapshot = sqlite3.connect(":memory:")
    try:
        source.backup(snapshot)
    except BaseException:
        snapshot.close()
        raise
    finally:
        source.close()
    after = _source_database_fingerprint(path)
    if before != after:
        snapshot.close()
        raise RuntimeError("The legacy database changed during snapshot creation.")
    snapshot.row_factory = sqlite3.Row
    snapshot.execute("PRAGMA foreign_keys = ON")
    snapshot.execute("PRAGMA query_only = ON")
    return snapshot, hashlib.sha256(snapshot.serialize()).hexdigest(), after


def _note_paths(value: Path | Iterable[Path] | None) -> list[Path]:
    if value is None:
        return []
    if isinstance(value, Path):
        if not value.exists():
            raise FileNotFoundError(f"The legacy note path does not exist: {value}")
        if value.is_dir():
            return sorted(value.rglob("*.md"), key=lambda path: path.as_posix())
        if value.is_file():
            return [value]
        raise ValueError(f"The legacy note path is not a file or directory: {value}")
    paths = list(value)
    missing = next((path for path in paths if not path.exists()), None)
    if missing is not None:
        raise FileNotFoundError(f"The legacy note path does not exist: {missing}")
    invalid = next((path for path in paths if not path.is_file()), None)
    if invalid is not None:
        raise ValueError(f"The legacy note path is not a file: {invalid}")
    return sorted(paths, key=lambda path: path.as_posix())


def _read_note(path: Path) -> LegacyNote:
    raw = path.read_text(encoding="utf-8-sig")
    metadata: dict[str, Any] = {}
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            loaded = yaml.safe_load(parts[1]) or {}
            if isinstance(loaded, dict):
                metadata = loaded
            body = parts[2].lstrip()
    return LegacyNote(
        path=path,
        name=path.name,
        metadata=metadata,
        body=body,
        sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    )


def _note_manifest(notes: list[LegacyNote]) -> str:
    values = sorted((note.name, note.sha256) for note in notes)
    return sha256_text(canonical_json(values))


def _conversation_id(note: LegacyNote) -> str | None:
    for key in (
        "conversation_id",
        "conversation-id",
        "source_conversation_id",
    ):
        value = note.metadata.get(key)
        if value:
            return str(value)
    return None


def _title(note: LegacyNote) -> str:
    title = note.metadata.get("title")
    if title:
        return str(title)
    match = re.search(r"(?m)^#\s+(.+?)\s*$", note.body)
    return match.group(1) if match else note.path.stem


def _map_notes(
    notes: list[LegacyNote],
    rows: list[dict[str, Any]],
    *,
    title_key: str,
) -> tuple[dict[str, LegacyNote], int, int]:
    valid_ids = {
        str(row["conversation_id"] if "conversation_id" in row else row["id"])
        for row in rows
    }
    mapping: dict[str, LegacyNote] = {}
    unresolved = 0
    duplicates = 0
    for note in notes:
        candidate = _conversation_id(note)
        if candidate not in valid_ids:
            note_title = _title(note).casefold()
            matches = [
                str(row["conversation_id"] if "conversation_id" in row else row["id"])
                for row in rows
                if str(row.get(title_key) or "").casefold() == note_title
            ]
            if len(matches) == 1:
                candidate = matches[0]
            else:
                candidate = None
        if candidate is None or candidate not in valid_ids:
            unresolved += 1
            continue
        if candidate in mapping:
            duplicates += 1
            continue
        mapping[candidate] = note
    return mapping, unresolved, duplicates


def _section(body: str, heading: str) -> str:
    match = re.search(
        rf"(?ms)^##\s+{re.escape(heading)}\s*$\n(.*?)(?=^##\s+|\Z)",
        body,
    )
    return match.group(1).strip() if match else ""


def _summary(note: LegacyNote) -> str:
    if note.metadata.get("summary"):
        return str(note.metadata["summary"]).strip()
    summary = _section(note.body, "Summary")
    if summary:
        return summary
    preface = re.split(r"(?m)^##\s+", note.body, maxsplit=1)[0]
    lines = [
        line.strip()
        for line in preface.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "<!--", "-"))
    ]
    return "\n".join(lines).strip()


def _base_date(note: LegacyNote, meeting: dict[str, Any]) -> datetime | None:
    raw = (
        note.metadata.get("meeting_start")
        or note.metadata.get("meeting_date")
        or meeting.get("start_time")
    )
    if not raw:
        return None
    try:
        value = str(raw)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.fromisoformat(f"{raw}T00:00:00+00:00")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_cues(note: LegacyNote, meeting: dict[str, Any]) -> list[LegacyCue]:
    transcript = _section(note.body, "Transcript")
    if not transcript:
        return []
    base = _base_date(note, meeting)
    values: list[dict[str, Any]] = []
    for line in transcript.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = CUE_RE.match(stripped)
        if match:
            parts = [int(value) for value in match.group("time").split(":")]
            while len(parts) < 3:
                parts.append(0)
            occurred = None
            if base is not None:
                occurred = datetime.combine(
                    base.date(),
                    time(parts[0], parts[1], parts[2]),
                    tzinfo=base.tzinfo,
                )
            values.append(
                {
                    "speaker": match.group("speaker").strip(),
                    "text": match.group("text").strip(),
                    "occurred_at": occurred,
                }
            )
        elif values:
            values[-1]["text"] = f"{values[-1]['text']}\n{stripped}".strip()
        else:
            values.append({"speaker": "", "text": stripped, "occurred_at": None})
    return [
        LegacyCue(
            ordinal=index,
            speaker=str(value["speaker"]),
            text=str(value["text"]),
            occurred_at=value["occurred_at"],
        )
        for index, value in enumerate(values)
    ]


def _load_legacy(
    *,
    source: str,
    source_instance: str,
    sync_db: Path,
    chat_notes: Path | Iterable[Path] | None,
    meeting_notes: Path | Iterable[Path] | None,
    immutable: bool,
) -> LegacyData:
    database = sync_db.expanduser().resolve(strict=True)
    if not database.is_file():
        raise ValueError("The legacy database must be a regular file.")
    connection, database_sha256, source_database_fingerprint = _logical_snapshot(
        database,
        immutable=immutable,
    )
    try:
        names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing = sorted(REQUIRED_TABLES - names)
        if missing:
            raise ValueError(
                f"The legacy database is missing a required table: {missing[0]}."
            )
        conversations = [
            dict(row)
            for row in connection.execute("SELECT * FROM conversations ORDER BY id")
        ]
        messages = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM messages ORDER BY conversation_id, timestamp, message_id"
            )
        ]
        attachments = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM attachments ORDER BY conversation_id, message_id, attachment_index"
            )
        ]
        meetings = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM meetings ORDER BY conversation_id"
            )
        ]
        duplicate_natural_keys = sum(
            int(row[0])
            for query in (
                "SELECT count(*) FROM (SELECT id FROM conversations GROUP BY id HAVING count(*) > 1)",
                "SELECT count(*) FROM (SELECT conversation_id, message_id FROM messages GROUP BY conversation_id, message_id HAVING count(*) > 1)",
                "SELECT count(*) FROM (SELECT conversation_id, message_id, attachment_index FROM attachments GROUP BY conversation_id, message_id, attachment_index HAVING count(*) > 1)",
                "SELECT count(*) FROM (SELECT conversation_id FROM meetings GROUP BY conversation_id HAVING count(*) > 1)",
            )
            for row in [connection.execute(query).fetchone()]
        )
    finally:
        connection.close()
    chat_values = [_read_note(path) for path in _note_paths(chat_notes)]
    meeting_values = [_read_note(path) for path in _note_paths(meeting_notes)]
    chat_mapping, unresolved_chat, duplicate_chat = _map_notes(
        chat_values,
        conversations,
        title_key="name",
    )
    meeting_mapping, unresolved_meeting, duplicate_meeting = _map_notes(
        meeting_values,
        meetings,
        title_key="subject",
    )
    meeting_by_id = {str(row["conversation_id"]): row for row in meetings}
    cues = {
        conversation_id: _parse_cues(note, meeting_by_id[conversation_id])
        for conversation_id, note in meeting_mapping.items()
    }
    note_manifest = _note_manifest([*chat_values, *meeting_values])
    plan = LegacyMigrationPlan(
        source=source,
        source_instance=source_instance,
        database_sha256=database_sha256,
        note_manifest_sha256=note_manifest,
        conversations=len(conversations),
        messages=len(messages),
        attachments=len(attachments),
        meetings=len(meetings),
        meeting_notes=len(meeting_values),
        chat_notes=len(chat_values),
        transcript_cues=sum(len(value) for value in cues.values()),
        unresolved_identities=unresolved_chat + unresolved_meeting,
        duplicate_natural_keys=(
            duplicate_natural_keys + duplicate_chat + duplicate_meeting
        ),
    )
    return LegacyData(
        plan=plan,
        conversations=conversations,
        messages=messages,
        attachments=attachments,
        meetings=meetings,
        chat_notes=chat_values,
        meeting_notes=meeting_values,
        chat_mapping=chat_mapping,
        meeting_mapping=meeting_mapping,
        cues=cues,
        source_database_fingerprint=source_database_fingerprint,
    )


def plan_legacy_migration(
    *,
    source: str,
    source_instance: str,
    sync_db: Path,
    chat_notes: Path | Iterable[Path] | None = None,
    meeting_notes: Path | Iterable[Path] | None = None,
    immutable: bool = False,
) -> LegacyMigrationPlan:
    return _load_legacy(
        source=source,
        source_instance=source_instance,
        sync_db=sync_db,
        chat_notes=chat_notes,
        meeting_notes=meeting_notes,
        immutable=immutable,
    ).plan


def _sanitize_fragment(value: str, *, depth: int) -> str:
    route, marker, query_text = value.partition("?")
    safe_route = _sanitize_text(route, depth=depth + 1)
    if not marker:
        return safe_route
    safe_query = [
        (key, _sanitize_text(child, depth=depth + 1))
        for key, child in parse_qsl(query_text, keep_blank_values=True)
        if _normalize_security_key(key) not in AUTH_QUERY_KEY_TOKENS
    ]
    encoded = urlencode(safe_query)
    return f"{safe_route}?{encoded}" if encoded else safe_route


def _sanitize_url(value: str, *, depth: int) -> str:
    if depth > 8:
        return "[redacted-url]"
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return "[redacted-url]"
    protocol_relative = not parsed.scheme and bool(parsed.netloc)
    if parsed.scheme.casefold() not in {"http", "https"} and not protocol_relative:
        return value
    # Rebuild the authority from the host and port so URL userinfo cannot survive.
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    safe_query = [
        (key, _sanitize_text(child, depth=depth + 1))
        for key, child in parse_qsl(parsed.query, keep_blank_values=True)
        if _normalize_security_key(key) not in AUTH_QUERY_KEY_TOKENS
    ]
    return urlunsplit(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            urlencode(safe_query),
            _sanitize_fragment(parsed.fragment, depth=depth + 1),
        )
    )


def _sanitize_text(value: str, *, depth: int = 0) -> str:
    if depth > 8:
        return "[redacted]"
    try:
        value = _decode_html_url_entities(value)
    except ValueError:
        return "[redacted]"
    decoded = unquote(value)
    if decoded != value and (
        HTTP_URL_RE.search(decoded)
        or PROTOCOL_RELATIVE_URL_RE.search(decoded)
        or SECRET_ASSIGNMENT_RE.search(decoded)
    ):
        return _sanitize_text(decoded, depth=depth + 1)

    def replace_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        candidate = raw.rstrip(".,;!)]}")
        suffix = raw[len(candidate) :]
        return f"{_sanitize_url(candidate, depth=depth + 1)}{suffix}"

    def replace_query_value(match: re.Match[str]) -> str:
        key = match.group("key")
        if _normalize_security_key(key) not in AUTH_QUERY_KEY_TOKENS:
            return match.group(0)
        # Legacy text can contain relative or schemeless URLs. Remove both the
        # secret key and value so the generic intake boundary can accept them.
        return f'{match.group("delimiter")}redacted=redacted'

    without_capability_urls = URL_QUERY_VALUE_RE.sub(replace_query_value, value)
    without_capability_urls = HTTP_URL_RE.sub(replace_url, without_capability_urls)
    without_capability_urls = PROTOCOL_RELATIVE_URL_RE.sub(
        replace_url,
        without_capability_urls,
    )
    return SECRET_ASSIGNMENT_RE.sub(
        r"\g<prefix>\g<indent>[redacted]",
        without_capability_urls,
    )


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize(child)
            for key, child in value.items()
            if _normalize_security_key(str(key)) not in SECRET_KEY_TOKENS
        }
    if isinstance(value, list):
        return [_sanitize(child) for child in value]
    if not isinstance(value, str):
        return value
    return _sanitize_text(value)


def _safe_text(value: Any) -> str:
    return _sanitize_text(str(value or ""))


def _legacy_source_payload(
    row: dict[str, Any],
    keys: tuple[str, ...],
) -> dict[str, Any]:
    return {key: _sanitize(row.get(key)) for key in keys if row.get(key) is not None}


def _raw_json(value: Any) -> Any:
    if not value:
        return None
    try:
        return _sanitize(json.loads(str(value)))
    except json.JSONDecodeError:
        return None


def _actor(row: dict[str, Any]) -> ArtifactActor | None:
    raw = _raw_json(row.get("raw_json"))
    stable_id = raw.get("authorId") if isinstance(raw, dict) else None
    name = _safe_text(row.get("author")).strip()
    if stable_id:
        return ArtifactActor(
            id=_safe_text(stable_id),
            name=name or None,
            id_confidence="stable",
        )
    if name:
        return ArtifactActor(name=name, id_confidence="display-name-only")
    return None


def _participants(note: LegacyNote | None) -> list[ArtifactActor]:
    if note is None:
        return []
    raw = note.metadata.get("participants", [])
    if not isinstance(raw, list):
        return []
    return [
        ArtifactActor(name=_safe_text(value), id_confidence="display-name-only")
        for value in raw
        if _safe_text(value).strip()
    ]


def _reactions(raw: Any) -> list[str]:
    if not isinstance(raw, dict) or not isinstance(raw.get("reactions"), list):
        return []
    values: list[str] = []
    for reaction in raw["reactions"]:
        if isinstance(reaction, str):
            value = reaction
        elif isinstance(reaction, dict):
            value = reaction.get("type") or reaction.get("reactionType") or ""
        else:
            value = ""
        if safe := _safe_text(value).strip():
            values.append(safe)
    return values


def _events(data: LegacyData, sync_db: Path) -> list[ArtifactEvent]:
    events: list[ArtifactEvent] = []
    attachments_by_message: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for attachment in data.attachments:
        key = (str(attachment["conversation_id"]), str(attachment["message_id"]))
        attachments_by_message.setdefault(key, []).append(attachment)

    for row in data.conversations:
        conversation_id = str(row["id"])
        note = data.chat_mapping.get(conversation_id)
        summary = _safe_text(_summary(note)) if note else ""
        source_payload = {
            "legacy": _legacy_source_payload(
                row,
                ("kind", "thread_type", "chat_sub_type"),
            )
        }
        if summary:
            source_payload["legacy_summary_candidate"] = summary
        events.append(
            ArtifactEvent.model_validate(
                {
                    "schema": "ai-memory/artifact-event@1",
                    "record": "event",
                    "entity": "conversation",
                    "operation": "upsert",
                    "external_id": conversation_id,
                    "source_updated_at": row.get("updated_at"),
                    "payload": ArtifactPayload(
                        title=_safe_text(row.get("name")) or None,
                        occurred_at=row.get("first_seen_at"),
                        content_format="plain",
                        participants=_participants(note),
                        source_payload=source_payload,
                    ),
                }
            )
        )

    for row in data.messages:
        conversation_id = str(row["conversation_id"])
        message_id = str(row["message_id"])
        attachment_links = [
            ArtifactLink(
                relation="attachment",
                target=ArtifactReference(
                    entity="attachment",
                    external_id=_attachment_external_id(attachment),
                ),
            )
            for attachment in attachments_by_message.get(
                (conversation_id, message_id), []
            )
        ]
        raw = _raw_json(row.get("raw_json"))
        reactions = _reactions(raw)
        events.append(
            ArtifactEvent.model_validate(
                {
                    "schema": "ai-memory/artifact-event@1",
                    "record": "event",
                    "entity": "message",
                    "operation": "upsert",
                    "external_id": _message_external_id(
                        conversation_id,
                        message_id,
                    ),
                    "parent": {
                        "entity": "conversation",
                        "external_id": conversation_id,
                    },
                    "source_version": row.get("version"),
                    "source_updated_at": row.get("updated_at"),
                    "payload": ArtifactPayload(
                        occurred_at=row.get("timestamp"),
                        text=_safe_text(row.get("content_markdown")),
                        content_format="markdown",
                        author=_actor(row),
                        links=attachment_links,
                        reactions=reactions,
                        classification=(
                            "system"
                            if str(row.get("message_type") or "").casefold() == "system"
                            else None
                        ),
                        source_payload={
                            "legacy": _legacy_source_payload(
                                row,
                                ("kind", "message_type", "parent_id"),
                            )
                        },
                    ),
                }
            )
        )

    for row in data.attachments:
        local_path = row.get("local_path")
        source_path = None
        if local_path:
            candidate = Path(str(local_path)).expanduser()
            if not candidate.is_absolute():
                candidate = sync_db.parent / candidate
            if candidate.is_file():
                source_path = candidate
        events.append(
            ArtifactEvent.model_validate(
                {
                    "schema": "ai-memory/artifact-event@1",
                    "record": "event",
                    "entity": "attachment",
                    "operation": "upsert",
                    "external_id": _attachment_external_id(row),
                    "parent": {
                        "entity": "message",
                        "external_id": _message_external_id(
                            str(row["conversation_id"]),
                            str(row["message_id"]),
                        ),
                    },
                    "source_updated_at": row.get("updated_at"),
                    "payload": ArtifactPayload(
                        title=_safe_text(row.get("name")) or None,
                        object=ArtifactObjectInput(
                            local_source_path=source_path,
                            media_type=_safe_text(row.get("content_type")) or None,
                            original_name=_safe_text(row.get("name")) or None,
                        ),
                        source_payload={
                            "legacy": _legacy_source_payload(
                                row,
                                ("kind", "remote_url", "status"),
                            )
                        },
                    ),
                }
            )
        )

    for row in data.meetings:
        conversation_id = str(row["conversation_id"])
        meeting_external_id = _meeting_external_id(row)
        note = data.meeting_mapping.get(conversation_id)
        transcript_external_id = _transcript_external_id(conversation_id)
        links = [
            ArtifactLink(
                relation="related-chat",
                target=ArtifactReference(
                    entity="conversation",
                    external_id=conversation_id,
                ),
            )
        ]
        if note is not None:
            links.append(
                ArtifactLink(
                    relation="contains",
                    target=ArtifactReference(
                        entity="transcript",
                        external_id=transcript_external_id,
                    ),
                )
            )
        source_payload = {
            "legacy": _legacy_source_payload(
                row,
                ("end_time", "join_url", "meeting_type"),
            )
        }
        if note is not None and (summary := _safe_text(_summary(note))):
            source_payload["legacy_summary_candidate"] = summary
        events.append(
            ArtifactEvent.model_validate(
                {
                    "schema": "ai-memory/artifact-event@1",
                    "record": "event",
                    "entity": "meeting",
                    "operation": "upsert",
                    "external_id": meeting_external_id,
                    "parent": {
                        "entity": "conversation",
                        "external_id": conversation_id,
                    },
                    "source_updated_at": row.get("updated_at"),
                    "payload": ArtifactPayload(
                        title=_safe_text(row.get("subject")) or None,
                        occurred_at=row.get("start_time"),
                        text=_safe_text(row.get("subject")) or None,
                        content_format="plain",
                        author=(
                            ArtifactActor(
                                id=_safe_text(row["organizer_id"]),
                                id_confidence="stable",
                            )
                            if row.get("organizer_id")
                            else None
                        ),
                        participants=_participants(note),
                        aliases=[
                            ArtifactAlias(
                                kind="conversation-id",
                                value=conversation_id,
                            )
                        ],
                        links=links,
                        source_payload=source_payload,
                    ),
                }
            )
        )
        if note is None:
            continue
        transcript_text = _safe_text(_section(note.body, "Transcript"))
        events.append(
            ArtifactEvent.model_validate(
                {
                    "schema": "ai-memory/artifact-event@1",
                    "record": "event",
                    "entity": "transcript",
                    "operation": "upsert",
                    "external_id": transcript_external_id,
                    "parent": {
                        "entity": "meeting",
                        "external_id": meeting_external_id,
                    },
                    "source_version": note.sha256,
                    "source_updated_at": row.get("updated_at"),
                    "payload": ArtifactPayload(
                        title=(
                            f"{_safe_text(row.get('subject') or 'Meeting')} transcript"
                        ),
                        occurred_at=row.get("start_time"),
                        text=transcript_text,
                        content_format="plain",
                        participants=_participants(note),
                        source_payload={
                            "legacy_note_name": _safe_text(note.name),
                            "legacy_note_sha256": note.sha256,
                        },
                    ),
                }
            )
        )
        for cue in data.cues.get(conversation_id, []):
            events.append(
                ArtifactEvent.model_validate(
                    {
                        "schema": "ai-memory/artifact-event@1",
                        "record": "event",
                        "entity": "transcript-cue",
                        "operation": "upsert",
                        "external_id": f"{transcript_external_id}:cue:{cue.ordinal}",
                        "parent": {
                            "entity": "transcript",
                            "external_id": transcript_external_id,
                        },
                        "source_version": note.sha256,
                        "source_sequence": cue.ordinal,
                        "source_updated_at": row.get("updated_at"),
                        "payload": ArtifactPayload(
                            occurred_at=cue.occurred_at,
                            text=_safe_text(cue.text),
                            content_format="plain",
                            author=(
                                ArtifactActor(
                                    name=_safe_text(cue.speaker),
                                    id_confidence="display-name-only",
                                )
                                if cue.speaker
                                else None
                            ),
                        ),
                    }
                )
            )
    return events


def _message_external_id(conversation_id: str, message_id: str) -> str:
    # The legacy schema scopes message IDs to a conversation.
    digest = sha256_text(canonical_json([conversation_id, message_id]))
    return f"legacy-message:{digest}"


def _attachment_external_id(row: dict[str, Any]) -> str:
    # Providers can reuse attachment IDs, so the full legacy natural key is required.
    digest = sha256_text(
        canonical_json(
            [
                str(row["conversation_id"]),
                str(row["message_id"]),
                int(row["attachment_index"]),
                str(row.get("attachment_id") or ""),
            ]
        )
    )
    return f"legacy-attachment:{digest}"


def _meeting_external_id(row: dict[str, Any]) -> str:
    conversation_id = str(row["conversation_id"])
    occurrence = str(
        row.get("start_time")
        or row.get("updated_at")
        or row.get("first_seen_at")
        or "legacy"
    )
    try:
        parsed = datetime.fromisoformat(occurrence.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        occurrence = (
            parsed.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    except ValueError:
        pass
    return (
        f"conversation:{quote(conversation_id, safe='')}:"
        f"{quote(occurrence, safe='')}"
    )


def _transcript_external_id(conversation_id: str) -> str:
    return f"{conversation_id}:legacy-transcript"


def _observed_at(data: LegacyData) -> datetime:
    values = [
        row.get("updated_at")
        for rows in (
            data.conversations,
            data.messages,
            data.attachments,
            data.meetings,
        )
        for row in rows
        if row.get("updated_at")
    ]
    parsed = [
        datetime.fromisoformat(str(value).replace("Z", "+00:00")) for value in values
    ]
    return max(parsed) if parsed else datetime(1970, 1, 1, tzinfo=timezone.utc)


def _source_files_unchanged(database: Path, data: LegacyData) -> bool:
    if _source_database_fingerprint(database) != data.source_database_fingerprint:
        return False
    notes = [_read_note(note.path) for note in [*data.chat_notes, *data.meeting_notes]]
    return _note_manifest(notes) == data.plan.note_manifest_sha256


def _validate_migration_events(
    data: LegacyData,
    events: list[ArtifactEvent],
) -> None:
    artifact_ids: dict[str, ArtifactEvent] = {}
    for event in events:
        value = artifact_id(
            data.plan.source,
            data.plan.source_instance,
            event.entity,
            event.external_id,
        )
        if value in artifact_ids:
            raise ValueError("Legacy records map to duplicate artifact identities.")
        artifact_ids[value] = event
        # Migration is a trust-boundary adapter. Validate the final envelope too.
        _reject_secret_material(event.model_dump(mode="json", by_alias=True))

    for value, event in artifact_ids.items():
        references: list[ArtifactReference] = []
        if event.parent is not None:
            references.append(event.parent)
        if isinstance(event.payload, ArtifactPayload):
            references.extend(link.target for link in event.payload.links)
        for reference in references:
            target = artifact_id(
                data.plan.source,
                data.plan.source_instance,
                reference.entity,
                reference.external_id,
            )
            if target not in artifact_ids:
                raise ValueError(
                    "A legacy artifact references an identity outside the import: "
                    f"{value}."
                )


def _verify_import(
    settings: Settings,
    data: LegacyData,
    events: list[ArtifactEvent],
) -> dict[str, Any]:
    expected_rows = []
    expected_by_entity: Counter[str] = Counter()
    expected_by_parent: Counter[tuple[str, str]] = Counter()
    expected_object_links = 0
    expected_aliases: set[tuple[str, str, str]] = set()
    for event in events:
        value = artifact_id(
            data.plan.source,
            data.plan.source_instance,
            event.entity,
            event.external_id,
        )
        parent_value = None
        if event.parent is not None:
            parent_value = artifact_id(
                data.plan.source,
                data.plan.source_instance,
                event.parent.entity,
                event.parent.external_id,
            )
            expected_by_parent[(event.entity, parent_value)] += 1
        expected_rows.append((value, event.entity, parent_value))
        expected_by_entity[event.entity] += 1
        if isinstance(event.payload, ArtifactPayload):
            expected_aliases.update(
                (value, alias.kind, alias.value)
                for alias in event.payload.aliases
            )
        if (
            isinstance(event.payload, ArtifactPayload)
            and event.payload.object is not None
            and event.payload.object.local_source_path is not None
        ):
            expected_object_links += 1

    if len({row[0] for row in expected_rows}) != len(expected_rows):
        raise RuntimeError("Legacy records map to duplicate artifact identities.")

    with connect_artifact_db(
        settings.artifact_db,
        read_only=False,
    ) as connection:
        connection.execute(
            "CREATE TEMP TABLE expected_legacy_artifacts("
            "artifact_id TEXT PRIMARY KEY, entity TEXT, parent_artifact_id TEXT)"
        )
        connection.executemany(
            "INSERT INTO expected_legacy_artifacts VALUES (?, ?, ?)",
            expected_rows,
        )
        actual_by_entity = Counter(
            {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    "SELECT expected.entity, count(artifact.artifact_id) "
                    "FROM expected_legacy_artifacts AS expected "
                    "LEFT JOIN artifacts AS artifact "
                    "ON artifact.artifact_id = expected.artifact_id "
                    "AND artifact.deleted_at IS NULL "
                    "AND artifact.redacted_at IS NULL "
                    "GROUP BY expected.entity"
                )
            }
        )
        actual_by_parent = Counter(
            {
                (str(row[0]), str(row[1])): int(row[2])
                for row in connection.execute(
                    "SELECT expected.entity, expected.parent_artifact_id, "
                    "count(artifact.artifact_id) "
                    "FROM expected_legacy_artifacts AS expected "
                    "LEFT JOIN artifacts AS artifact "
                    "ON artifact.artifact_id = expected.artifact_id "
                    "AND artifact.parent_artifact_id = "
                    "expected.parent_artifact_id "
                    "AND artifact.deleted_at IS NULL "
                    "AND artifact.redacted_at IS NULL "
                    "WHERE expected.parent_artifact_id IS NOT NULL "
                    "GROUP BY expected.entity, expected.parent_artifact_id"
                )
            }
        )
        object_rows = list(
            connection.execute(
                "SELECT object.sha256, object.byte_count "
                "FROM expected_legacy_artifacts AS expected "
                "JOIN artifact_object_links AS link "
                "ON link.artifact_id = expected.artifact_id "
                "JOIN artifact_objects AS object ON object.sha256 = link.sha256"
            )
        )
        actual_aliases = {
            (str(row[0]), str(row[1]), str(row[2]))
            for row in connection.execute(
                "SELECT alias.artifact_id, alias.alias_kind, alias.alias_value "
                "FROM expected_legacy_artifacts AS expected "
                "JOIN artifact_aliases AS alias "
                "ON alias.artifact_id = expected.artifact_id "
                "WHERE alias.source = ? AND alias.source_instance = ?",
                (data.plan.source, data.plan.source_instance),
            )
        }

    if actual_by_entity != expected_by_entity:
        raise RuntimeError("Legacy migration entity counts do not match.")
    if actual_by_parent != expected_by_parent:
        raise RuntimeError("Legacy migration parent counts do not match.")
    if len(object_rows) != expected_object_links:
        raise RuntimeError("Legacy migration object counts do not match.")
    if actual_aliases != expected_aliases:
        raise RuntimeError("Legacy migration alias rows do not match.")
    verified_hashes: set[str] = set()
    for row in object_rows:
        digest = str(row[0])
        verification = verify_object(settings, digest)
        if not verification.ok or verification.byte_count != int(row[1]):
            raise RuntimeError("A legacy migration object hash does not match.")
        verified_hashes.add(digest)
    return {
        "active_by_entity": dict(sorted(actual_by_entity.items())),
        "children_by_parent": len(actual_by_parent),
        "object_links": len(object_rows),
        "object_hashes": len(verified_hashes),
        "aliases": len(actual_aliases),
        "unresolved_identities": data.plan.unresolved_identities,
    }


def run_legacy_migration(
    settings: Settings,
    *,
    source: str,
    source_instance: str,
    sync_db: Path,
    chat_notes: Path | Iterable[Path] | None = None,
    meeting_notes: Path | Iterable[Path] | None = None,
    immutable: bool = False,
) -> LegacyMigrationReceipt:
    database = sync_db.expanduser().resolve(strict=True)
    data = _load_legacy(
        source=source,
        source_instance=source_instance,
        sync_db=database,
        chat_notes=chat_notes,
        meeting_notes=meeting_notes,
        immutable=immutable,
    )
    if data.plan.unresolved_identities:
        raise ValueError("Resolve every legacy note identity before import.")
    if data.plan.duplicate_natural_keys:
        raise ValueError("Resolve duplicate legacy identities before import.")
    events = _events(data, database)
    _validate_migration_events(data, events)
    input_sha256 = sha256_text(
        canonical_json(
            {
                "source": source,
                "source_instance": source_instance,
                "database_sha256": data.plan.database_sha256,
                "note_manifest_sha256": data.plan.note_manifest_sha256,
                "events": [
                    event.model_dump(mode="json", by_alias=True) for event in events
                ],
            }
        )
    )
    batch_id = f"legacy-{input_sha256[:32]}"
    if not _source_files_unchanged(database, data):
        raise RuntimeError("A legacy migration source changed before import.")
    store = ArtifactStore(settings)
    with connect_artifact_db(
        settings.artifact_db,
        read_only=True,
    ) as connection:
        prior = connection.execute(
            "SELECT input_sha256 FROM artifact_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
    if prior is not None:
        if prior[0] != input_sha256:
            raise RuntimeError("The legacy migration batch identity has a conflict.")
        verification = _verify_import(settings, data, events)
        if not _source_files_unchanged(database, data):
            raise RuntimeError("A legacy migration source changed during import.")
        _record_migration_evidence(
            settings,
            batch_id,
            data,
            verification,
        )
        return LegacyMigrationReceipt(
            **data.plan.model_dump(),
            batch_id=batch_id,
            accepted_events=0,
            unchanged_events=len(events),
            source_files_changed=0,
            verified=True,
        )

    batch = ParsedArtifactBatch(
        manifest=ArtifactBatchManifest.model_validate(
            {
                "schema": "ai-memory/artifact-batch@1",
                "record": "batch",
                "batch_id": batch_id,
                "source": source,
                "source_instance": source_instance,
                "observed_at": _observed_at(data),
                "event_count": len(events),
            }
        ),
        events=events,
        input_sha256=input_sha256,
    )
    receipt = store.apply_batch(batch)
    verification = _verify_import(settings, data, events)
    if not _source_files_unchanged(database, data):
        raise RuntimeError("A legacy migration source changed during import.")
    _record_migration_evidence(
        settings,
        batch_id,
        data,
        verification,
    )
    return LegacyMigrationReceipt(
        **data.plan.model_dump(),
        batch_id=batch_id,
        accepted_events=receipt.accepted,
        unchanged_events=receipt.unchanged,
        source_files_changed=0,
        verified=True,
    )


def _record_migration_evidence(
    settings: Settings,
    batch_id: str,
    data: LegacyData,
    verification: dict[str, Any],
) -> None:
    evidence = canonical_json(
        {
            "database_sha256": data.plan.database_sha256,
            "note_manifest_sha256": data.plan.note_manifest_sha256,
            "counts": data.plan.model_dump(
                exclude={
                    "source",
                    "source_instance",
                    "database_sha256",
                    "note_manifest_sha256",
                }
            ),
            "verification": verification,
        }
    )
    with connect_artifact_db(settings.artifact_db) as connection:
        connection.execute(
            "INSERT INTO artifact_metadata(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"legacy_migration:{batch_id}", evidence),
        )
        connection.commit()
