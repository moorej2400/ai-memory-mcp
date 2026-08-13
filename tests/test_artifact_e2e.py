from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

from ai_memory_mcp.artifacts.distillation import (
    list_pending_distillations,
    mark_distilled,
)
from ai_memory_mcp.artifacts.identity import artifact_id, artifact_uri
from ai_memory_mcp.artifacts.ingest import (
    ingest_artifact_batch,
    read_artifact_batch,
)
from ai_memory_mcp.artifacts.models import ArtifactScope
from ai_memory_mcp.artifacts.search import ArtifactSearch
from ai_memory_mcp.artifacts.store import ArtifactStore
from ai_memory_mcp.config import Settings
from ai_memory_mcp.service import MemoryService


def _event(
    entity: str,
    external_id: str,
    text: str,
    occurred_at: str,
    *,
    parent: tuple[str, str] | None = None,
    title: str | None = None,
    links: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    event: dict[str, object] = {
        "schema": "ai-memory/artifact-event@1",
        "record": "event",
        "entity": entity,
        "operation": "upsert",
        "external_id": external_id,
        "source_updated_at": occurred_at,
        "payload": {
            "title": title,
            "text": text,
            "occurred_at": occurred_at,
            "content_format": "plain",
            "links": links or [],
        },
    }
    if parent is not None:
        event["parent"] = {
            "entity": parent[0],
            "external_id": parent[1],
        }
    return event


def _jsonl_batch() -> str:
    messages = [
        "We need one release control for production changes.",
        "The release owner will record the approval before deployment.",
        "The operations reviewer will verify the rollback command.",
        "Use the amber release gate for every production rollout.",
        "The team will test the gate in the staging environment.",
        "The next review will confirm the first production result.",
    ]
    cues = [
        "Today we must select the production release control.",
        "Use the amber release gate for every production rollout.",
        "Record the approval before the deployment starts.",
        "The release owner is responsible for that approval record.",
        "Verify the rollback command before the change window.",
        "Test the release gate in the staging environment first.",
        "Review the first production result at the next meeting.",
        "The group accepted this release process.",
    ]
    events: list[dict[str, object]] = [
        _event(
            "conversation",
            "conversation-1",
            "Release control discussion",
            "2026-08-12T13:00:00Z",
            title="Release Control Discussion",
        ),
        _event(
            "meeting",
            "meeting-1",
            "Release control review",
            "2026-08-12T14:00:00Z",
            parent=("conversation", "conversation-1"),
            title="Release Control Review",
            links=[
                {
                    "relation": "related-chat",
                    "target": {
                        "entity": "conversation",
                        "external_id": "conversation-1",
                    },
                },
                {
                    "relation": "contains",
                    "target": {
                        "entity": "transcript",
                        "external_id": "transcript-1",
                    },
                },
            ],
        ),
    ]
    events.extend(
        _event(
            "message",
            f"message-{index}",
            text,
            f"2026-08-12T13:{index:02d}:00Z",
            parent=("conversation", "conversation-1"),
        )
        for index, text in enumerate(messages, start=1)
    )
    events.append(
        _event(
            "transcript",
            "transcript-1",
            "Synthetic transcript source",
            "2026-08-12T14:00:00Z",
            parent=("meeting", "meeting-1"),
            title="Release Control Review Transcript",
        )
    )
    events.extend(
        _event(
            "transcript-cue",
            f"cue-{index}",
            text,
            f"2026-08-12T14:{index:02d}:00Z",
            parent=("transcript", "transcript-1"),
        )
        for index, text in enumerate(cues, start=1)
    )
    manifest = {
        "schema": "ai-memory/artifact-batch@1",
        "record": "batch",
        "batch_id": "artifact-e2e-batch-1",
        "source": "teams",
        "source_instance": "workspace",
        "observed_at": "2026-08-12T15:00:00Z",
        "event_count": len(events),
    }
    return "\n".join(
        json.dumps(record, sort_keys=True, separators=(",", ":"))
        for record in [manifest, *events]
    ) + "\n"


def _meeting_note(candidate, first_cue: str, second_cue: str) -> str:
    return f"""---
type: memory
memory_id: mem-release-control-review
title: Release Control Review
root_scope: work
primary_scope:
  kind: reference
  id: artifact:{candidate.artifact_id}
status: active
created: 2026-08-12
updated: 2026-08-12
artifact_kind: meeting
source_artifact: {candidate.artifact_uri}
distilled_through_event: {candidate.latest_event_id}
source_digest: {candidate.source_digest}
related: []
provenance:
  - source: artifact-store
    reference: {candidate.artifact_uri}
    verified: 2026-08-12
---
# Release Control Review

%% ai-memory:distilled-begin %%

The meeting selected one production release control and assigned two checks.

## Decisions

- Use the amber release gate for every production rollout.
- Record approval before deployment and verify the rollback command.

## Evidence

> “Use the amber release gate for every production rollout.”
>
> Source: [decision cue]({first_cue})

> “Verify the rollback command before the change window.”
>
> Source: [verification cue]({second_cue})

%% ai-memory:distilled-end %%

## Manual notes

Keep this section outside the managed region.
"""


def test_batch_to_distilled_markdown_recall_is_idempotent(
    artifact_settings: Settings,
) -> None:
    batch = read_artifact_batch(StringIO(_jsonl_batch()))
    receipt = ingest_artifact_batch(artifact_settings, batch)
    assert receipt.accepted == 17
    store = ArtifactStore(artifact_settings)
    assert store.count() == 17

    search = ArtifactSearch(artifact_settings)
    hits = search.search(
        '"amber release gate for every production rollout"',
        scope=ArtifactScope(source="teams", entities=("message",)),
    )
    assert hits[0].external_id == "message-4"
    context = search.read(hits[0].artifact_uri, direction="around", limit=6)
    assert [record.text for record in context.records] == [
        "We need one release control for production changes.",
        "The release owner will record the approval before deployment.",
        "The operations reviewer will verify the rollback command.",
        "Use the amber release gate for every production rollout.",
        "The team will test the gate in the staging environment.",
        "The next review will confirm the first production result.",
    ]

    meeting_reference = artifact_uri(
        "meeting",
        artifact_id("teams", "workspace", "meeting", "meeting-1"),
    )
    meeting_context = search.read(meeting_reference, limit=30)
    assert {record.entity for record in meeting_context.records} == {
        "conversation",
        "meeting",
        "message",
        "transcript",
        "transcript-cue",
    }

    candidate = list_pending_distillations(
        artifact_settings,
        scope=ArtifactScope(source="teams", entities=("meeting",)),
        limit=1,
    )[0]
    first_cue = artifact_uri(
        "transcript-cue",
        artifact_id("teams", "workspace", "transcript-cue", "cue-2"),
    )
    second_cue = artifact_uri(
        "transcript-cue",
        artifact_id("teams", "workspace", "transcript-cue", "cue-5"),
    )
    relative = Path("References/Meetings/2026/release-control-review.md")
    meeting_note = artifact_settings.memory_root / relative
    meeting_note.parent.mkdir(parents=True)
    meeting_note.write_text(
        _meeting_note(candidate, first_cue, second_cue),
        encoding="utf-8",
    )

    service = MemoryService(artifact_settings)
    sync = service.sync()
    assert sync.ok is True
    assert sync.errors == []
    mark_distilled(
        artifact_settings,
        artifact_uri=candidate.artifact_uri,
        memory_id="mem-release-control-review",
        memory_source_id=artifact_settings.primary_source_id,
        memory_path=relative.as_posix(),
        event_id=candidate.latest_event_id,
        source_digest=candidate.source_digest,
    )
    recalled = service.recall("amber release gate production rollout")
    assert recalled.status == "answered"
    assert recalled.evidence[0].evidence_class == "distilled"
    assert recalled.evidence[0].memory_id == "mem-release-control-review"

    before_count = store.count()
    before_events = sum(
        store.event_count(
            artifact_id("teams", "workspace", entity, external_id)
        )
        for entity, external_id in [
            ("conversation", "conversation-1"),
            ("meeting", "meeting-1"),
            *[("message", f"message-{index}") for index in range(1, 7)],
            ("transcript", "transcript-1"),
            *[("transcript-cue", f"cue-{index}") for index in range(1, 9)],
        ]
    )
    replay = ingest_artifact_batch(artifact_settings, batch)
    assert replay.status == "ok"
    assert store.count() == before_count
    after_events = sum(
        store.event_count(
            artifact_id("teams", "workspace", entity, external_id)
        )
        for entity, external_id in [
            ("conversation", "conversation-1"),
            ("meeting", "meeting-1"),
            *[("message", f"message-{index}") for index in range(1, 7)],
            ("transcript", "transcript-1"),
            *[("transcript-cue", f"cue-{index}") for index in range(1, 9)],
        ]
    )
    assert after_events == before_events

    markdown = meeting_note.read_text(encoding="utf-8")
    assert "## Transcript" not in markdown
    assert markdown.count("artifact://transcript-cue/") == 2
