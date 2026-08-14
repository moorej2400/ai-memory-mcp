from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai_memory_mcp.artifacts.models import (
    ArtifactBatchManifest,
    ArtifactEvent,
)


def test_upsert_requires_a_payload() -> None:
    with pytest.raises(ValidationError, match="payload"):
        ArtifactEvent.model_validate(
            {
                "schema": "ai-memory/artifact-event@1",
                "record": "event",
                "entity": "message",
                "operation": "upsert",
                "external_id": "message-17",
            }
        )


def test_delete_forbids_a_payload() -> None:
    with pytest.raises(ValidationError, match="payload"):
        ArtifactEvent.model_validate(
            {
                "schema": "ai-memory/artifact-event@1",
                "record": "event",
                "entity": "message",
                "operation": "delete",
                "external_id": "message-17",
                "payload": {"title": "unexpected"},
            }
        )


def test_manifest_rejects_an_invalid_batch_id() -> None:
    with pytest.raises(ValidationError, match="batch_id"):
        ArtifactBatchManifest.model_validate(
            {
                "schema": "ai-memory/artifact-batch@1",
                "record": "batch",
                "batch_id": "bad batch path/../",
                "source": "teams",
                "source_instance": "work",
                "observed_at": "2026-08-13T10:00:00Z",
                "event_count": 0,
            }
        )


def test_event_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="unknown"):
        ArtifactEvent.model_validate(
            {
                "schema": "ai-memory/artifact-event@1",
                "record": "event",
                "entity": "message",
                "operation": "upsert",
                "external_id": "message-17",
                "payload": {"text": "Useful text."},
                "unknown": "value",
            }
        )
