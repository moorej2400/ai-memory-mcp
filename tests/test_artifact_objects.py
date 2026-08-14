from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from threading import Barrier

import pytest

import ai_memory_mcp.artifacts.objects as objects_module
from ai_memory_mcp.artifacts.models import (
    ArtifactBatchManifest,
    ArtifactEvent,
    ArtifactObjectInput,
    ArtifactPayload,
    ParsedArtifactBatch,
    RedactionPayload,
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


def test_concurrent_object_publication_reuses_the_verified_winner(
    artifact_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"same concurrent bytes")
    second.write_bytes(b"same concurrent bytes")
    publication_barrier = Barrier(2)
    real_link = os.link

    def synchronized_link(source: Path, destination: Path) -> None:
        publication_barrier.wait(timeout=5)
        real_link(source, destination)

    monkeypatch.setattr(os, "link", synchronized_link)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(store_object, artifact_settings, source)
            for source in (first, second)
        ]
        stored = [future.result(timeout=10) for future in futures]

    assert stored[0] == stored[1]
    assert verify_object(artifact_settings, stored[0].sha256).ok is True
    assert not list(
        (artifact_settings.artifact_objects_dir / "sha256").rglob("*.partial-*")
    )


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


def test_redaction_quarantines_an_unshared_object_and_blocks_later_handoffs(
    artifact_settings: Settings,
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.txt"
    first.write_bytes(b"first private attachment")
    later = tmp_path / "later.txt"
    later.write_bytes(b"later private attachment")
    store = ArtifactStore(artifact_settings)

    def object_event(path: Path, *, source_sequence: int) -> ArtifactEvent:
        return ArtifactEvent.model_validate(
            {
                "schema": "ai-memory/artifact-event@1",
                "record": "event",
                "entity": "attachment",
                "operation": "upsert",
                "external_id": "attachment-redaction",
                "source_sequence": source_sequence,
                "payload": ArtifactPayload(
                    object=ArtifactObjectInput(local_source_path=path)
                ),
            }
        )

    def batch(event: ArtifactEvent, batch_id: str) -> ParsedArtifactBatch:
        return ParsedArtifactBatch(
            manifest=ArtifactBatchManifest.model_validate(
                {
                    "schema": "ai-memory/artifact-batch@1",
                    "record": "batch",
                    "batch_id": batch_id,
                    "source": "chat-source",
                    "source_instance": "workspace",
                    "observed_at": "2026-01-02T12:00:00Z",
                    "event_count": 1,
                }
            ),
            events=[event],
            input_sha256=(batch_id * 64)[:64],
        )

    store.apply_batch(batch(object_event(first, source_sequence=1), "object-first"))
    first_digest = sha256(first.read_bytes()).hexdigest()
    first_object = (
        artifact_settings.artifact_objects_dir
        / "sha256"
        / first_digest[:2]
        / first_digest
    )
    assert first_object.is_file()
    redaction = ArtifactEvent.model_validate(
        {
            "schema": "ai-memory/artifact-event@1",
            "record": "event",
            "entity": "attachment",
            "operation": "redact",
            "external_id": "attachment-redaction",
            "source_sequence": 2,
            "payload": RedactionPayload(reason="Source privacy request"),
        }
    )
    store.apply_batch(batch(redaction, "object-redact"))

    quarantined = list(
        (artifact_settings.artifact_objects_dir / "quarantine").rglob(
            f"{first_digest}-*"
        )
    )
    assert first_object.exists() is False
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == b"first private attachment"

    later_digest = sha256(later.read_bytes()).hexdigest()
    later_event = object_event(later, source_sequence=3)
    later.rename(tmp_path / "later-moved.txt")
    store.apply_batch(batch(later_event, "object-later"))
    later_object = (
        artifact_settings.artifact_objects_dir
        / "sha256"
        / later_digest[:2]
        / later_digest
    )
    assert later_object.exists() is False


def test_same_batch_redaction_does_not_require_an_upsert_object_handoff(
    artifact_settings: Settings,
    tmp_path: Path,
) -> None:
    missing_handoff = tmp_path / "missing-handoff.bin"
    digest = sha256(b"bytes that must not be read").hexdigest()
    events = [
        ArtifactEvent.model_validate(
            {
                "schema": "ai-memory/artifact-event@1",
                "record": "event",
                "entity": "attachment",
                "operation": "upsert",
                "external_id": "same-batch-redaction",
                "source_sequence": 1,
                "payload": ArtifactPayload(
                    object=ArtifactObjectInput(
                        local_source_path=missing_handoff,
                        expected_sha256=digest,
                        original_name="private.bin",
                    )
                ),
            }
        ),
        ArtifactEvent.model_validate(
            {
                "schema": "ai-memory/artifact-event@1",
                "record": "event",
                "entity": "attachment",
                "operation": "redact",
                "external_id": "same-batch-redaction",
                "source_sequence": 2,
                "payload": RedactionPayload(reason="Source privacy request"),
            }
        ),
    ]
    batch = ParsedArtifactBatch(
        manifest=ArtifactBatchManifest.model_validate(
            {
                "schema": "ai-memory/artifact-batch@1",
                "record": "batch",
                "batch_id": "same-batch-object-redaction",
                "source": "chat-source",
                "source_instance": "workspace",
                "observed_at": "2026-01-02T12:00:00Z",
                "event_count": len(events),
            }
        ),
        events=events,
        input_sha256="d" * 64,
    )

    receipt = ArtifactStore(artifact_settings).apply_batch(batch)

    assert receipt.accepted == 1
    assert receipt.redactions == 1
    current = ArtifactStore(artifact_settings).get_by_external_id(
        "chat-source",
        "workspace",
        "attachment",
        "same-batch-redaction",
    )
    assert current.redacted_at is not None
    with connect_artifact_db(
        artifact_settings.artifact_db,
        read_only=True,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM artifact_objects"
        ).fetchone()[0] == 0


def test_redaction_quarantines_current_prior_and_stale_revision_objects(
    artifact_settings: Settings,
    tmp_path: Path,
) -> None:
    sources = [tmp_path / f"revision-{index}.txt" for index in (1, 2, 3)]
    for index, source in enumerate(sources, start=1):
        source.write_bytes(f"private revision {index}".encode())

    def object_event(path: Path, source_sequence: int) -> ArtifactEvent:
        return ArtifactEvent.model_validate(
            {
                "schema": "ai-memory/artifact-event@1",
                "record": "event",
                "entity": "attachment",
                "operation": "upsert",
                "external_id": "attachment-revisions",
                "source_sequence": source_sequence,
                "payload": ArtifactPayload(
                    object=ArtifactObjectInput(local_source_path=path)
                ),
            }
        )

    def batch(
        event: ArtifactEvent,
        batch_id: str,
        input_character: str,
    ) -> ParsedArtifactBatch:
        return ParsedArtifactBatch(
            manifest=ArtifactBatchManifest.model_validate(
                {
                    "schema": "ai-memory/artifact-batch@1",
                    "record": "batch",
                    "batch_id": batch_id,
                    "source": "chat-source",
                    "source_instance": "workspace",
                    "observed_at": "2026-01-02T12:00:00Z",
                    "event_count": 1,
                }
            ),
            events=[event],
            input_sha256=input_character * 64,
        )

    store = ArtifactStore(artifact_settings)
    store.apply_batch(batch(object_event(sources[0], 1), "revision-a", "a"))
    store.apply_batch(batch(object_event(sources[1], 2), "revision-b", "b"))
    stale = store.apply_batch(
        batch(object_event(sources[2], 1), "revision-stale", "c")
    )
    assert stale.stale == 1

    redaction = ArtifactEvent.model_validate(
        {
            "schema": "ai-memory/artifact-event@1",
            "record": "event",
            "entity": "attachment",
            "operation": "redact",
            "external_id": "attachment-revisions",
            "source_sequence": 3,
            "payload": RedactionPayload(reason="Source privacy request"),
        }
    )
    store.apply_batch(batch(redaction, "revision-redact", "d"))

    for source in sources:
        digest = sha256(source.read_bytes()).hexdigest()
        active_path = (
            artifact_settings.artifact_objects_dir
            / "sha256"
            / digest[:2]
            / digest
        )
        quarantined = list(
            (artifact_settings.artifact_objects_dir / "quarantine").rglob(
                f"{digest}-*"
            )
        )
        assert active_path.exists() is False
        assert len(quarantined) == 1
        assert quarantined[0].read_bytes() == source.read_bytes()

    with connect_artifact_db(
        artifact_settings.artifact_db,
        read_only=True,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM artifact_objects"
        ).fetchone()[0] == 0


def test_redaction_preserves_an_object_that_an_active_artifact_still_uses(
    artifact_settings: Settings,
    tmp_path: Path,
) -> None:
    source = tmp_path / "shared.txt"
    source.write_bytes(b"shared attachment bytes")
    digest = sha256(source.read_bytes()).hexdigest()
    events = [
        ArtifactEvent.model_validate(
            {
                "schema": "ai-memory/artifact-event@1",
                "record": "event",
                "entity": "attachment",
                "operation": "upsert",
                "external_id": external_id,
                "source_sequence": 1,
                "payload": ArtifactPayload(
                    object=ArtifactObjectInput(local_source_path=source)
                ),
            }
        )
        for external_id in ("attachment-shared-a", "attachment-shared-b")
    ]
    initial = ParsedArtifactBatch(
        manifest=ArtifactBatchManifest.model_validate(
            {
                "schema": "ai-memory/artifact-batch@1",
                "record": "batch",
                "batch_id": "object-shared-initial",
                "source": "chat-source",
                "source_instance": "workspace",
                "observed_at": "2026-01-02T12:00:00Z",
                "event_count": 2,
            }
        ),
        events=events,
        input_sha256="c" * 64,
    )
    store = ArtifactStore(artifact_settings)
    store.apply_batch(initial)
    redaction = ArtifactEvent.model_validate(
        {
            "schema": "ai-memory/artifact-event@1",
            "record": "event",
            "entity": "attachment",
            "operation": "redact",
            "external_id": "attachment-shared-a",
            "source_sequence": 2,
            "payload": RedactionPayload(reason="Source privacy request"),
        }
    )
    store.apply_batch(
        ParsedArtifactBatch(
            manifest=ArtifactBatchManifest.model_validate(
                {
                    "schema": "ai-memory/artifact-batch@1",
                    "record": "batch",
                    "batch_id": "object-shared-redact",
                    "source": "chat-source",
                    "source_instance": "workspace",
                    "observed_at": "2026-01-02T12:01:00Z",
                    "event_count": 1,
                }
            ),
            events=[redaction],
            input_sha256="d" * 64,
        )
    )

    object_path = artifact_settings.artifact_objects_dir / "sha256" / digest[:2] / digest
    assert object_path.read_bytes() == b"shared attachment bytes"
    assert not list(
        (artifact_settings.artifact_objects_dir / "quarantine").rglob(
            f"{digest}-*"
        )
    )


def test_redaction_preserves_object_bytes_used_by_a_nonredacted_revision(
    artifact_settings: Settings,
    tmp_path: Path,
) -> None:
    historical = tmp_path / "historical.txt"
    historical.write_bytes(b"historical shared bytes")
    current = tmp_path / "current.txt"
    current.write_bytes(b"current private bytes")
    historical_digest = sha256(historical.read_bytes()).hexdigest()
    current_digest = sha256(current.read_bytes()).hexdigest()

    def object_event(external_id: str, source: Path, sequence: int) -> ArtifactEvent:
        return ArtifactEvent.model_validate(
            {
                "schema": "ai-memory/artifact-event@1",
                "record": "event",
                "entity": "attachment",
                "operation": "upsert",
                "external_id": external_id,
                "source_sequence": sequence,
                "payload": ArtifactPayload(
                    object=ArtifactObjectInput(local_source_path=source)
                ),
            }
        )

    def redact_event(external_id: str, sequence: int) -> ArtifactEvent:
        return ArtifactEvent.model_validate(
            {
                "schema": "ai-memory/artifact-event@1",
                "record": "event",
                "entity": "attachment",
                "operation": "redact",
                "external_id": external_id,
                "source_sequence": sequence,
                "payload": RedactionPayload(reason="Source privacy request"),
            }
        )

    def batch(
        events: list[ArtifactEvent],
        batch_id: str,
        input_character: str,
    ) -> ParsedArtifactBatch:
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
            input_sha256=input_character * 64,
        )

    store = ArtifactStore(artifact_settings)
    store.apply_batch(
        batch(
            [
                object_event("revision-owner", historical, 1),
                object_event("current-sharer", historical, 1),
            ],
            "historical-share-initial",
            "a",
        )
    )
    store.apply_batch(
        batch(
            [object_event("revision-owner", current, 2)],
            "historical-share-correction",
            "b",
        )
    )
    store.apply_batch(
        batch(
            [redact_event("current-sharer", 2)],
            "historical-share-redact-current",
            "c",
        )
    )

    historical_path = (
        artifact_settings.artifact_objects_dir
        / "sha256"
        / historical_digest[:2]
        / historical_digest
    )
    assert historical_path.read_bytes() == b"historical shared bytes"
    with connect_artifact_db(
        artifact_settings.artifact_db,
        read_only=True,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM artifact_objects WHERE sha256 = ?",
            (historical_digest,),
        ).fetchone()[0] == 1
    assert not list(
        (artifact_settings.artifact_objects_dir / "quarantine").rglob(
            f"{historical_digest}-*"
        )
    )

    store.apply_batch(
        batch(
            [redact_event("revision-owner", 3)],
            "historical-share-redact-owner",
            "d",
        )
    )
    for digest in (historical_digest, current_digest):
        active_path = (
            artifact_settings.artifact_objects_dir
            / "sha256"
            / digest[:2]
            / digest
        )
        assert active_path.exists() is False
        assert len(
            list(
                (artifact_settings.artifact_objects_dir / "quarantine").rglob(
                    f"{digest}-*"
                )
            )
        ) == 1


