from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import ai_memory_mcp.artifacts.schema as schema_module
from ai_memory_mcp.ann import available as ann_available
import ai_memory_mcp.artifacts.vector_index as vector_index_module
from ai_memory_mcp.artifacts.models import (
    ArtifactBatchManifest,
    ArtifactEvent,
    ArtifactPayload,
    ArtifactReference,
    ArtifactScope,
    ParsedArtifactBatch,
)
from ai_memory_mcp.artifacts.store import ArtifactStore
from ai_memory_mcp.artifacts.identity import artifact_id
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
    assert first.embedded_bursts == 2
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


def test_vector_candidate_scans_use_compact_index_for_each_scope(artifact_settings):
    _populate(artifact_settings)
    first = build_artifact_vector_index(artifact_settings)
    receipt = ArtifactStore(artifact_settings).apply_batch(_batch("compact-scan-delta", [
        _event("message", "message-1", "Updated validation instructions.",
               "2026-01-02T10:02:00Z", parent=("conversation", "conversation-1")),
    ]))
    assert receipt.accepted == 1
    second = build_artifact_vector_index(artifact_settings)
    assert not second.unchanged
    assert len(vector_index_module.artifact_segment_paths(Path(second.snapshot))) == 2
    for snapshot in (first.snapshot, second.snapshot):
        with vector_index_module._connect(Path(snapshot), read_only=True) as connection:
            table_roots = {
                (database[0], connection.execute(
                    f"SELECT rootpage FROM {database[1]}.sqlite_master WHERE name = 'bursts'"
                ).fetchone()[0])
                for database in connection.execute("PRAGMA database_list").fetchall()
                if database[1].startswith("s")
            }
            for extra, params in (
                ("", ()),
                (" AND b.source = ? AND b.entity = ?", ("chat-source", "message")),
                (" AND b.meeting_artifact_uri IS NOT NULL AND b.meeting_occurred_at >= ?", ("2026-01-01",)),
                (" AND b.parent_artifact_id = ? AND b.started_at >= ? AND b.ended_at <= ?", ("parent", "2026-01-01", "2026-12-31")),
            ):
                for projection in ("count(*)", "b.burst_id, b.ann_vector"):
                    sql = "SELECT " + projection + " FROM vector_candidates b WHERE 1 = 1" + extra
                    plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql, params)]
                    assert any("bursts_ann_scan_idx" in step for step in plan), plan
                    bytecode = connection.execute("EXPLAIN " + sql, params).fetchall()
                    table_cursors = {row[2] for row in bytecode if row[1] == "OpenRead" and (row[4], row[3]) in table_roots}
                    # An outer coroutine scan is safe. A Column instruction on a
                    # base-table cursor proves the compact scan is not covered.
                    assert not any(row[1] == "Column" and row[2] in table_cursors for row in bytecode)
                    full_sql = "SELECT " + projection + " FROM bursts b WHERE b.vector_blob IS NOT NULL" + extra
                    assert sorted(tuple(row) for row in connection.execute(sql, params)) == sorted(
                        tuple(row) for row in connection.execute(full_sql, params)
                    )


def test_old_vector_schema_rebuilds_the_compact_index(artifact_settings):
    _populate(artifact_settings)
    first = build_artifact_vector_index(artifact_settings)
    with sqlite3.connect(first.snapshot) as connection:
        connection.execute("UPDATE metadata SET value = '6' WHERE key = 'schema_version'")
    second = build_artifact_vector_index(artifact_settings)
    assert not second.unchanged
    assert second.snapshot != first.snapshot
    for path in vector_index_module.artifact_segment_paths(Path(second.snapshot)):
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'bursts_ann_scan_idx'").fetchone()


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
    assert first_call_count == first.embedded_bursts == 2

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
    with sqlite3.connect(second.snapshot) as connection:
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
    assert metadata["publication_mode"] == "immutable-delta"
    assert metadata["segment_fanout"] == "2"


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
    original_group = vector_index.build_representations

    def capture(records, **kwargs):
        grouped_parents.append({record.parent_artifact_id for record in records})
        return original_group(records, **kwargs)

    monkeypatch.setattr(vector_index, "build_representations", capture)
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


