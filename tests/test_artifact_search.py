from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_memory_mcp.artifacts.identity import artifact_id, artifact_uri
from ai_memory_mcp.artifacts.models import (
    ArtifactBatchManifest,
    ArtifactEvent,
    ArtifactLink,
    ArtifactPayload,
    ArtifactReference,
    ArtifactScope,
    ParsedArtifactBatch,
)
from ai_memory_mcp.artifacts.search import ArtifactSearch
from ai_memory_mcp.artifacts.schema import connect_artifact_db
from ai_memory_mcp.artifacts.store import ArtifactStore
from ai_memory_mcp.config import Settings
from ai_memory_mcp.text import fts_expression


def _event(
    entity: str,
    external_id: str,
    text: str,
    occurred_at: str,
    *,
    parent: tuple[str, str] | None = None,
    links: list[ArtifactLink] | None = None,
) -> ArtifactEvent:
    parent_reference = (
        ArtifactReference(entity=parent[0], external_id=parent[1])
        if parent
        else None
    )
    return ArtifactEvent.model_validate(
        {
            "schema": "ai-memory/artifact-event@1",
            "record": "event",
            "entity": entity,
            "operation": "upsert",
            "external_id": external_id,
            "parent": parent_reference,
            "source_updated_at": occurred_at,
            "payload": ArtifactPayload(
                text=text,
                content_format="plain",
                occurred_at=occurred_at,
                links=links or [],
                source_payload={"provider_field": f"raw-{external_id}"},
            ),
        }
    )


def _batch(
    source: str,
    source_instance: str,
    batch_id: str,
    events: list[ArtifactEvent],
) -> ParsedArtifactBatch:
    return ParsedArtifactBatch(
        manifest=ArtifactBatchManifest.model_validate(
            {
                "schema": "ai-memory/artifact-batch@1",
                "record": "batch",
                "batch_id": batch_id,
                "source": source,
                "source_instance": source_instance,
                "observed_at": "2026-01-02T12:00:00Z",
                "event_count": len(events),
            }
        ),
        events=events,
        input_sha256=(batch_id * 64)[:64],
    )


@pytest.fixture
def populated_artifact_store(artifact_settings: Settings) -> ArtifactStore:
    store = ArtifactStore(artifact_settings)
    events = [
        _event(
            "conversation",
            "conversation-1",
            "Operations discussion",
            "2026-01-02T09:00:00Z",
        ),
        _event(
            "message",
            "message-1",
            "Review the rotation schedule.",
            "2026-01-02T10:00:00Z",
            parent=("conversation", "conversation-1"),
        ),
        _event(
            "message",
            "message-2",
            "Use the documented rotation procedure.",
            "2026-01-02T10:05:00Z",
            parent=("conversation", "conversation-1"),
        ),
        _event(
            "message",
            "message-3",
            "Confirm the result after rotation.",
            "2026-01-02T10:10:00Z",
            parent=("conversation", "conversation-1"),
        ),
    ]
    store.apply_batch(_batch("chat-source", "workspace", "search-1", events))
    other = [
        _event(
            "conversation",
            "conversation-1",
            "Other discussion",
            "2026-01-02T09:00:00Z",
        ),
        _event(
            "message",
            "message-1",
            "Use an undocumented rotation procedure.",
            "2026-01-02T10:05:00Z",
            parent=("conversation", "conversation-1"),
        ),
    ]
    store.apply_batch(_batch("other-source", "workspace", "search-2", other))
    return store


def test_raw_search_filters_before_ranking(
    populated_artifact_store: ArtifactStore,
) -> None:
    search = ArtifactSearch(populated_artifact_store.settings)
    hits = search.search(
        "documented procedure",
        ArtifactScope(
            source="chat-source",
            source_instance="workspace",
            entities=("message",),
        ),
        limit=10,
    )
    assert [hit.text for hit in hits] == [
        "Use the documented rotation procedure."
    ]
    assert all(hit.evidence_class == "raw" for hit in hits)
    assert all(hit.artifact_uri.startswith("artifact://message/") for hit in hits)


def test_raw_search_supports_parent_and_date_scope(
    populated_artifact_store: ArtifactStore,
) -> None:
    search = ArtifactSearch(populated_artifact_store.settings)
    parent = artifact_uri(
        "conversation",
        artifact_id(
            "chat-source",
            "workspace",
            "conversation",
            "conversation-1",
        ),
    )
    hits = search.search(
        "rotation",
        ArtifactScope(
            parent=parent,
            date_from="2026-01-02T10:04:00Z",
            date_to="2026-01-02T10:06:00Z",
        ),
        limit=10,
    )
    assert [hit.text for hit in hits] == [
        "Use the documented rotation procedure."
    ]


