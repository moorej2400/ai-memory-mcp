from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from ai_memory_mcp.artifacts.models import (
    ArtifactBatchManifest,
    ArtifactEvent,
    ArtifactPayload,
    ArtifactReference,
    ArtifactScope,
    ParsedArtifactBatch,
)
from ai_memory_mcp.artifacts.store import ArtifactStore
from ai_memory_mcp.artifacts.vector_index import (
    build_artifact_vector_index,
    current_artifact_index_path,
    search_artifact_vectors,
)
from ai_memory_mcp.config import Settings
from ai_memory_mcp.service import MemoryService


def _event(
    entity: str,
    external_id: str,
    text: str,
    occurred_at: str,
    *,
    parent: tuple[str, str] | None = None,
) -> ArtifactEvent:
    return ArtifactEvent.model_validate(
        {
            "schema": "ai-memory/artifact-event@1",
            "record": "event",
            "entity": entity,
            "operation": "upsert",
            "external_id": external_id,
            "parent": (
                ArtifactReference(entity=parent[0], external_id=parent[1])
                if parent
                else None
            ),
            "source_updated_at": occurred_at,
            "payload": ArtifactPayload(
                title="Example conversation" if entity == "conversation" else None,
                text=text,
                occurred_at=occurred_at,
                content_format="plain",
                author=(
                    {"id": "actor-a", "name": "Actor A"}
                    if entity == "message"
                    else None
                ),
            ),
        }
    )


def _batch(batch_id: str, events: list[ArtifactEvent]) -> ParsedArtifactBatch:
    return ParsedArtifactBatch(
        manifest=ArtifactBatchManifest.model_validate(
            {
                "schema": "ai-memory/artifact-batch@1",
                "record": "batch",
                "batch_id": batch_id,
                "source": "chat-source",
                "source_instance": "workspace",
                "observed_at": "2026-01-02T12:00:00Z",
                "event_count": len(events),
            }
        ),
        events=events,
        input_sha256=(batch_id * 64)[:64],
    )


def _populate(settings: Settings) -> None:
    long_text = (
        "The deployment credential rotation procedure requires validation. " * 5
    )
    events = [
        _event(
            "conversation",
            "conversation-1",
            "Operations",
            "2026-01-02T10:00:00Z",
        ),
        _event(
            "message",
            "message-1",
            long_text,
            "2026-01-02T10:01:00Z",
            parent=("conversation", "conversation-1"),
        ),
        _event(
            "message",
            "message-2",
            "Thanks.",
            "2026-01-02T10:30:00Z",
            parent=("conversation", "conversation-1"),
        ),
    ]
    ArtifactStore(settings).apply_batch(_batch("vector-batch-1", events))


def test_vector_index_publishes_revisioned_snapshot(
    artifact_settings: Settings,
) -> None:
    _populate(artifact_settings)
    first = build_artifact_vector_index(artifact_settings)
    assert first.bursts == 2
    assert first.embedded_bursts == 1
    assert first.unchanged is False
    snapshot = Path(first.snapshot)
    assert snapshot.is_file()
    assert current_artifact_index_path(artifact_settings) == snapshot
    pointer = json.loads(
        artifact_settings.artifact_pointer_path.read_text(encoding="utf-8")
    )
    assert pointer["snapshot"] == snapshot.name
    with sqlite3.connect(snapshot) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
    assert int(metadata["artifact_change_counter"]) == first.change_counter
    assert metadata["embedding_fingerprint"] == first.embedding_fingerprint

    repeated = build_artifact_vector_index(artifact_settings)
    assert repeated.snapshot == first.snapshot
    assert repeated.unchanged is True


def test_force_build_preserves_previous_snapshot(
    artifact_settings: Settings,
) -> None:
    _populate(artifact_settings)
    first = build_artifact_vector_index(artifact_settings)
    second = build_artifact_vector_index(artifact_settings, force=True)
    assert second.snapshot != first.snapshot
    assert Path(first.snapshot).is_file()
    assert Path(second.snapshot).is_file()


def test_vector_query_is_scoped_and_detects_staleness(
    artifact_settings: Settings,
) -> None:
    _populate(artifact_settings)
    build_artifact_vector_index(artifact_settings)
    result = search_artifact_vectors(
        artifact_settings,
        "credential validation procedure",
        ArtifactScope(source="chat-source", entities=("message",)),
        limit=10,
    )
    assert result.stale is False
    assert result.hits
    assert result.hits[0].evidence_class == "burst"

    newer = _event(
        "message",
        "message-3",
        "A later message.",
        "2026-01-02T10:40:00Z",
        parent=("conversation", "conversation-1"),
    )
    ArtifactStore(artifact_settings).apply_batch(
        _batch("vector-batch-2", [newer])
    )
    stale = search_artifact_vectors(
        artifact_settings,
        "credential validation procedure",
        ArtifactScope(),
        limit=10,
    )
    assert stale.stale is True
    assert stale.hits == []

    recalled = MemoryService(artifact_settings).recall(
        '"The deployment credential rotation procedure requires validation."',
        source_label="chat-source",
    )
    assert recalled.status == "answered"
    assert recalled.evidence[0].evidence_class == "raw"
    assert any("semantic index is stale" in value for value in recalled.warnings)


def test_burst_semantic_hit_uses_the_raw_answer_gate(
    artifact_settings: Settings,
) -> None:
    _populate(artifact_settings)
    build_artifact_vector_index(artifact_settings)
    response = MemoryService(artifact_settings).recall(
        "How should deployment access be checked?",
        source_label="chat-source",
    )
    assert response.status == "no_answer"
    assert response.evidence
    assert response.evidence[0].evidence_class in {"raw", "burst"}
    assert any("Raw artifact" in warning for warning in response.warnings)


def test_sync_reports_artifact_index_independently(
    artifact_settings: Settings,
) -> None:
    _populate(artifact_settings)
    settings = replace(
        artifact_settings,
        memory_root=artifact_settings.memory_root.with_name("vault-unavailable"),
    )
    result = MemoryService(settings).sync()
    assert result.ok is True
    assert result.index is None
    assert result.artifact_index is not None
    assert result.artifact_index.embedded_bursts == 1
    assert result.errors
