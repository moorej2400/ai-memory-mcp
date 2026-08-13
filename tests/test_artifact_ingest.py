from __future__ import annotations

import io
import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_memory_mcp.artifacts.ingest import read_artifact_batch
from ai_memory_mcp.artifacts.identity import artifact_id
from ai_memory_mcp.artifacts.models import (
    ArtifactBatchManifest,
    ArtifactAlias,
    ArtifactEvent,
    ArtifactPayload,
    ArtifactObjectInput,
    ArtifactReference,
    CoverageClaim,
    ParsedArtifactBatch,
    RedactionPayload,
)
from ai_memory_mcp.artifacts.schema import connect_artifact_db
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
    links: list[dict[str, object]] | None = None,
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
            links=links or [],
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


def test_replayed_object_batch_returns_receipt_before_reading_source_again(
    artifact_store: ArtifactStore,
    tmp_path: Path,
) -> None:
    source = tmp_path / "handoff.txt"
    source.write_text("neutral attachment body", encoding="utf-8")
    event = _event(
        entity="attachment",
        external_id="attachment-replay",
        text="Attachment",
        source_updated_at="2026-01-02T10:00:00Z",
    )
    assert isinstance(event.payload, ArtifactPayload)
    event.payload.object = ArtifactObjectInput(local_source_path=source)
    batch = _batch([event], batch_id="batch-object-replay")
    first = artifact_store.apply_batch(batch)
    source.rename(tmp_path / "handoff-moved.txt")

    assert artifact_store.apply_batch(batch) == first


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


def test_artifact_times_are_stored_in_canonical_utc(
    artifact_store: ArtifactStore,
) -> None:
    event = _event(
        entity="message",
        external_id="message-offset",
        text="Offset message",
        source_updated_at="2026-01-02T05:00:00-05:00",
    )
    assert isinstance(event.payload, ArtifactPayload)
    event.payload.occurred_at = datetime.fromisoformat("2026-01-02T05:00:00-05:00")
    artifact_store.apply_batch(_batch([event], batch_id="batch-offset"))
    current = artifact_store.get_by_external_id(
        "chat-source", "workspace", "message", "message-offset"
    )
    assert current.occurred_at == "2026-01-02T10:00:00+00:00"
    assert current.source_updated_at == "2026-01-02T10:00:00+00:00"


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


def test_redaction_suppresses_all_later_upsert_payloads(
    artifact_store: ArtifactStore,
    artifact_settings: Settings,
) -> None:
    marker = "rawcontentmustnotreturn"
    artifact_store.apply_batch(message_batch(text="Initial sensitive text"))
    artifact_store.apply_batch(
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
            batch_id="batch-redact",
        )
    )
    for suffix, source_time in (
        ("stale", "2026-01-02T10:30:00Z"),
        ("equal", "2026-01-02T11:00:00Z"),
        ("newer", "2026-01-02T12:00:00Z"),
    ):
        receipt = artifact_store.apply_batch(
            _batch(
                [
                    _event(
                        entity="message",
                        external_id="message-17",
                        text=f"{marker}-{suffix}",
                        parent_entity="conversation",
                        parent_external_id="conversation-1",
                        source_updated_at=source_time,
                    )
                ],
                batch_id=f"batch-{suffix}",
            )
        )
        assert receipt.redactions == 1

    current = artifact_store.get_by_external_id(
        "chat-source", "workspace", "message", "message-17"
    )
    assert current.redacted_at is not None
    assert current.text_content == ""
    with connect_artifact_db(
        artifact_settings.artifact_db,
        read_only=True,
    ) as connection:
        later_payloads = connection.execute(
            "SELECT payload_json FROM artifact_events "
            "WHERE artifact_id = ? AND operation = 'upsert'",
            (current.artifact_id,),
        ).fetchall()
        searchable = connection.execute(
            "SELECT count(*) FROM artifacts_fts WHERE artifacts_fts MATCH ?",
            (marker,),
        ).fetchone()[0]
    assert all(row[0] is None for row in later_payloads)
    assert searchable == 0


def test_parent_only_correction_updates_the_current_parent(
    artifact_store: ArtifactStore,
) -> None:
    artifact_store.apply_batch(
        _batch(
            [
                _event(
                    entity="conversation",
                    external_id="conversation-1",
                    text="First conversation",
                ),
                _event(
                    entity="conversation",
                    external_id="conversation-2",
                    text="Second conversation",
                ),
                _event(
                    entity="message",
                    external_id="message-parent-fix",
                    text="Stable text",
                    parent_entity="conversation",
                    parent_external_id="conversation-1",
                    source_updated_at="2026-01-02T10:00:00Z",
                ),
            ]
        )
    )
    receipt = artifact_store.apply_batch(
        _batch(
            [
                _event(
                    entity="message",
                    external_id="message-parent-fix",
                    text="Stable text",
                    parent_entity="conversation",
                    parent_external_id="conversation-2",
                    source_updated_at="2026-01-02T10:00:00Z",
                )
            ],
            batch_id="batch-parent-fix",
        )
    )
    current = artifact_store.get_by_external_id(
        "chat-source", "workspace", "message", "message-parent-fix"
    )
    assert receipt.accepted == 1
    assert current.parent_artifact_id == artifact_id(
        "chat-source", "workspace", "conversation", "conversation-2"
    )
    assert artifact_store.event_count(current.artifact_id) == 2


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


