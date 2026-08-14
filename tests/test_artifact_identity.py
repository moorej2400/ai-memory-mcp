from __future__ import annotations

import pytest

from ai_memory_mcp.artifacts.identity import (
    artifact_id,
    artifact_uri,
    event_id,
    parse_artifact_uri,
)


def test_artifact_id_includes_source_instance_and_entity() -> None:
    first = artifact_id("teams", "work", "message", "message-17")
    assert first == artifact_id("teams", "work", "message", "message-17")
    assert first != artifact_id("teams", "personal", "message", "message-17")
    assert first != artifact_id("teams", "work", "meeting", "message-17")
    assert first.startswith("art_")


def test_artifact_uri_does_not_expose_external_id() -> None:
    value = artifact_id("teams", "work", "message", "unsafe/../value")
    uri = artifact_uri("message", value)
    assert uri == f"artifact://message/{value}"
    assert "unsafe" not in uri
    assert parse_artifact_uri(uri) == ("message", value)


def test_identity_rejects_invalid_fields() -> None:
    with pytest.raises(ValueError, match="source"):
        artifact_id("Teams Tenant", "work", "message", "message-17")
    with pytest.raises(ValueError, match="external"):
        artifact_id("teams", "work", "message", "")
    with pytest.raises(ValueError, match="Artifact URI"):
        parse_artifact_uri("artifact://message/not-an-artifact-id")


def test_event_id_ignores_batch_observation_data() -> None:
    fields = {
        "entity": "message",
        "external_id": "message-17",
        "operation": "upsert",
        "source_sequence": 3,
        "payload_sha256": "a" * 64,
    }
    assert event_id("teams", "work", fields).startswith("evt_")
    assert event_id("teams", "work", fields) == event_id(
        "teams",
        "work",
        {**fields, "batch_id": "another-batch", "observed_at": "tomorrow"},
    )


def test_event_id_changes_for_a_parent_only_correction() -> None:
    fields = {
        "entity": "message",
        "external_id": "message-17",
        "operation": "upsert",
        "source_updated_at": "2026-01-02T10:00:00+00:00",
        "payload_sha256": "a" * 64,
    }
    assert event_id(
        "teams",
        "work",
        {**fields, "parent_artifact_id": "art_" + "a" * 32},
    ) != event_id(
        "teams",
        "work",
        {**fields, "parent_artifact_id": "art_" + "b" * 32},
    )
