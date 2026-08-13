from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from ai_memory_mcp.artifacts.ingest import read_artifact_batch
from ai_memory_mcp.artifacts.models import (
    ArtifactBatchManifest,
    ArtifactEvent,
    ArtifactPayload,
    ArtifactReference,
    CoverageClaim,
    ParsedArtifactBatch,
    RedactionPayload,
)
from ai_memory_mcp.artifacts.store import ArtifactStore
from ai_memory_mcp.config import Settings

OBSERVED = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def artifact_store(artifact_settings: Settings) -> ArtifactStore:
    return ArtifactStore(artifact_settings)


def _event(
    *,
    entity: str,
    external_id: str,
    text: str | None = None,
    parent_entity: str | None = None,
    parent_external_id: str | None = None,
    operation: str = "upsert",
    source_updated_at: str | None = None,
    source_sequence: int | None = None,
    source_version: str | None = None,
    classification: str | None = None,
    source_payload: dict[str, object] | None = None,
) -> ArtifactEvent:
    parent = None
    if parent_entity and parent_external_id:
        parent = ArtifactReference(
            entity=parent_entity,
            external_id=parent_external_id,
        )
    if operation == "delete":
        payload = None
    elif operation == "redact":
        payload = RedactionPayload(reason="Source privacy request")
    else:
        payload = ArtifactPayload(
            text=text,
            content_format="plain",
            classification=classification,
            source_payload=source_payload or {},
        )
    return ArtifactEvent.model_validate(
        {
            "schema": "ai-memory/artifact-event@1",
            "record": "event",
            "entity": entity,
            "operation": operation,
            "external_id": external_id,
            "parent": parent,
            "source_version": source_version,
            "source_sequence": source_sequence,
            "source_updated_at": source_updated_at,
            "payload": payload,
        }
    )


def _batch(
    events: list[ArtifactEvent],
    *,
    batch_id: str = "batch-1",
    coverage: list[CoverageClaim] | None = None,
    input_sha256: str | None = None,
) -> ParsedArtifactBatch:
    manifest = ArtifactBatchManifest.model_validate(
        {
            "schema": "ai-memory/artifact-batch@1",
            "record": "batch",
            "batch_id": batch_id,
            "source": "chat-source",
            "source_instance": "workspace",
            "observed_at": OBSERVED,
            "event_count": len(events),
            "coverage": coverage or [],
        }
    )
    return ParsedArtifactBatch(
        manifest=manifest,
        events=events,
        input_sha256=input_sha256 or f"{batch_id:0<64}"[:64],
    )


def message_batch(
    *,
    batch_id: str = "batch-1",
    text: str = "Use the blue setting.",
    source_updated_at: str = "2026-01-02T10:00:00Z",
    source_sequence: int | None = None,
) -> ParsedArtifactBatch:
    return _batch(
        [
            _event(
                entity="conversation",
                external_id="conversation-1",
                text="Example conversation",
                source_updated_at="2026-01-02T09:00:00Z",
            ),
            _event(
                entity="message",
                external_id="message-17",
                text=text,
                parent_entity="conversation",
                parent_external_id="conversation-1",
                source_updated_at=source_updated_at,
                source_sequence=source_sequence,
            ),
        ],
        batch_id=batch_id,
    )


def test_replayed_events_are_idempotent(artifact_store: ArtifactStore) -> None:
    first = artifact_store.apply_batch(message_batch())
    second = artifact_store.apply_batch(message_batch(batch_id="batch-2"))
    assert first.accepted == 2
    assert second.accepted == 0
    assert second.unchanged == 2
    assert artifact_store.count("message") == 1


def test_replayed_batch_id_returns_stored_receipt(
    artifact_store: ArtifactStore,
) -> None:
    batch = message_batch()
    first = artifact_store.apply_batch(batch)
    second = artifact_store.apply_batch(batch)
    assert second == first


def test_reused_batch_id_with_different_input_is_rejected(
    artifact_store: ArtifactStore,
) -> None:
    artifact_store.apply_batch(message_batch())
    changed = message_batch(text="Different")
    changed.input_sha256 = "f" * 64
    with pytest.raises(ValueError, match="batch ID"):
        artifact_store.apply_batch(changed)