@pytest.mark.parametrize("position_key", [None, "ordinal", "message_index"])
def test_immutable_deltas_match_full_rebuild_after_edit_append_and_delete(artifact_settings, position_key):
    store = ArtifactStore(artifact_settings)
    def message(index):
        event = _event("message", f"record-{index}", f"Context record {index} contains unique detail.",
                      f"2026-01-02T10:00:{index:02d}Z", parent=("conversation", "context-parent"))
        if position_key:
            event = event.model_copy(update={"payload": event.payload.model_copy(update={
                "source_payload": {position_key: index},
                "occurred_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
            })})
        return event
    store.apply_batch(_batch("context-initial", [
        _event("conversation", "context-parent", "Context history", "2026-01-02T10:00:00Z"),
        *(message(index) for index in range(20)),
    ]))
    first = build_artifact_vector_index(artifact_settings)
    initial_segment = vector_index_module.artifact_segment_paths(Path(first.snapshot))[0]
    initial_bytes = initial_segment.read_bytes()
    edited = message(10).model_copy(update={
        "source_updated_at": datetime(2027, 1, 1, tzinfo=timezone.utc),
        "payload": message(10).payload.model_copy(update={"text": "A corrected detail about orchard links."}),
    })
    deleted = message(9).model_copy(update={
        "operation": "delete", "payload": None,
        "source_updated_at": datetime(2027, 1, 2, tzinfo=timezone.utc),
    })
    for number, event in enumerate((edited, message(20), deleted)):
        store.apply_batch(_batch(f"context-update-{number}", [event]))
        incremental = build_artifact_vector_index(artifact_settings)
        full = build_artifact_vector_index(artifact_settings, force=True, publish_pointer=False)
        with vector_index_module._connect(Path(incremental.snapshot), read_only=True) as inc, \
                vector_index_module._connect(Path(full.snapshot), read_only=True) as rebuilt:
            for table in ("bursts", "representation_dependencies", "representation_coverage"):
                assert sorted(map(tuple, inc.execute(f"SELECT * FROM {table}"))) == sorted(
                    map(tuple, rebuilt.execute(f"SELECT * FROM {table}"))
                )
        assert initial_segment.read_bytes() == initial_bytes
        assert Path(incremental.snapshot).stat().st_size < initial_segment.stat().st_size
    segments = vector_index_module.artifact_segment_paths(Path(incremental.snapshot))
    assert len(segments) == 4
    compacted = vector_index_module._compact_segments(artifact_settings, Path(incremental.snapshot))
    assert len(compacted) == 1
    store.apply_batch(_batch("context-after-compaction", [message(21)]))
    after = build_artifact_vector_index(artifact_settings)
    assert vector_index_module.artifact_segment_paths(Path(after.snapshot))[0] == compacted[0]
    assert len(vector_index_module.artifact_segment_paths(Path(after.snapshot))) == 2
    full = build_artifact_vector_index(artifact_settings, force=True, publish_pointer=False)
    with vector_index_module._connect(Path(after.snapshot), read_only=True) as inc, \
            vector_index_module._connect(Path(full.snapshot), read_only=True) as rebuilt:
        assert sorted(map(tuple, inc.execute("SELECT * FROM bursts"))) == sorted(
            map(tuple, rebuilt.execute("SELECT * FROM bursts"))
        )


