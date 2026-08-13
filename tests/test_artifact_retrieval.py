from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from ai_memory_mcp.artifacts.identity import artifact_id, artifact_uri
from ai_memory_mcp.artifacts.models import (
    ArtifactBatchManifest,
    ArtifactEvent,
    ArtifactPayload,
    ParsedArtifactBatch,
)
from ai_memory_mcp.artifacts.store import ArtifactStore
from ai_memory_mcp.config import Settings
from ai_memory_mcp.service import MemoryService


def _raw_batch(
    *,
    batch_id: str = "recall-batch-1",
    external_id: str = "message-1",
    text: str = "Use the documented rotation procedure.",
    occurred_at: str = "2026-01-02T10:00:00Z",
) -> ParsedArtifactBatch:
    event = ArtifactEvent.model_validate(
        {
            "schema": "ai-memory/artifact-event@1",
            "record": "event",
            "entity": "message",
            "operation": "upsert",
            "external_id": external_id,
            "source_updated_at": occurred_at,
            "payload": ArtifactPayload(
                text=text,
                occurred_at=occurred_at,
                content_format="plain",
            ),
        }
    )
    manifest = ArtifactBatchManifest.model_validate(
        {
            "schema": "ai-memory/artifact-batch@1",
            "record": "batch",
            "batch_id": batch_id,
            "source": "chat-source",
            "source_instance": "workspace",
            "observed_at": occurred_at,
            "event_count": 1,
        }
    )
    return ParsedArtifactBatch(
        manifest=manifest,
        events=[event],
        input_sha256=(batch_id * 64)[:64],
    )


def test_raw_paraphrase_is_a_lead_not_an_answer(
    artifact_settings: Settings,
) -> None:
    ArtifactStore(artifact_settings).apply_batch(
        _raw_batch(
            text="Change the credential with the documented rotation procedure."
        )
    )
    response = MemoryService(artifact_settings).recall(
        "How do we change the credential?",
        source_label="chat-source",
    )
    assert response.status == "no_answer"
    assert response.evidence
    assert response.evidence[0].evidence_class == "raw"
    assert any("Raw artifact" in warning for warning in response.warnings)


def test_exact_raw_phrase_can_answer(artifact_settings: Settings) -> None:
    ArtifactStore(artifact_settings).apply_batch(_raw_batch())
    response = MemoryService(artifact_settings).recall(
        '"Use the documented rotation procedure."',
        source_label="chat-source",
    )
    assert response.status == "answered"
    assert response.evidence[0].evidence_class == "raw"
    assert response.citations[0].path.startswith("artifact://")


def test_artifact_recall_works_without_a_markdown_index(
    artifact_settings: Settings,
) -> None:
    ArtifactStore(artifact_settings).apply_batch(_raw_batch())
    assert not artifact_settings.pointer_path.exists()
    response = MemoryService(artifact_settings).recall(
        '"Use the documented rotation procedure."',
        source_label="chat-source",
    )
    assert response.status == "answered"
    assert response.evidence[0].evidence_class == "raw"
    assert not artifact_settings.pointer_path.exists()


def test_exact_artifact_uri_routes_to_the_focus(
    artifact_settings: Settings,
) -> None:
    ArtifactStore(artifact_settings).apply_batch(_raw_batch())
    reference = artifact_uri(
        "message",
        artifact_id("chat-source", "workspace", "message", "message-1"),
    )
    response = MemoryService(artifact_settings).recall(reference)
    assert response.status == "answered"
    assert response.intent == "exact"
    assert response.citations[0].path == reference


def test_recent_raw_near_tie_ranks_above_old_raw(
    artifact_settings: Settings,
) -> None:
    store = ArtifactStore(artifact_settings)
    store.apply_batch(
        _raw_batch(
            batch_id="old-batch",
            external_id="old-message",
            occurred_at="2020-01-02T10:00:00Z",
        )
    )
    store.apply_batch(
        _raw_batch(
            batch_id="recent-batch",
            external_id="recent-message",
            occurred_at="2026-08-01T10:00:00Z",
        )
    )
    response = MemoryService(artifact_settings).recall(
        "documented rotation",
        source_label="chat-source",
    )
    recent = artifact_id(
        "chat-source", "workspace", "message", "recent-message"
    )
    assert response.evidence[0].memory_id == recent


def test_old_exact_external_identifier_can_answer(
    artifact_settings: Settings,
) -> None:
    ArtifactStore(artifact_settings).apply_batch(
        _raw_batch(
            external_id="old-message",
            occurred_at="2020-01-02T10:00:00Z",
        )
    )
    response = MemoryService(artifact_settings).recall(
        "old-message",
        source_label="chat-source",
    )
    assert response.status == "answered"
    assert response.evidence[0].reasons == ["exact identifier"]


def test_markdown_recall_works_when_artifact_database_is_missing(
    benchmark_settings: Settings,
    tmp_path: Path,
) -> None:
    settings = replace(
        benchmark_settings,
        artifact_db=tmp_path / "missing" / "artifacts.sqlite3",
        artifact_objects_dir=tmp_path / "missing" / "objects",
        artifact_backup_dir=tmp_path / "missing" / "backups",
    )
    response = MemoryService(settings).recall("ALPHA-142", limit=1)
    assert response.status == "answered"
    assert response.evidence[0].evidence_class == "distilled"
    assert not settings.artifact_db.exists()


def test_distilled_note_outranks_stale_raw_paraphrase(
    benchmark_settings: Settings,
    tmp_path: Path,
) -> None:
    settings = replace(
        benchmark_settings,
        artifact_db=tmp_path / "artifacts.sqlite3",
        artifact_objects_dir=tmp_path / "objects",
        artifact_backup_dir=tmp_path / "backups",
    )
    ArtifactStore(settings).apply_batch(
        _raw_batch(
            text="Transient authentication retry policy guidance.",
            occurred_at="2020-01-02T10:00:00Z",
        )
    )
    response = MemoryService(settings).recall(
        "transient authentication retry policy",
        limit=5,
    )
    assert response.evidence[0].evidence_class == "distilled"


def test_raw_audit_log_contains_digests_instead_of_text(
    artifact_settings: Settings,
    tmp_path: Path,
) -> None:
    settings = replace(artifact_settings, log_dir=tmp_path / "logs")
    sensitive_marker = "private-marker-for-audit-test"
    ArtifactStore(settings).apply_batch(_raw_batch(text=sensitive_marker))
    MemoryService(settings).recall(
        f'"{sensitive_marker}"',
        source_label="chat-source",
    )
    record = json.loads(
        (settings.resolved_log_dir / "retrieval.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )
    serialized = json.dumps(record)
    assert sensitive_marker not in serialized
    raw = record["response"]["evidence"][0]
    assert set(raw) == {
        "artifact_uri",
        "evidence_class",
        "source_label",
        "score",
        "text_sha256",
        "text_characters",
    }


def test_status_and_ordered_read_include_artifact_state(
    artifact_settings: Settings,
) -> None:
    ArtifactStore(artifact_settings).apply_batch(_raw_batch())
    service = MemoryService(artifact_settings)
    status = service.status()
    assert status.artifact_database.available is True
    assert status.artifact_database.schema_version == 1
    assert status.artifact_database.artifacts == 1

    reference = artifact_uri(
        "message",
        artifact_id("chat-source", "workspace", "message", "message-1"),
    )
    read = service.artifact_read(reference, include_payload=True)
    assert read.focus == reference
    assert read.records[0].text == "Use the documented rotation procedure."