def test_redaction_restores_earlier_object_moves_when_a_later_move_fails(
    artifact_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = [tmp_path / "first.txt", tmp_path / "second.txt"]
    for index, source in enumerate(sources, start=1):
        source.write_bytes(f"private object {index}".encode())
    upserts = [
        ArtifactEvent.model_validate(
            {
                "schema": "ai-memory/artifact-event@1",
                "record": "event",
                "entity": "attachment",
                "operation": "upsert",
                "external_id": f"attachment-rollback-{index}",
                "source_sequence": 1,
                "payload": ArtifactPayload(
                    object=ArtifactObjectInput(local_source_path=source)
                ),
            }
        )
        for index, source in enumerate(sources, start=1)
    ]

    def batch(events: list[ArtifactEvent], batch_id: str) -> ParsedArtifactBatch:
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

    store = ArtifactStore(artifact_settings)
    store.apply_batch(batch(upserts, "object-rollback-initial"))
    object_paths = [
        artifact_settings.artifact_objects_dir
        / "sha256"
        / digest[:2]
        / digest
        for digest in (sha256(source.read_bytes()).hexdigest() for source in sources)
    ]
    redactions = [
        ArtifactEvent.model_validate(
            {
                "schema": "ai-memory/artifact-event@1",
                "record": "event",
                "entity": "attachment",
                "operation": "redact",
                "external_id": f"attachment-rollback-{index}",
                "source_sequence": 2,
                "payload": RedactionPayload(reason="Source privacy request"),
            }
        )
        for index in (1, 2)
    ]
    real_quarantine = objects_module.quarantine_object
    calls = 0

    def fail_second_quarantine(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic quarantine failure")
        return real_quarantine(*args, **kwargs)

    monkeypatch.setattr(
        objects_module,
        "quarantine_object",
        fail_second_quarantine,
    )
    with pytest.raises(OSError, match="synthetic quarantine failure"):
        store.apply_batch(batch(redactions, "object-rollback-redact"))

    assert all(path.is_file() for path in object_paths)
    assert store.count("attachment") == 2
    assert not list(
        path
        for path in (artifact_settings.artifact_objects_dir / "quarantine").rglob("*")
        if path.is_file()
    )


def test_metadata_only_snapshot_restores_object_links_after_coverage_and_cascade(
    artifact_settings: Settings,
    tmp_path: Path,
) -> None:
    source = tmp_path / "restored.txt"
    source.write_bytes(b"restored attachment bytes")
    digest = sha256(source.read_bytes()).hexdigest()

    conversation = ArtifactEvent.model_validate(
        {
            "schema": "ai-memory/artifact-event@1",
            "record": "event",
            "entity": "conversation",
            "operation": "upsert",
            "external_id": "object-restore-conversation",
            "source_sequence": 1,
            "payload": ArtifactPayload(title="Object restore conversation"),
        }
    )
    message = ArtifactEvent.model_validate(
        {
            "schema": "ai-memory/artifact-event@1",
            "record": "event",
            "entity": "message",
            "operation": "upsert",
            "external_id": "object-restore-message",
            "parent": {
                "entity": "conversation",
                "external_id": "object-restore-conversation",
            },
            "source_sequence": 1,
            "payload": ArtifactPayload(text="Attachment source message"),
        }
    )
    attachment = ArtifactEvent.model_validate(
        {
            "schema": "ai-memory/artifact-event@1",
            "record": "event",
            "entity": "attachment",
            "operation": "upsert",
            "external_id": "object-restore-attachment",
            "parent": {
                "entity": "message",
                "external_id": "object-restore-message",
            },
            "source_sequence": 1,
            "payload": ArtifactPayload(
                title="restored.txt",
                object=ArtifactObjectInput(
                    local_source_path=source,
                    media_type="text/plain",
                    original_name="restored.txt",
                ),
            ),
        }
    )
    metadata_attachment = attachment.model_copy(deep=True)
    assert isinstance(metadata_attachment.payload, ArtifactPayload)
    metadata_attachment.payload.object = ArtifactObjectInput(
        expected_sha256=digest,
        media_type="text/plain",
        original_name="restored.txt",
    )

    def batch(
        batch_id: str,
        observed_at: str,
        events: list[ArtifactEvent],
        input_character: str,
        *,
        coverage: list[dict[str, object]] | None = None,
    ) -> ParsedArtifactBatch:
        return ParsedArtifactBatch(
            manifest=ArtifactBatchManifest.model_validate(
                {
                    "schema": "ai-memory/artifact-batch@1",
                    "record": "batch",
                    "batch_id": batch_id,
                    "source": "chat-source",
                    "source_instance": "workspace",
                    "observed_at": observed_at,
                    "event_count": len(events),
                    "coverage": coverage or [],
                }
            ),
            events=events,
            input_sha256=input_character * 64,
        )

    store = ArtifactStore(artifact_settings)
    store.apply_batch(
        batch(
            "object-restore-initial",
            "2026-01-02T12:00:00Z",
            [conversation, message, attachment],
            "a",
        )
    )

    def assert_one_link() -> None:
        with connect_artifact_db(
            artifact_settings.artifact_db,
            read_only=True,
        ) as connection:
            assert connection.execute(
                "SELECT count(*) FROM artifact_object_links WHERE sha256 = ?",
                (digest,),
            ).fetchone()[0] == 1

    assert_one_link()
    store.apply_batch(
        batch(
            "object-restore-attachment-coverage",
            "2026-01-02T12:01:00Z",
            [],
            "b",
            coverage=[
                {
                    "parent": {
                        "entity": "message",
                        "external_id": "object-restore-message",
                    },
                    "entity": "attachment",
                    "complete": True,
                }
            ],
        )
    )
    store.apply_batch(
        batch(
            "object-restore-after-coverage",
            "2026-01-02T12:02:00Z",
            [metadata_attachment],
            "c",
        )
    )
    assert_one_link()

    store.apply_batch(
        batch(
            "object-restore-message-coverage",
            "2026-01-02T12:03:00Z",
            [],
            "d",
            coverage=[
                {
                    "parent": {
                        "entity": "conversation",
                        "external_id": "object-restore-conversation",
                    },
                    "entity": "message",
                    "complete": True,
                }
            ],
        )
    )
    store.apply_batch(
        batch(
            "object-restore-after-cascade",
            "2026-01-02T12:04:00Z",
            [message, metadata_attachment],
            "e",
        )
    )
    assert_one_link()
    object_path = (
        artifact_settings.artifact_objects_dir / "sha256" / digest[:2] / digest
    )
    assert object_path.read_bytes() == b"restored attachment bytes"


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
        assert (
            connection.execute("SELECT count(*) FROM artifact_batches").fetchone()[0]
            == 0
        )
