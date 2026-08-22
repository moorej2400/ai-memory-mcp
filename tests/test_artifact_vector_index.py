from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import ai_memory_mcp.artifacts.schema as schema_module
from ai_memory_mcp.ann import ANN_BANDS, available as ann_available
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


def test_vector_index_reuses_unchanged_burst_embeddings(
    artifact_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_memory_mcp.embedding import HashedProvider

    provider = HashedProvider(dimensions=artifact_settings.semantic_dimensions)
    original_embed = provider.embed
    calls: list[str] = []

    def counted_embed(text: str) -> dict[int, float]:
        calls.append(text)
        return original_embed(text)

    monkeypatch.setattr(provider, "embed", counted_embed)
    monkeypatch.setattr(
        "ai_memory_mcp.artifacts.vector_index.resolve_provider",
        lambda *_args, **_kwargs: provider,
    )
    _populate(artifact_settings)
    first = build_artifact_vector_index(artifact_settings)
    first_call_count = len(calls)
    assert first_call_count == first.embedded_bursts == 1

    ArtifactStore(artifact_settings).apply_batch(
        _batch(
            "vector-incremental-batch",
            [
                _event(
                    "conversation",
                    "conversation-incremental",
                    "Additional operations",
                    "2026-01-03T10:00:00Z",
                ),
                _event(
                    "message",
                    "message-incremental",
                    "A separate durable procedure needs semantic retrieval. " * 5,
                    "2026-01-03T10:01:00Z",
                    parent=("conversation", "conversation-incremental"),
                ),
            ],
        )
    )
    second = build_artifact_vector_index(artifact_settings)

    assert second.unchanged is False
    assert second.embedded_updates == 1
    assert second.reused_bursts == first.bursts
    assert len(calls) == first_call_count + 1


def test_vector_index_groups_only_dirty_parents_after_the_first_build(
    artifact_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ai_memory_mcp.artifacts.vector_index as vector_index

    _populate(artifact_settings)
    ArtifactStore(artifact_settings).apply_batch(
        _batch(
            "vector-second-parent",
            [
                _event(
                    "conversation",
                    "conversation-2",
                    "Second conversation",
                    "2026-01-02T11:00:00Z",
                ),
                _event(
                    "message",
                    "message-3",
                    "A separate semantic record exists in this conversation. " * 5,
                    "2026-01-02T11:01:00Z",
                    parent=("conversation", "conversation-2"),
                ),
            ],
        )
    )
    build_artifact_vector_index(artifact_settings)
    grouped_parents: list[set[str]] = []
    original_group = vector_index.group_bursts

    def capture(records):
        grouped_parents.append({record.parent_artifact_id for record in records})
        return original_group(records)

    monkeypatch.setattr(vector_index, "group_bursts", capture)
    ArtifactStore(artifact_settings).apply_batch(
        _batch(
            "vector-one-parent-change",
            [
                _event(
                    "message",
                    "message-4",
                    "Only the first conversation receives this semantic update. " * 5,
                    "2026-01-02T10:31:00Z",
                    parent=("conversation", "conversation-1"),
                )
            ],
        )
    )

    build_artifact_vector_index(artifact_settings)

    assert len(grouped_parents) == 1
    assert len(grouped_parents[0]) == 1


@pytest.mark.skipif(not ann_available(), reason="NumPy ANN backend is unavailable")
def test_artifact_backend_transition_rebuilds_all_ann_buckets(
    artifact_settings: Settings,
) -> None:
    _populate(artifact_settings)
    first = build_artifact_vector_index(artifact_settings)
    with sqlite3.connect(first.snapshot) as connection:
        connection.execute(
            "UPDATE metadata SET value = 'exact' WHERE key = 'ann_backend'"
        )
        connection.execute("DELETE FROM burst_ann_buckets")
        connection.commit()
    ArtifactStore(artifact_settings).apply_batch(
        _batch(
            "vector-backend-transition",
            [
                _event(
                    "message",
                    "message-transition",
                    "A second semantic procedure needs complete ANN coverage. " * 5,
                    "2026-01-02T10:02:00Z",
                    parent=("conversation", "conversation-1"),
                )
            ],
        )
    )

    second = build_artifact_vector_index(artifact_settings)
    with sqlite3.connect(second.snapshot) as connection:
        vectors = int(
            connection.execute(
                "SELECT count(*) FROM bursts WHERE vector_blob IS NOT NULL"
            ).fetchone()[0]
        )
        buckets = int(
            connection.execute("SELECT count(*) FROM burst_ann_buckets").fetchone()[0]
        )
    assert buckets == vectors * ANN_BANDS


def test_vector_index_rejects_a_network_filesystem_snapshot(
    artifact_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _populate(artifact_settings)
    state_root = artifact_settings.state_dir.resolve()
    monkeypatch.setattr(
        schema_module,
        "_network_filesystem_type",
        lambda path: "nfs" if path.is_relative_to(state_root) else None,
    )

    with pytest.raises(ValueError, match="network filesystem"):
        build_artifact_vector_index(artifact_settings)


def test_force_build_preserves_previous_snapshot(
    artifact_settings: Settings,
) -> None:
    _populate(artifact_settings)
    first = build_artifact_vector_index(artifact_settings)
    second = build_artifact_vector_index(artifact_settings, force=True)
    assert second.snapshot != first.snapshot
    assert Path(first.snapshot).is_file()
    assert Path(second.snapshot).is_file()


def test_failed_vector_build_leaves_no_final_looking_snapshot(
    artifact_settings: Settings,
    monkeypatch,
) -> None:
    _populate(artifact_settings)

    def fail_insert(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("Synthetic vector build failure.")

    monkeypatch.setattr(
        "ai_memory_mcp.artifacts.vector_index._insert_burst",
        fail_insert,
    )
    with pytest.raises(RuntimeError, match="Synthetic vector build failure"):
        build_artifact_vector_index(artifact_settings)

    assert list(artifact_settings.state_dir.glob("artifact-index-*.sqlite")) == []
    assert current_artifact_index_path(artifact_settings) is None


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


def test_vector_query_does_not_return_a_burst_across_date_boundaries(
    artifact_settings: Settings,
) -> None:
    long_text = "The bounded retrieval procedure uses exact evidence. " * 5
    ArtifactStore(artifact_settings).apply_batch(
        _batch(
            "vector-date-boundary",
            [
                _event(
                    "conversation",
                    "conversation-date-boundary",
                    "Operations",
                    "2026-01-02T09:55:00Z",
                ),
                _event(
                    "message",
                    "message-date-first",
                    long_text,
                    "2026-01-02T10:00:00Z",
                    parent=("conversation", "conversation-date-boundary"),
                ),
                _event(
                    "message",
                    "message-date-second",
                    long_text,
                    "2026-01-02T10:05:00Z",
                    parent=("conversation", "conversation-date-boundary"),
                ),
            ],
        )
    )
    build_artifact_vector_index(artifact_settings)
    before_end = search_artifact_vectors(
        artifact_settings,
        "bounded retrieval exact evidence",
        ArtifactScope(
            source="chat-source",
            entities=("message",),
            date_to=datetime(2026, 1, 2, 10, 2, tzinfo=timezone.utc),
        ),
        limit=10,
    )
    after_start = search_artifact_vectors(
        artifact_settings,
        "bounded retrieval exact evidence",
        ArtifactScope(
            source="chat-source",
            entities=("message",),
            date_from=datetime(2026, 1, 2, 10, 2, tzinfo=timezone.utc),
        ),
        limit=10,
    )
    fully_contained = search_artifact_vectors(
        artifact_settings,
        "bounded retrieval exact evidence",
        ArtifactScope(
            source="chat-source",
            entities=("message",),
            date_from=datetime(2026, 1, 2, 9, 59, tzinfo=timezone.utc),
            date_to=datetime(2026, 1, 2, 10, 6, tzinfo=timezone.utc),
        ),
        limit=10,
    )

    assert before_end.hits == []
    assert after_start.hits == []
    assert fully_contained.hits


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


def test_short_message_with_an_attachment_child_is_embedded(
    artifact_settings: Settings,
) -> None:
    events = [
        _event(
            "conversation",
            "conversation-attachment",
            "Attachment discussion",
            "2026-01-02T10:00:00Z",
        ),
        _event(
            "message",
            "message-attachment",
            "See file.",
            "2026-01-02T10:01:00Z",
            parent=("conversation", "conversation-attachment"),
        ),
        _event(
            "attachment",
            "attachment-1",
            "example.txt",
            "2026-01-02T10:01:00Z",
            parent=("message", "message-attachment"),
        ),
    ]
    ArtifactStore(artifact_settings).apply_batch(
        _batch("attachment-vector-batch", events)
    )
    result = build_artifact_vector_index(artifact_settings)
    assert result.bursts == 1
    assert result.embedded_bursts == 1


def test_sync_preserves_an_exact_external_id_raw_match(
    artifact_settings: Settings,
) -> None:
    ArtifactStore(artifact_settings).apply_batch(
        _batch(
            "exact-id-vector-batch",
            [
                _event(
                    "conversation",
                    "conversation-exact",
                    "Operations",
                    "2026-01-02T10:00:00Z",
                ),
                _event(
                    "message",
                    "deployment-credential",
                    (
                        "The deployment credential requires a neutral review. "
                        * 5
                    ),
                    "2026-01-02T10:01:00Z",
                    parent=("conversation", "conversation-exact"),
                ),
            ],
        )
    )
    service = MemoryService(artifact_settings)
    before = service.recall(
        "deployment-credential",
        source_label="chat-source",
    )
    assert before.status == "answered"
    assert before.evidence[0].evidence_class == "raw"

    sync = service.sync()
    assert sync.artifact_index is not None
    after = service.recall(
        "deployment-credential",
        source_label="chat-source",
    )
    assert after.status == "answered"
    assert after.evidence[0].evidence_class == "raw"
    assert after.evidence[0].reasons == ["exact identifier"]


def test_sync_does_not_publish_a_partial_generation(
    artifact_settings: Settings,
) -> None:
    _populate(artifact_settings)
    settings = replace(
        artifact_settings,
        memory_root=artifact_settings.memory_root.with_name("vault-unavailable"),
    )
    result = MemoryService(settings).sync()
    assert result.ok is False
    assert result.index is None
    assert result.artifact_index is None
    assert result.errors
    assert not settings.generation_pointer_path.exists()