def test_newer_edit_creates_revision_and_updates_current(
    artifact_store: ArtifactStore,
) -> None:
    artifact_store.apply_batch(message_batch())
    receipt = artifact_store.apply_batch(
        _batch(
            [
                _event(
                    entity="message",
                    external_id="message-17",
                    text="Use the green setting.",
                    parent_entity="conversation",
                    parent_external_id="conversation-1",
                    source_updated_at="2026-01-02T11:00:00Z",
                )
            ],
            batch_id="batch-2",
        )
    )
    assert receipt.accepted == 1
    current = artifact_store.get_by_external_id(
        "chat-source", "workspace", "message", "message-17"
    )
    assert current.text_content == "Use the green setting."
    assert artifact_store.event_count(current.artifact_id) == 2


def test_older_edit_is_recorded_but_does_not_replace_current(
    artifact_store: ArtifactStore,
) -> None:
    artifact_store.apply_batch(
        message_batch(text="New text", source_updated_at="2026-01-02T11:00:00Z")
    )
    receipt = artifact_store.apply_batch(
        _batch(
            [
                _event(
                    entity="message",
                    external_id="message-17",
                    text="Old text",
                    parent_entity="conversation",
                    parent_external_id="conversation-1",
                    source_updated_at="2026-01-02T10:00:00Z",
                )
            ],
            batch_id="batch-2",
        )
    )
    assert receipt.stale == 1
    current = artifact_store.get_by_external_id(
        "chat-source", "workspace", "message", "message-17"
    )
    assert current.text_content == "New text"
    assert artifact_store.event_count(current.artifact_id) == 2


def test_source_sequence_has_priority_over_source_time(
    artifact_store: ArtifactStore,
) -> None:
    artifact_store.apply_batch(message_batch(source_sequence=20))
    receipt = artifact_store.apply_batch(
        _batch(
            [
                _event(
                    entity="message",
                    external_id="message-17",
                    text="Sequence is older",
                    parent_entity="conversation",
                    parent_external_id="conversation-1",
                    source_sequence=19,
                    source_updated_at="2026-01-03T10:00:00Z",
                )
            ],
            batch_id="batch-2",
        )
    )
    assert receipt.stale == 1


def test_equal_ordering_value_with_different_content_is_a_conflict(
    artifact_store: ArtifactStore,
) -> None:
    artifact_store.apply_batch(message_batch(text="First"))
    receipt = artifact_store.apply_batch(
        _batch(
            [
                _event(
                    entity="message",
                    external_id="message-17",
                    text="Second",
                    parent_entity="conversation",
                    parent_external_id="conversation-1",
                    source_updated_at="2026-01-02T10:00:00Z",
                )
            ],
            batch_id="batch-2",
        )
    )
    assert receipt.conflicts == 1
    assert artifact_store.get_by_external_id(
        "chat-source", "workspace", "message", "message-17"
    ).text_content == "First"


def test_delete_tombstones_and_upsert_can_restore(
    artifact_store: ArtifactStore,
) -> None:
    artifact_store.apply_batch(message_batch())
    deleted = artifact_store.apply_batch(
        _batch(
            [
                _event(
                    entity="message",
                    external_id="message-17",
                    parent_entity="conversation",
                    parent_external_id="conversation-1",
                    operation="delete",
                    source_updated_at="2026-01-02T11:00:00Z",
                )
            ],
            batch_id="batch-2",
        )
    )
    assert deleted.tombstones == 1
    assert artifact_store.count("message") == 0

    restored = artifact_store.apply_batch(
        _batch(
            [
                _event(
                    entity="message",
                    external_id="message-17",
                    text="Restored",
                    parent_entity="conversation",
                    parent_external_id="conversation-1",
                    source_updated_at="2026-01-02T12:00:00Z",
                )
            ],
            batch_id="batch-3",
        )
    )
    assert restored.accepted == 1
    assert artifact_store.count("message") == 1


def test_redaction_clears_current_and_prior_revision_payloads(
    artifact_store: ArtifactStore,
) -> None:
    artifact_store.apply_batch(message_batch(text="Sensitive text"))
    receipt = artifact_store.apply_batch(
        _batch(
            [
                _event(
                    entity="message",
                    external_id="message-17",
                    parent_entity="conversation",
                    parent_external_id="conversation-1",
                    operation="redact",
                    source_updated_at="2026-01-02T11:00:00Z",
                )
            ],
            batch_id="batch-2",
        )
    )
    assert receipt.redactions == 1
    current = artifact_store.get_by_external_id(
        "chat-source", "workspace", "message", "message-17"
    )
    assert current.text_content == ""
    assert current.redacted_at is not None
    assert artifact_store.revision_payloads(current.artifact_id)[:-1] == [None]


