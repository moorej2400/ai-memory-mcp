from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path

import pytest

from ai_memory_mcp.artifacts.models import (
    ArtifactBatchManifest,
    ArtifactEvent,
    ArtifactObjectInput,
    ArtifactPayload,
    ParsedArtifactBatch,
)
from ai_memory_mcp.artifacts.objects import store_object, verify_object
from ai_memory_mcp.artifacts.schema import connect_artifact_db
from ai_memory_mcp.artifacts.store import ArtifactStore
from ai_memory_mcp.config import Settings


def test_store_object_uses_content_addressed_path(
    artifact_settings: Settings,
    tmp_path: Path,
) -> None:
    source = tmp_path / "brief.txt"
    source.write_bytes(b"neutral attachment")
    digest = sha256(source.read_bytes()).hexdigest()

    stored = store_object(artifact_settings, source)

    assert stored.sha256 == digest
    assert stored.relative_path == f"sha256/{digest[:2]}/{digest}"
    assert stored.byte_count == len(b"neutral attachment")
    assert verify_object(artifact_settings, digest).ok is True


def test_store_object_rejects_a_wrong_expected_hash(
    artifact_settings: Settings,
    tmp_path: Path,
) -> None:
    source = tmp_path / "brief.txt"
    source.write_bytes(b"neutral attachment")
    with pytest.raises(ValueError, match="hash"):
        store_object(artifact_settings, source, expected_sha256="0" * 64)


def test_store_object_rejects_non_files_and_object_tree_sources(
    artifact_settings: Settings,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="regular file"):
        store_object(artifact_settings, tmp_path)

    nested = artifact_settings.artifact_objects_dir / "incoming.txt"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"do not copy from the object tree")
    with pytest.raises(ValueError, match="object directory"):
        store_object(artifact_settings, nested)


def test_existing_object_is_verified_and_reused(
    artifact_settings: Settings,
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"same bytes")
    second.write_bytes(b"same bytes")
    stored_first = store_object(artifact_settings, first)
    stored_second = store_object(artifact_settings, second)
    assert stored_second == stored_first


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode test")
def test_stored_objects_use_private_modes(
    artifact_settings: Settings,
    tmp_path: Path,
) -> None:
    source = tmp_path / "private.bin"
    source.write_bytes(b"private bytes")
    stored = store_object(artifact_settings, source)
    path = artifact_settings.artifact_objects_dir / stored.relative_path
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_intake_links_object_without_storing_the_source_path(
    artifact_settings: Settings,
    tmp_path: Path,
) -> None:
    source = tmp_path / "attachment.txt"
    source.write_bytes(b"attachment body")
    event = ArtifactEvent.model_validate(
        {
            "schema": "ai-memory/artifact-event@1",
            "record": "event",
            "entity": "attachment",
            "operation": "upsert",
            "external_id": "attachment-1",
            "source_updated_at": "2026-01-02T12:00:00Z",
            "payload": ArtifactPayload(
                title="Example attachment",
                object=ArtifactObjectInput(
                    local_source_path=source,
                    media_type="text/plain",
                    original_name="attachment.txt",
                ),
            ),
        }
    )
    manifest = ArtifactBatchManifest.model_validate(
        {
            "schema": "ai-memory/artifact-batch@1",
            "record": "batch",
            "batch_id": "object-batch-1",
            "source": "chat-source",
            "source_instance": "workspace",
            "observed_at": "2026-01-02T12:00:00Z",
            "event_count": 1,
        }
    )
    batch = ParsedArtifactBatch(
        manifest=manifest,
        events=[event],
        input_sha256="a" * 64,
    )
    ArtifactStore(artifact_settings).apply_batch(batch)

    with connect_artifact_db(
        artifact_settings.artifact_db,
        read_only=True,
    ) as connection:
        row = connection.execute(
            """
            SELECT a.payload_json, o.sha256, o.relative_path, l.original_name
            FROM artifacts AS a
            JOIN artifact_object_links AS l USING(artifact_id)
            JOIN artifact_objects AS o USING(sha256)
            """
        ).fetchone()
    assert row is not None
    payload = json.loads(row["payload_json"])
    assert "local_source_path" not in payload["object"]
    assert str(source) not in row["payload_json"]
    assert row["sha256"] == sha256(source.read_bytes()).hexdigest()
    assert row["relative_path"].startswith("sha256/")
    assert row["original_name"] == "attachment.txt"


def test_object_failure_happens_before_database_state(
    artifact_settings: Settings,
    tmp_path: Path,
) -> None:
    source = tmp_path / "attachment.txt"
    source.write_bytes(b"attachment body")
    event = ArtifactEvent.model_validate(
        {
            "schema": "ai-memory/artifact-event@1",
            "record": "event",
            "entity": "attachment",
            "operation": "upsert",
            "external_id": "attachment-1",
            "payload": ArtifactPayload(
                object=ArtifactObjectInput(
                    local_source_path=source,
                    expected_sha256="0" * 64,
                )
            ),
        }
    )
    manifest = ArtifactBatchManifest.model_validate(
        {
            "schema": "ai-memory/artifact-batch@1",
            "record": "batch",
            "batch_id": "object-batch-1",
            "source": "chat-source",
            "source_instance": "workspace",
            "observed_at": "2026-01-02T12:00:00Z",
            "event_count": 1,
        }
    )
    batch = ParsedArtifactBatch(
        manifest=manifest,
        events=[event],
        input_sha256="b" * 64,
    )

    with pytest.raises(ValueError, match="hash"):
        ArtifactStore(artifact_settings).apply_batch(batch)
    with connect_artifact_db(
        artifact_settings.artifact_db,
        read_only=True,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM artifact_batches"
        ).fetchone()[0] == 0