def test_coverage_tombstone_adds_revision_evidence_and_clears_relations(
    artifact_store: ArtifactStore,
    artifact_settings: Settings,
    tmp_path: Path,
) -> None:
    source = tmp_path / "message-object.txt"
    source.write_text("neutral attachment body", encoding="utf-8")
    conversation = _event(
        entity="conversation",
        external_id="conversation-1",
        text="Conversation",
    )
    attachment = _event(
        entity="attachment",
        external_id="attachment-1",
        text="Attachment",
        parent_entity="conversation",
        parent_external_id="conversation-1",
    )
    message = _event(
        entity="message",
        external_id="message-covered",
        text="Covered message",
        parent_entity="conversation",
        parent_external_id="conversation-1",
        links=[
            {
                "relation": "contains",
                "target": {
                    "entity": "attachment",
                    "external_id": "attachment-1",
                },
            }
        ],
    )
    assert isinstance(message.payload, ArtifactPayload)
    message.payload.aliases = [ArtifactAlias(kind="message", value="alias-1")]
    message.payload.object = ArtifactObjectInput(local_source_path=source)
    artifact_store.apply_batch(_batch([conversation, attachment, message]))
    message_id = artifact_id(
        "chat-source", "workspace", "message", "message-covered"
    )
    claim = CoverageClaim(
        parent=ArtifactReference(
            entity="conversation",
            external_id="conversation-1",
        ),
        entity="message",
        complete=True,
    )
    artifact_store.apply_batch(
        _batch([], batch_id="batch-coverage-delete", coverage=[claim])
    )

    with connect_artifact_db(
        artifact_settings.artifact_db,
        read_only=True,
    ) as connection:
        current = connection.execute(
            "SELECT last_event_id, deleted_at FROM artifacts WHERE artifact_id = ?",
            (message_id,),
        ).fetchone()
        events = connection.execute(
            "SELECT event_id, operation, payload_json FROM artifact_events "
            "WHERE artifact_id = ? ORDER BY rowid",
            (message_id,),
        ).fetchall()
        relation_counts = {
            "aliases": connection.execute(
                "SELECT count(*) FROM artifact_aliases WHERE artifact_id = ?",
                (message_id,),
            ).fetchone()[0],
            "links": connection.execute(
                "SELECT count(*) FROM artifact_links WHERE source_artifact_id = ?",
                (message_id,),
            ).fetchone()[0],
            "objects": connection.execute(
                "SELECT count(*) FROM artifact_object_links WHERE artifact_id = ?",
                (message_id,),
            ).fetchone()[0],
        }
    assert current["deleted_at"] is not None
    assert [row["operation"] for row in events] == ["upsert", "delete"]
    assert events[-1]["payload_json"] is None
    assert current["last_event_id"] == events[-1]["event_id"]
    assert relation_counts == {"aliases": 0, "links": 0, "objects": 0}


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


@pytest.mark.parametrize("operation", ["delete", "redact"])
def test_system_message_removal_does_not_trigger_distillation(
    artifact_store: ArtifactStore,
    operation: str,
) -> None:
    artifact_store.apply_batch(
        _batch(
            [
                _event(
                    entity="conversation",
                    external_id="conversation-system",
                    text="Conversation",
                ),
                _event(
                    entity="message",
                    external_id="message-system",
                    text="Person joined",
                    parent_entity="conversation",
                    parent_external_id="conversation-system",
                    classification="system",
                    source_updated_at="2026-01-02T10:00:00Z",
                ),
            ],
            batch_id="batch-system",
        )
    )
    artifact_store.apply_batch(
        _batch(
            [
                _event(
                    entity="message",
                    external_id="message-system",
                    parent_entity="conversation",
                    parent_external_id="conversation-system",
                    operation=operation,
                    source_updated_at="2026-01-02T11:00:00Z",
                )
            ],
            batch_id=f"batch-system-{operation}",
        )
    )
    assert artifact_store.distillation_status(
        "chat-source", "workspace", "conversation", "conversation-system"
    ) is None


def test_system_message_coverage_tombstone_does_not_trigger_distillation(
    artifact_store: ArtifactStore,
) -> None:
    artifact_store.apply_batch(
        _batch(
            [
                _event(
                    entity="conversation",
                    external_id="conversation-system",
                    text="Conversation",
                ),
                _event(
                    entity="message",
                    external_id="message-system",
                    text="Person joined",
                    parent_entity="conversation",
                    parent_external_id="conversation-system",
                    classification="system",
                ),
            ],
            batch_id="batch-system",
        )
    )
    claim = CoverageClaim(
        parent=ArtifactReference(
            entity="conversation",
            external_id="conversation-system",
        ),
        entity="message",
        complete=True,
    )
    artifact_store.apply_batch(
        _batch([], batch_id="batch-system-coverage", coverage=[claim])
    )
    assert artifact_store.distillation_status(
        "chat-source", "workspace", "conversation", "conversation-system"
    ) is None


