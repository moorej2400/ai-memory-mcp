from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

from ai_memory_mcp.config import Settings

from .identity import artifact_id, canonical_json, sha256_text
from .ingest import AUTH_QUERY_KEYS, SECRET_KEYS
from .models import (
    ArtifactActor,
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
from .schema import connect_artifact_db
from .store import ArtifactStore

REQUIRED_TABLES = {"conversations", "messages", "attachments", "meetings"}
CUE_RE = re.compile(
    r"^\s*\[?(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\]?\s+"
    r"(?P<speaker>[^:]{1,100}):\s*(?P<text>.*)$"
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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _legacy_connection(path: Path, *, immutable: bool) -> sqlite3.Connection:
    query = "mode=ro&immutable=1" if immutable else "mode=ro"
    uri = f"{path.resolve().as_uri()}?{query}"
    connection = sqlite3.connect(uri, uri=True, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def _note_paths(value: Path | Iterable[Path] | None) -> list[Path]:
    if value is None:
        return []
    if isinstance(value, Path):
        if value.is_dir():
            return sorted(value.rglob("*.md"), key=lambda path: path.as_posix())
        return [value] if value.is_file() else []
    return sorted(
        (path for path in value if path.is_file()),
        key=lambda path: path.as_posix(),
    )


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
    valid_ids = {str(row["conversation_id"] if "conversation_id" in row else row["id"]) for row in rows}
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
        if line.strip()
        and not line.lstrip().startswith(("#", "<!--", "-"))
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
    database_sha256 = _sha256_file(database)
    with _legacy_connection(database, immutable=immutable) as connection:
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
        conversations = [dict(row) for row in connection.execute("SELECT * FROM conversations ORDER BY id")]
        messages = [dict(row) for row in connection.execute("SELECT * FROM messages ORDER BY conversation_id, timestamp, message_id")]
        attachments = [dict(row) for row in connection.execute("SELECT * FROM attachments ORDER BY conversation_id, message_id, attachment_index")]
        meetings = [dict(row) for row in connection.execute("SELECT * FROM meetings ORDER BY conversation_id")]
        duplicate_natural_keys = sum(
            int(row[0])
            for query in (
                "SELECT count(*) FROM (SELECT id FROM conversations GROUP BY id HAVING count(*) > 1)",
                "SELECT count(*) FROM (SELECT conversation_id, message_id FROM messages GROUP BY conversation_id, message_id HAVING count(*) > 1)",
                "SELECT count(*) FROM (SELECT conversation_id FROM meetings GROUP BY conversation_id HAVING count(*) > 1)",
            )
            for row in [connection.execute(query).fetchone()]
        )
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


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize(child)
            for key, child in value.items()
            if str(key).replace("-", "_").casefold() not in SECRET_KEYS
        }
    if isinstance(value, list):
        return [_sanitize(child) for child in value]
    if not isinstance(value, str):
        return value
    parsed = urlsplit(value)
    if parsed.scheme.casefold() not in {"http", "https"}:
        return value
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in AUTH_QUERY_KEYS
    ]
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


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
    name = str(row.get("author") or "").strip()
    if stable_id:
        return ArtifactActor(
            id=str(stable_id),
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
        ArtifactActor(name=str(value), id_confidence="display-name-only")
        for value in raw
        if str(value).strip()
    ]


def _events(data: LegacyData, sync_db: Path) -> list[ArtifactEvent]:
    events: list[ArtifactEvent] = []
    attachments_by_message: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for attachment in data.attachments:
        key = (str(attachment["conversation_id"]), str(attachment["message_id"]))
        attachments_by_message.setdefault(key, []).append(attachment)

    for row in data.conversations:
        conversation_id = str(row["id"])
        note = data.chat_mapping.get(conversation_id)
        summary = _summary(note) if note else ""
        source_payload = {
            "legacy": _sanitize(
                {
                    "kind": row.get("kind"),
                    "thread_type": row.get("thread_type"),
                    "chat_sub_type": row.get("chat_sub_type"),
                    "raw": _raw_json(row.get("raw_json")),
                }
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
                        title=str(row.get("name") or "") or None,
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
        reactions = (
            [str(value) for value in raw.get("reactions", [])]
            if isinstance(raw, dict) and isinstance(raw.get("reactions"), list)
            else []
        )
        events.append(
            ArtifactEvent.model_validate(
                {
                    "schema": "ai-memory/artifact-event@1",
                    "record": "event",
                    "entity": "message",
                    "operation": "upsert",
                    "external_id": message_id,
                    "parent": {
                        "entity": "conversation",
                        "external_id": conversation_id,
                    },
                    "source_version": row.get("version"),
                    "source_updated_at": row.get("updated_at"),
                    "payload": ArtifactPayload(
                        occurred_at=row.get("timestamp"),
                        text=str(row.get("content_markdown") or ""),
                        content_format="markdown",
                        author=_actor(row),
                        links=attachment_links,
                        reactions=reactions,
                        classification=(
                            "system"
                            if str(row.get("message_type") or "").casefold()
                            == "system"
                            else None
                        ),
                        source_payload={
                            "legacy": _sanitize(
                                {
                                    "kind": row.get("kind"),
                                    "message_type": row.get("message_type"),
                                    "parent_id": row.get("parent_id"),
                                    "raw": raw,
                                }
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
                        "external_id": str(row["message_id"]),
                    },
                    "source_updated_at": row.get("updated_at"),
                    "payload": ArtifactPayload(
                        title=str(row.get("name") or "") or None,
                        object=ArtifactObjectInput(
                            local_source_path=source_path,
                            media_type=str(row.get("content_type") or "") or None,
                            original_name=str(row.get("name") or "") or None,
                        ),
                        source_payload={
                            "legacy": _sanitize(
                                {
                                    "kind": row.get("kind"),
                                    "remote_url": row.get("remote_url"),
                                    "status": row.get("status"),
                                    "raw": _raw_json(row.get("raw_json")),
                                }
                            )
                        },
                    ),
                }
            )
        )

    for row in data.meetings:
        conversation_id = str(row["conversation_id"])
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
            "legacy": _sanitize(
                {
                    "end_time": row.get("end_time"),
                    "join_url": row.get("join_url"),
                    "meeting_type": row.get("meeting_type"),
                    "raw": _raw_json(row.get("raw_json")),
                }
            )
        }
        if note is not None and (summary := _summary(note)):
            source_payload["legacy_summary_candidate"] = summary
        events.append(
            ArtifactEvent.model_validate(
                {
                    "schema": "ai-memory/artifact-event@1",
                    "record": "event",
                    "entity": "meeting",
                    "operation": "upsert",
                    "external_id": conversation_id,
                    "parent": {
                        "entity": "conversation",
                        "external_id": conversation_id,
                    },
                    "source_updated_at": row.get("updated_at"),
                    "payload": ArtifactPayload(
                        title=str(row.get("subject") or "") or None,
                        occurred_at=row.get("start_time"),
                        text=str(row.get("subject") or "") or None,
                        content_format="plain",
                        author=(
                            ArtifactActor(
                                id=str(row["organizer_id"]),
                                id_confidence="stable",
                            )
                            if row.get("organizer_id")
                            else None
                        ),
                        participants=_participants(note),
                        links=links,
                        source_payload=source_payload,
                    ),
                }
            )
        )
        if note is None:
            continue
        transcript_text = _section(note.body, "Transcript")
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
                        "external_id": conversation_id,
                    },
                    "source_version": note.sha256,
                    "source_updated_at": row.get("updated_at"),
                    "payload": ArtifactPayload(
                        title=f"{str(row.get('subject') or 'Meeting')} transcript",
                        occurred_at=row.get("start_time"),
                        text=transcript_text,
                        content_format="plain",
                        participants=_participants(note),
                        source_payload={
                            "legacy_note_name": note.name,
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
                            text=cue.text,
                            content_format="plain",
                            author=(
                                ArtifactActor(
                                    name=cue.speaker,
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


def _attachment_external_id(row: dict[str, Any]) -> str:
    return str(
        row.get("attachment_id")
        or (
            f"{row['conversation_id']}:{row['message_id']}:"
            f"{row['attachment_index']}"
        )
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
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        for value in values
    ]
    return max(parsed) if parsed else datetime(1970, 1, 1, tzinfo=timezone.utc)


def _source_files_unchanged(database: Path, data: LegacyData) -> bool:
    if _sha256_file(database) != data.plan.database_sha256:
        return False
    notes = [
        _read_note(note.path) for note in [*data.chat_notes, *data.meeting_notes]
    ]
    return _note_manifest(notes) == data.plan.note_manifest_sha256


def _verify_import(
    settings: Settings,
    data: LegacyData,
    events: list[ArtifactEvent],
) -> dict[str, Any]:
    expected_rows = []
    expected_by_entity: Counter[str] = Counter()
    expected_by_parent: Counter[tuple[str, str]] = Counter()
    expected_object_links = 0
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

    if actual_by_entity != expected_by_entity:
        raise RuntimeError("Legacy migration entity counts do not match.")
    if actual_by_parent != expected_by_parent:
        raise RuntimeError("Legacy migration parent counts do not match.")
    if len(object_rows) != expected_object_links:
        raise RuntimeError("Legacy migration object counts do not match.")
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
    input_sha256 = sha256_text(
        canonical_json(
            {
                "source": source,
                "source_instance": source_instance,
                "database_sha256": data.plan.database_sha256,
                "note_manifest_sha256": data.plan.note_manifest_sha256,
                "events": [
                    event.model_dump(mode="json", by_alias=True)
                    for event in events
                ],
            }
        )
    )
    batch_id = f"legacy-{input_sha256[:32]}"
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
        _verify_import(settings, data, events)
        if not _source_files_unchanged(database, data):
            raise RuntimeError("A legacy migration source changed during import.")
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
    evidence = canonical_json(
        {
            "database_sha256": data.plan.database_sha256,
            "note_manifest_sha256": data.plan.note_manifest_sha256,
            "counts": data.plan.model_dump(
                exclude={"source", "source_instance", "database_sha256", "note_manifest_sha256"}
            ),
            "verification": verification,
        }
    )
    with connect_artifact_db(settings.artifact_db) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO artifact_metadata(key, value) VALUES (?, ?)",
            (f"legacy_migration:{batch_id}", evidence),
        )
        connection.commit()
    return LegacyMigrationReceipt(
        **data.plan.model_dump(),
        batch_id=batch_id,
        accepted_events=receipt.accepted,
        unchanged_events=receipt.unchanged,
        source_files_changed=0,
        verified=True,
    )