def test_parent_must_exist_before_its_child_in_the_batch(
    artifact_store: ArtifactStore,
) -> None:
    child = _event(
        entity="message",
        external_id="message-1",
        text="Child",
        parent_entity="conversation",
        parent_external_id="conversation-1",
    )
    parent = _event(
        entity="conversation",
        external_id="conversation-1",
        text="Parent",
    )
    with pytest.raises(ValueError, match="parent"):
        artifact_store.apply_batch(_batch([child, parent]))
    assert artifact_store.count() == 0


def test_complete_coverage_tombstones_an_omitted_child(
    artifact_store: ArtifactStore,
) -> None:
    artifact_store.apply_batch(message_batch())
    claim = CoverageClaim(
        parent=ArtifactReference(
            entity="conversation",
            external_id="conversation-1",
        ),
        entity="message",
        complete=True,
    )
    receipt = artifact_store.apply_batch(
        _batch([], batch_id="batch-2", coverage=[claim])
    )
    assert receipt.tombstones == 1
    assert artifact_store.count("message") == 0


def test_incomplete_coverage_does_not_tombstone(
    artifact_store: ArtifactStore,
) -> None:
    artifact_store.apply_batch(message_batch())
    claim = CoverageClaim(
        parent=ArtifactReference(
            entity="conversation",
            external_id="conversation-1",
        ),
        entity="message",
        complete=False,
    )
    artifact_store.apply_batch(_batch([], batch_id="batch-2", coverage=[claim]))
    assert artifact_store.count("message") == 1


def test_non_system_message_marks_conversation_for_distillation(
    artifact_store: ArtifactStore,
) -> None:
    artifact_store.apply_batch(message_batch())
    state = artifact_store.distillation_status(
        "chat-source", "workspace", "conversation", "conversation-1"
    )
    assert state == "pending"


def test_system_message_does_not_mark_conversation_for_distillation(
    artifact_store: ArtifactStore,
) -> None:
    events = [
        _event(
            entity="conversation",
            external_id="conversation-1",
            text="Conversation",
        ),
        _event(
            entity="message",
            external_id="message-1",
            text="Person joined",
            parent_entity="conversation",
            parent_external_id="conversation-1",
            classification="system",
        ),
    ]
    artifact_store.apply_batch(_batch(events))
    assert (
        artifact_store.distillation_status(
            "chat-source", "workspace", "conversation", "conversation-1"
        )
        is None
    )


def _jsonl(*records: dict[str, object]) -> str:
    return "".join(json.dumps(record) + "\n" for record in records)


def _manifest(event_count: int = 1) -> dict[str, object]:
    return {
        "schema": "ai-memory/artifact-batch@1",
        "record": "batch",
        "batch_id": "batch-1",
        "source": "chat-source",
        "source_instance": "workspace",
        "observed_at": "2026-01-02T12:00:00Z",
        "event_count": event_count,
    }


def _message_record(source_payload: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "schema": "ai-memory/artifact-event@1",
        "record": "event",
        "entity": "message",
        "operation": "upsert",
        "external_id": "message-1",
        "payload": {
            "text": "Neutral text",
            "content_format": "plain",
            "source_payload": source_payload or {},
        },
    }


def test_jsonl_parser_hashes_the_exact_input() -> None:
    raw = _jsonl(_manifest(), _message_record())
    parsed = read_artifact_batch(io.StringIO(raw))
    from hashlib import sha256

    assert parsed.input_sha256 == sha256(raw.encode()).hexdigest()
    assert len(parsed.events) == 1


@pytest.mark.parametrize(
    "raw,match",
    [
        ("not json\n", "JSON"),
        (_jsonl(_message_record()), "manifest"),
        (_jsonl(_manifest(2), _message_record()), "event count"),
        (_jsonl(_manifest(), _manifest()), "manifest"),
    ],
)
def test_jsonl_parser_rejects_invalid_streams(raw: str, match: str) -> None:
    with pytest.raises((ValueError, ValidationError), match=match):
        read_artifact_batch(io.StringIO(raw))


def test_jsonl_parser_rejects_oversized_input() -> None:
    raw = _jsonl(_manifest(), _message_record())
    with pytest.raises(ValueError, match="size"):
        read_artifact_batch(io.StringIO(raw), max_bytes=10)


@pytest.mark.parametrize(
    "payload",
    [
        {"nested": {"access_token": "secret"}},
        {"temporaryDownloadUrl": "https://example.invalid/file"},
        {"url": "https://example.invalid/file?sig=secret"},
    ],
)
def test_jsonl_parser_rejects_secret_material(payload: dict[str, object]) -> None:
    raw = _jsonl(_manifest(), _message_record(payload))
    with pytest.raises(ValueError, match="secret|authentication"):
        read_artifact_batch(io.StringIO(raw))