def test_read_message_returns_ordered_context(
    populated_artifact_store: ArtifactStore,
) -> None:
    search = ArtifactSearch(populated_artifact_store.settings)
    focus = artifact_uri(
        "message",
        artifact_id("chat-source", "workspace", "message", "message-2"),
    )
    result = search.read(focus, direction="around", limit=3)
    assert [record.occurred_at for record in result.records] == sorted(
        record.occurred_at for record in result.records
    )
    assert result.records[1].reference == result.focus == focus


def test_read_conversation_returns_active_message_children(
    populated_artifact_store: ArtifactStore,
) -> None:
    search = ArtifactSearch(populated_artifact_store.settings)
    reference = artifact_uri(
        "conversation",
        artifact_id(
            "chat-source",
            "workspace",
            "conversation",
            "conversation-1",
        ),
    )
    result = search.read(reference, direction="after", limit=2)
    assert [record.text for record in result.records] == [
        "Review the rotation schedule.",
        "Use the documented rotation procedure.",
    ]
    assert result.next_cursor is not None

    next_page = search.read(
        reference,
        cursor=result.next_cursor,
        direction="after",
        limit=2,
    )
    assert [record.text for record in next_page.records] == [
        "Confirm the result after rotation."
    ]


def test_include_payload_returns_only_the_exact_focus(
    populated_artifact_store: ArtifactStore,
) -> None:
    search = ArtifactSearch(populated_artifact_store.settings)
    focus = artifact_uri(
        "message",
        artifact_id("chat-source", "workspace", "message", "message-2"),
    )
    result = search.read(
        focus,
        direction="around",
        limit=10,
        include_payload=True,
    )
    assert len(result.records) == 1
    assert result.records[0].reference == focus
    assert result.records[0].payload is not None
    assert result.records[0].payload["source_payload"]["provider_field"]


def test_meeting_read_traverses_contains_and_related_chat_links(
    artifact_settings: Settings,
) -> None:
    contains = ArtifactLink(
        relation="contains",
        target=ArtifactReference(entity="transcript", external_id="transcript-1"),
    )
    related = ArtifactLink(
        relation="related-chat",
        target=ArtifactReference(
            entity="conversation",
            external_id="conversation-1",
        ),
    )
    events = [
        _event(
            "meeting",
            "meeting-1",
            "Weekly review",
            "2026-01-02T09:00:00Z",
            links=[contains, related],
        ),
        _event(
            "transcript",
            "transcript-1",
            "Transcript",
            "2026-01-02T09:00:00Z",
            parent=("meeting", "meeting-1"),
        ),
        _event(
            "transcript-cue",
            "cue-1",
            "Decision from the meeting.",
            "2026-01-02T09:15:00Z",
            parent=("transcript", "transcript-1"),
        ),
        _event(
            "conversation",
            "conversation-1",
            "Related conversation",
            "2026-01-02T09:00:00Z",
        ),
        _event(
            "message",
            "message-1",
            "Follow-up from the meeting.",
            "2026-01-02T09:20:00Z",
            parent=("conversation", "conversation-1"),
        ),
    ]
    store = ArtifactStore(artifact_settings)
    store.apply_batch(_batch("chat-source", "workspace", "meeting-1", events))
    reference = artifact_uri(
        "meeting",
        artifact_id("chat-source", "workspace", "meeting", "meeting-1"),
    )
    result = ArtifactSearch(artifact_settings).read(
        reference,
        direction="after",
        limit=10,
    )
    assert {record.text for record in result.records} >= {
        "Transcript",
        "Decision from the meeting.",
        "Related conversation",
        "Follow-up from the meeting.",
    }