@pytest.mark.parametrize("record_count", [100, 1000])
def test_neighbor_lookup_does_not_scan_the_parent_history(artifact_settings, record_count):
    from ai_memory_mcp.artifacts.schema import connect_artifact_db
    store = ArtifactStore(artifact_settings)
    events = [_event("conversation", "bounded-parent", "Bounded history", "2026-01-01T00:00:00Z")]
    for index in range(record_count):
        event = _event("message", f"bounded-{index}", "Ordered source text.", "2026-01-01T00:00:00Z",
                       parent=("conversation", "bounded-parent"))
        events.append(event.model_copy(update={"payload": event.payload.model_copy(update={
            "source_payload": {"ordinal": index},
        })}))
    store.apply_batch(_batch("bounded-parent-batch", events))
    anchor = artifact_id("chat-source", "workspace", "message", f"bounded-{record_count - 1}")
    steps = 0
    def count():
        nonlocal steps
        steps += 1
        return 0
    with connect_artifact_db(artifact_settings.artifact_db, read_only=True) as connection:
        connection.set_progress_handler(count, 1)
        neighbors = vector_index_module._context_neighbor_ids(connection, {anchor})
    assert len(neighbors) == 2
    # Include SQLite's initial schema work. A parent scan used thousands of
    # operations even at 100 rows; indexed lookups stay below this same bound.
    assert steps < 1000