def test_related_chat_message_reopens_meeting_and_changes_its_digest(
    artifact_store: ArtifactStore,
    artifact_settings: Settings,
) -> None:
    artifact_store.apply_batch(
        _batch(
            [
                _event(
                    entity="conversation",
                    external_id="conversation-1",
                    text="Meeting chat",
                ),
                _event(
                    entity="meeting",
                    external_id="meeting-1",
                    text="Review meeting",
                    parent_entity="conversation",
                    parent_external_id="conversation-1",
                    links=[
                        {
                            "relation": "related-chat",
                            "target": {
                                "entity": "conversation",
                                "external_id": "conversation-1",
                            },
                        }
                    ],
                ),
                _event(
                    entity="message",
                    external_id="message-1",
                    text="Use the first release check.",
                    parent_entity="conversation",
                    parent_external_id="conversation-1",
                    source_updated_at="2026-01-02T10:00:00Z",
                ),
            ]
        )
    )
    meeting_id = artifact_id(
        "chat-source",
        "workspace",
        "meeting",
        "meeting-1",
    )
    empty_digest = hashlib.sha256().hexdigest()
    with connect_artifact_db(artifact_settings.artifact_db) as connection:
        initial = connection.execute(
            "SELECT latest_source_digest FROM distillation_state "
            "WHERE artifact_id = ?",
            (meeting_id,),
        ).fetchone()
        assert initial is not None
        assert initial[0] != empty_digest
        connection.execute(
            "UPDATE distillation_state SET status = 'distilled' "
            "WHERE artifact_id = ?",
            (meeting_id,),
        )

    artifact_store.apply_batch(
        _batch(
            [
                _event(
                    entity="message",
                    external_id="message-2",
                    text="Use the second release check.",
                    parent_entity="conversation",
                    parent_external_id="conversation-1",
                    source_updated_at="2026-01-02T11:00:00Z",
                )
            ],
            batch_id="batch-2",
        )
    )
    with connect_artifact_db(
        artifact_settings.artifact_db,
        read_only=True,
    ) as connection:
        changed = connection.execute(
            "SELECT status, latest_source_digest FROM distillation_state "
            "WHERE artifact_id = ?",
            (meeting_id,),
        ).fetchone()
    assert changed is not None
    assert changed["status"] == "pending"
    assert changed["latest_source_digest"] != initial[0]


def test_meeting_digest_excludes_sibling_meeting_descendants(
    artifact_store: ArtifactStore,
    artifact_settings: Settings,
) -> None:
    related = [
        {
            "relation": "related-chat",
            "target": {
                "entity": "conversation",
                "external_id": "conversation-shared",
            },
        }
    ]
    artifact_store.apply_batch(
        _batch(
            [
                _event(
                    entity="conversation",
                    external_id="conversation-shared",
                    text="Shared chat",
                ),
                _event(
                    entity="meeting",
                    external_id="meeting-one",
                    text="Meeting one",
                    parent_entity="conversation",
                    parent_external_id="conversation-shared",
                    links=related,
                ),
                _event(
                    entity="transcript",
                    external_id="transcript-one",
                    text="Transcript one",
                    parent_entity="meeting",
                    parent_external_id="meeting-one",
                ),
                _event(
                    entity="transcript-cue",
                    external_id="cue-one",
                    text="Cue one",
                    parent_entity="transcript",
                    parent_external_id="transcript-one",
                    source_updated_at="2026-01-02T10:00:00Z",
                ),
                _event(
                    entity="meeting",
                    external_id="meeting-two",
                    text="Meeting two",
                    parent_entity="conversation",
                    parent_external_id="conversation-shared",
                    links=related,
                ),
                _event(
                    entity="transcript",
                    external_id="transcript-two",
                    text="Transcript two",
                    parent_entity="meeting",
                    parent_external_id="meeting-two",
                ),
                _event(
                    entity="transcript-cue",
                    external_id="cue-two",
                    text="Cue two",
                    parent_entity="transcript",
                    parent_external_id="transcript-two",
                    source_updated_at="2026-01-02T10:00:00Z",
                ),
            ],
            batch_id="batch-shared-meetings",
        )
    )
    meeting_one = artifact_id(
        "chat-source", "workspace", "meeting", "meeting-one"
    )
    with connect_artifact_db(
        artifact_settings.artifact_db,
        read_only=True,
    ) as connection:
        before = ArtifactStore._source_digest(connection, meeting_one)

    artifact_store.apply_batch(
        _batch(
            [
                _event(
                    entity="transcript-cue",
                    external_id="cue-two",
                    text="Changed cue two",
                    parent_entity="transcript",
                    parent_external_id="transcript-two",
                    source_updated_at="2026-01-02T11:00:00Z",
                )
            ],
            batch_id="batch-change-sibling",
        )
    )
    with connect_artifact_db(
        artifact_settings.artifact_db,
        read_only=True,
    ) as connection:
        after = ArtifactStore._source_digest(connection, meeting_one)
    assert after == before


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