def test_meeting_read_excludes_sibling_meetings_and_their_descendants(
    artifact_settings: Settings,
) -> None:
    related = ArtifactLink(
        relation="related-chat",
        target=ArtifactReference(
            entity="conversation",
            external_id="conversation-shared",
        ),
    )
    events = [
        _event(
            "conversation",
            "conversation-shared",
            "Shared conversation",
            "2026-01-02T09:00:00Z",
        ),
        _event(
            "message",
            "message-shared",
            "Shared follow-up",
            "2026-01-02T09:05:00Z",
            parent=("conversation", "conversation-shared"),
        ),
        _event(
            "meeting",
            "meeting-one",
            "Meeting one",
            "2026-01-02T10:00:00Z",
            parent=("conversation", "conversation-shared"),
            links=[related],
        ),
        _event(
            "transcript",
            "transcript-one",
            "Transcript one",
            "2026-01-02T10:00:00Z",
            parent=("meeting", "meeting-one"),
        ),
        _event(
            "transcript-cue",
            "cue-one",
            "Decision for meeting one",
            "2026-01-02T10:10:00Z",
            parent=("transcript", "transcript-one"),
        ),
        _event(
            "meeting",
            "meeting-two",
            "Meeting two",
            "2026-01-03T10:00:00Z",
            parent=("conversation", "conversation-shared"),
            links=[related],
        ),
        _event(
            "transcript",
            "transcript-two",
            "Transcript two",
            "2026-01-03T10:00:00Z",
            parent=("meeting", "meeting-two"),
        ),
        _event(
            "transcript-cue",
            "cue-two",
            "Decision for meeting two",
            "2026-01-03T10:10:00Z",
            parent=("transcript", "transcript-two"),
        ),
    ]
    ArtifactStore(artifact_settings).apply_batch(
        _batch("chat-source", "workspace", "shared-meetings", events)
    )
    reference = artifact_uri(
        "meeting",
        artifact_id("chat-source", "workspace", "meeting", "meeting-one"),
    )
    texts = {
        record.text
        for record in ArtifactSearch(artifact_settings)
        .read(reference, direction="after", limit=20)
        .records
    }
    assert {
        "Shared conversation",
        "Shared follow-up",
        "Transcript one",
        "Decision for meeting one",
    } <= texts
    assert texts.isdisjoint(
        {"Meeting two", "Transcript two", "Decision for meeting two"}
    )


def test_meeting_read_does_not_traverse_an_inactive_related_chat(
    artifact_settings: Settings,
) -> None:
    related = ArtifactLink(
        relation="related-chat",
        target=ArtifactReference(
            entity="conversation",
            external_id="conversation-inactive",
        ),
    )
    events = [
        _event(
            "conversation",
            "conversation-inactive",
            "Inactive conversation",
            "2026-01-02T09:00:00Z",
        ),
        _event(
            "message",
            "message-inactive",
            "Message under inactive conversation",
            "2026-01-02T09:05:00Z",
            parent=("conversation", "conversation-inactive"),
        ),
        _event(
            "meeting",
            "meeting-inactive-chat",
            "Meeting",
            "2026-01-02T10:00:00Z",
            links=[related],
        ),
    ]
    store = ArtifactStore(artifact_settings)
    store.apply_batch(_batch("chat-source", "workspace", "inactive-base", events))
    delete = ArtifactEvent.model_validate(
        {
            "schema": "ai-memory/artifact-event@1",
            "record": "event",
            "entity": "conversation",
            "operation": "delete",
            "external_id": "conversation-inactive",
            "source_updated_at": "2026-01-02T11:00:00Z",
        }
    )
    store.apply_batch(
        _batch("chat-source", "workspace", "inactive-delete", [delete])
    )
    reference = artifact_uri(
        "meeting",
        artifact_id(
            "chat-source", "workspace", "meeting", "meeting-inactive-chat"
        ),
    )
    result = ArtifactSearch(artifact_settings).read(
        reference,
        direction="after",
        limit=20,
    )
    assert result.records == []


def test_search_rejects_an_old_schema_without_migrating_it(
    artifact_settings: Settings,
    tmp_path: Path,
) -> None:
    old_db = tmp_path / "old-artifacts.sqlite3"
    with sqlite3.connect(old_db) as connection:
        connection.executescript(
            """
            CREATE TABLE artifact_schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL,
                application_version TEXT NOT NULL
            );
            CREATE TABLE sentinel(value TEXT NOT NULL);
            INSERT INTO sentinel VALUES ('unchanged');
            """
        )
    settings = replace(artifact_settings, artifact_db=old_db)
    before = old_db.read_bytes()

    with pytest.raises(RuntimeError, match="schema|migrate"):
        ArtifactSearch(settings)

    assert old_db.read_bytes() == before
    with connect_artifact_db(old_db, read_only=True) as connection:
        assert connection.execute("SELECT value FROM sentinel").fetchone()[0] == (
            "unchanged"
        )


def test_fts_expression_quotes_unique_tokens_and_limits_count() -> None:
    expression = fts_expression(" ".join(["one", "one", *map(str, range(30))]))
    assert expression.count(" OR ") == 23
    assert expression.startswith('"one" OR "0"')