def test_vector_append_reads_only_bounded_neighbor_records(
    artifact_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation = _event(
        "conversation",
        "conversation-large",
        "Large conversation",
        "2026-01-02T10:00:00Z",
    )
    messages = [
        _event(
            "message",
            f"message-{index:03d}",
            f"Bounded history record {index} contains reusable context.",
            f"2026-01-02T10:{index // 60:02d}:{index % 60:02d}Z",
            parent=("conversation", "conversation-large"),
        )
        for index in range(100)
    ]
    ArtifactStore(artifact_settings).apply_batch(
        _batch("large-conversation", [conversation, *messages])
    )
    build_artifact_vector_index(artifact_settings)

    observed: list[tuple[int, int | None]] = []
    original = vector_index_module.build_representations

    def capture(records, *, anchor_artifact_uris=None):
        observed.append(
            (
                len(records),
                len(anchor_artifact_uris)
                if anchor_artifact_uris is not None
                else None,
            )
        )
        return original(records, anchor_artifact_uris=anchor_artifact_uris)

    monkeypatch.setattr(vector_index_module, "build_representations", capture)
    ArtifactStore(artifact_settings).apply_batch(
        _batch(
            "large-conversation-append",
            [
                _event(
                    "message",
                    "message-100",
                    "The appended violet boundary marker remains searchable.",
                    "2026-01-02T10:01:40Z",
                    parent=("conversation", "conversation-large"),
                )
            ],
        )
    )

    build_artifact_vector_index(artifact_settings)

    assert observed
    loaded_records, rebuilt_anchors = observed[0]
    assert loaded_records <= 5
    assert rebuilt_anchors is not None
    assert rebuilt_anchors <= 5


def test_interrupted_pointer_publication_keeps_previous_snapshot(
    artifact_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _populate(artifact_settings)
    first = build_artifact_vector_index(artifact_settings)
    ArtifactStore(artifact_settings).apply_batch(
        _batch(
            "interrupted-publication",
            [
                _event(
                    "message",
                    "message-interrupted",
                    "This update must not replace the prior pointer after failure.",
                    "2026-01-02T10:31:00Z",
                    parent=("conversation", "conversation-1"),
                )
            ],
        )
    )

    def fail_pointer(*_args, **_kwargs):
        raise OSError("synthetic pointer failure")

    monkeypatch.setattr(vector_index_module, "_publish_pointer", fail_pointer)
    with pytest.raises(OSError, match="synthetic pointer failure"):
        build_artifact_vector_index(artifact_settings)

    assert current_artifact_index_path(artifact_settings) == Path(first.snapshot)


@pytest.mark.skipif(not ann_available(), reason="NumPy ANN backend is unavailable")
def test_artifact_backend_transition_rebuilds_all_ann_signatures(
    artifact_settings: Settings,
) -> None:
    _populate(artifact_settings)
    first = build_artifact_vector_index(artifact_settings)
    with sqlite3.connect(first.snapshot) as connection:
        connection.execute(
            "UPDATE metadata SET value = 'exact' WHERE key = 'ann_backend'"
        )
        connection.commit()
    segment = vector_index_module.artifact_segment_paths(Path(first.snapshot))[0]
    with sqlite3.connect(segment) as connection:
        connection.execute("UPDATE bursts SET ann_vector = X''")
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
    with vector_index_module._connect(Path(second.snapshot), read_only=True) as connection:
        vectors = int(
            connection.execute(
                "SELECT count(*) FROM bursts WHERE vector_blob IS NOT NULL"
            ).fetchone()[0]
        )
        ann_vectors = int(
            connection.execute(
                "SELECT count(*) FROM bursts WHERE vector_blob IS NOT NULL "
                "AND length(ann_vector) > 0"
            ).fetchone()[0]
        )
    assert ann_vectors == vectors


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


def test_pointer_publication_retries_a_transient_file_lock(
    artifact_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_settings.state_dir.mkdir(parents=True)
    snapshot = artifact_settings.state_dir / "artifact-index-test.sqlite"
    snapshot.touch()
    real_replace = vector_index_module.os.replace
    attempts = 0

    def replace_after_transient_lock(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("Synthetic transient pointer lock.")
        real_replace(source, destination)

    monkeypatch.setattr(vector_index_module.os, "replace", replace_after_transient_lock)
    monkeypatch.setattr(vector_index_module.time, "sleep", lambda _seconds: None)

    vector_index_module._publish_pointer(artifact_settings, snapshot)

    assert attempts == 2
    assert artifact_settings.artifact_pointer_path.is_file()


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

    assert before_end.hits
    assert after_start.hits
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


def test_timestamp_free_cue_is_indexed_by_source_order(
    artifact_settings: Settings,
) -> None:
    conversation = _event(
        "meeting", "meeting-order", "Order review", "2026-01-02T10:00:00Z"
    )
    transcript = _event(
        "transcript",
        "transcript-order",
        "",
        "2026-01-02T10:00:00Z",
        parent=("meeting", "meeting-order"),
    )
    cue = ArtifactEvent.model_validate(
        {
            "schema": "ai-memory/artifact-event@1",
            "record": "event",
            "entity": "transcript-cue",
            "operation": "upsert",
            "external_id": "cue-without-time",
            "parent": {
                "entity": "transcript",
                "external_id": "transcript-order",
            },
            "source_sequence": 7,
            "payload": {
                "text": "The timestamp-free orchid marker remains searchable.",
                "content_format": "plain",
            },
        }
    )
    ArtifactStore(artifact_settings).apply_batch(
        _batch("timestamp-free-cue", [conversation, transcript, cue])
    )

    built = build_artifact_vector_index(artifact_settings)
    result = search_artifact_vectors(
        artifact_settings,
        "orchid marker",
        ArtifactScope(entities=("transcript-cue",)),
        10,
    )

    assert built.missing_absolute_time == 1
    assert result.hits[0].artifact_id == artifact_id(
        "chat-source", "workspace", "transcript-cue", "cue-without-time"
    )
    assert result.hits[0].occurred_at is None


def test_long_message_tail_keeps_offsets_and_winning_text(
    artifact_settings: Settings,
) -> None:
    tail = "The unique zircon-tail-marker closes the record."
    ArtifactStore(artifact_settings).apply_batch(
        _batch(
            "long-message-tail",
            [
                _event(
                    "conversation",
                    "conversation-long",
                    "Long record",
                    "2026-01-02T10:00:00Z",
                ),
                _event(
                    "message",
                    "message-long",
                    ("Neutral prefix content. " * 400) + tail,
                    "2026-01-02T10:01:00Z",
                    parent=("conversation", "conversation-long"),
                ),
            ],
        )
    )
    build_artifact_vector_index(artifact_settings)

    result = search_artifact_vectors(
        artifact_settings,
        "zircon tail marker",
        ArtifactScope(entities=("message",)),
        10,
    )

    assert tail in result.hits[0].text
    assert result.hits[0].segment_start is not None
    assert result.hits[0].segment_start > 0


def test_short_reply_uses_cross_speaker_context_but_cites_reply(
    artifact_settings: Settings,
) -> None:
    first = _event(
        "conversation",
        "conversation-fruit",
        "Fruit",
        "2026-01-02T10:00:00Z",
    )
    question = _event(
        "message",
        "fruit-question",
        "Where can I buy apples?",
        "2026-01-02T10:01:00Z",
        parent=("conversation", "conversation-fruit"),
    )
    reply = ArtifactEvent.model_validate(
        {
            "schema": "ai-memory/artifact-event@1",
            "record": "event",
            "entity": "message",
            "operation": "upsert",
            "external_id": "fruit-reply",
            "parent": {
                "entity": "conversation",
                "external_id": "conversation-fruit",
            },
            "source_updated_at": "2026-01-02T10:02:00Z",
            "payload": {
                "text": "Here is the link to the fruit you like: https://fruit.example",
                "occurred_at": "2026-01-02T10:02:00Z",
                "author": {"id": "actor-b", "name": "Actor B"},
                "links": [
                    {
                        "relation": "reply-to",
                        "target": {
                            "entity": "message",
                            "external_id": "fruit-question",
                        },
                    }
                ],
            },
        }
    )
    ArtifactStore(artifact_settings).apply_batch(
        _batch("reply-context", [first, question, reply])
    )
    build_artifact_vector_index(artifact_settings)

    result = search_artifact_vectors(
        artifact_settings,
        "link to buy apples",
        ArtifactScope(entities=("message",)),
        10,
    )
    reply_id = artifact_id(
        "chat-source", "workspace", "message", "fruit-reply"
    )

    assert any(hit.artifact_id == reply_id for hit in result.hits)
    reply_hit = next(hit for hit in result.hits if hit.artifact_id == reply_id)
    assert "fruit you like" in reply_hit.text
    assert "Where can I buy apples" not in reply_hit.text


@pytest.mark.parametrize("record_count", [100, 10_000])
def test_ann_hydration_reads_selected_ids_without_scanning_segments(record_count):
    # Reproduce the production UNION and tombstone shape without embeddings.
    # VM work must stay bounded as the unselected population grows.
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        for number in range(2):
            connection.execute(f"ATTACH ':memory:' AS s{number}")
            connection.execute(f"CREATE TABLE s{number}.bursts(burst_id TEXT PRIMARY KEY, anchor_artifact_uri TEXT, text_content TEXT)")
            connection.execute(f"CREATE TABLE s{number}.updated_anchors(artifact_uri TEXT PRIMARY KEY)")
        connection.executemany(
            "INSERT INTO s0.bursts VALUES (?, ?, ?)",
            ((f"id-{n}", f"anchor-{n}", "original") for n in range(record_count)),
        )
        connection.execute("INSERT INTO s1.updated_anchors VALUES ('anchor-0')")
        connection.execute("INSERT INTO s1.bursts VALUES ('id-0', 'anchor-0', 'updated')")
        connection.execute(
            "CREATE TEMP VIEW bursts AS SELECT r.* FROM s0.bursts r "
            "WHERE NOT EXISTS (SELECT 1 FROM s1.updated_anchors u WHERE u.artifact_uri=r.anchor_artifact_uri) "
            "UNION ALL SELECT * FROM s1.bursts"
        )
        connection.execute("CREATE TEMP TABLE query_ann_candidates(burst_id TEXT PRIMARY KEY, distance REAL) WITHOUT ROWID")
        connection.executemany("INSERT INTO query_ann_candidates VALUES (?, 0)", [("id-0",), ("id-1",)])
        operations = 0
        def progress():
            nonlocal operations
            operations += 1
            return 0
        connection.set_progress_handler(progress, 1)
        rows = vector_index_module._fetch_ann_candidates(connection)
        connection.set_progress_handler(None, 0)
    assert {row["burst_id"]: row["text_content"] for row in rows} == {"id-0": "updated", "id-1": "original"}
    assert operations < 200


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
