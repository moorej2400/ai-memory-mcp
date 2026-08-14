from __future__ import annotations

from pathlib import Path

import pytest

from ai_memory_mcp.artifacts.distillation import (
    list_pending_distillations,
    mark_distilled,
    mark_no_durable_memory,
    recommended_distilled_note_path,
    replace_managed_distillation,
)
from ai_memory_mcp.artifacts.identity import artifact_id, artifact_uri
from ai_memory_mcp.artifacts.models import (
    ArtifactBatchManifest,
    ArtifactEvent,
    ArtifactPayload,
    ArtifactReference,
    ArtifactScope,
    ParsedArtifactBatch,
    RedactionPayload,
)
from ai_memory_mcp.artifacts.store import ArtifactStore
from ai_memory_mcp.config import Settings


def _event(
    entity: str,
    external_id: str,
    text: str,
    *,
    occurred_at: str,
    parent: tuple[str, str] | None = None,
    operation: str = "upsert",
) -> ArtifactEvent:
    if operation == "redact":
        payload: ArtifactPayload | RedactionPayload | None = RedactionPayload(
            reason="Source privacy request"
        )
    elif operation == "delete":
        payload = None
    else:
        payload = ArtifactPayload(
            title="Review Meeting" if entity == "meeting" else None,
            text=text,
            occurred_at=occurred_at,
            content_format="plain",
        )
    return ArtifactEvent.model_validate(
        {
            "schema": "ai-memory/artifact-event@1",
            "record": "event",
            "entity": entity,
            "operation": operation,
            "external_id": external_id,
            "parent": (
                ArtifactReference(entity=parent[0], external_id=parent[1])
                if parent
                else None
            ),
            "source_updated_at": occurred_at,
            "payload": payload,
        }
    )


def _batch(
    batch_id: str,
    events: list[ArtifactEvent],
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
        input_sha256=(batch_id * 64)[:64],
    )


def _pending_meeting(settings: Settings):
    events = [
        _event(
            "meeting",
            "meeting-1",
            "Review meeting",
            occurred_at="2026-01-02T09:00:00Z",
        ),
        _event(
            "transcript",
            "transcript-1",
            "Transcript",
            occurred_at="2026-01-02T09:00:00Z",
            parent=("meeting", "meeting-1"),
        ),
        _event(
            "transcript-cue",
            "cue-1",
            "Use the green setting for new deployments.",
            occurred_at="2026-01-02T09:15:00Z",
            parent=("transcript", "transcript-1"),
        ),
    ]
    ArtifactStore(settings).apply_batch(_batch("meeting-batch-1", events))
    return list_pending_distillations(settings, limit=10)[0]


def _pending_conversation(settings: Settings):
    events = [
        _event(
            "conversation",
            "conversation-1",
            "Scheduling conversation",
            occurred_at="2026-01-02T10:00:00Z",
        ),
        _event(
            "message",
            "message-1",
            "Can we meet tomorrow?",
            occurred_at="2026-01-02T10:01:00Z",
            parent=("conversation", "conversation-1"),
        ),
    ]
    ArtifactStore(settings).apply_batch(_batch("conversation-batch-1", events))
    return list_pending_distillations(settings, limit=10)[0]


def _meeting_note(candidate, cue_uri: str) -> str:
    return f"""---
type: memory
memory_id: mem-review-meeting
title: Review Meeting
root_scope: work
primary_scope:
  kind: reference
  id: artifact:{candidate.artifact_id}
status: active
created: 2026-01-02
updated: 2026-01-02
artifact_kind: meeting
source_artifact: {candidate.artifact_uri}
distilled_through_event: {candidate.latest_event_id}
source_digest: {candidate.source_digest}
related: []
provenance:
  - source: artifact-store
    reference: {candidate.artifact_uri}
    verified: 2026-01-02
---
# Review Meeting

%% ai-memory:distilled-begin %%

The meeting resolved the configuration question and assigned validation.

## Decisions

- Use the green setting for new deployments.

## Evidence

> “Use the green setting for new deployments.”
>
> Source: [transcript cue]({cue_uri})

%% ai-memory:distilled-end %%

## Manual notes

Keep this manual material.
"""


def test_pending_list_applies_scope_and_recommends_a_safe_path(
    artifact_settings: Settings,
) -> None:
    candidate = _pending_meeting(artifact_settings)
    scoped = list_pending_distillations(
        artifact_settings,
        scope=ArtifactScope(
            source="chat-source",
            source_instance="workspace",
            entities=("meeting",),
        ),
        limit=10,
    )
    assert scoped == [candidate]
    path = recommended_distilled_note_path(candidate)
    assert path.as_posix().startswith("References/Meetings/2026/")
    assert ".." not in path.parts
    assert candidate.artifact_id.removeprefix("art_")[:10] in path.name

    unsafe_title = candidate.model_copy(update={"title": "../../Injected\\Title"})
    unsafe_path = recommended_distilled_note_path(unsafe_title)
    assert ".." not in unsafe_path.parts
    assert "\\" not in unsafe_path.as_posix()


def test_meeting_requires_a_markdown_note_before_completion(
    artifact_settings: Settings,
) -> None:
    candidate = _pending_meeting(artifact_settings)
    with pytest.raises(ValueError, match="Markdown note"):
        mark_distilled(
            artifact_settings,
            artifact_uri=candidate.artifact_uri,
            memory_id="mem-review-meeting",
            memory_source_id="core",
            memory_path="References/Meetings/Review Meeting.md",
            event_id=candidate.latest_event_id,
            source_digest=candidate.source_digest,
        )


@pytest.mark.parametrize("operation", ["delete", "redact"])
def test_inactive_parent_suppresses_pending_and_meeting_completion(
    artifact_settings: Settings,
    operation: str,
) -> None:
    store = ArtifactStore(artifact_settings)
    store.apply_batch(
        _batch(
            "meeting-parent-initial",
            [
                _event(
                    "conversation",
                    "meeting-parent-conversation",
                    "Conversation",
                    occurred_at="2026-01-02T09:00:00Z",
                ),
                _event(
                    "meeting",
                    "meeting-with-parent",
                    "Review meeting",
                    occurred_at="2026-01-02T09:05:00Z",
                    parent=("conversation", "meeting-parent-conversation"),
                ),
            ],
        )
    )
    candidate = next(
        item
        for item in list_pending_distillations(artifact_settings, limit=10)
        if item.entity == "meeting"
    )
    relative = Path("References/Meetings/Inactive Parent.md")
    note = artifact_settings.memory_root / relative
    note.parent.mkdir(parents=True)
    note.write_text(
        _meeting_note(candidate, candidate.artifact_uri),
        encoding="utf-8",
    )

    store.apply_batch(
        _batch(
            f"meeting-parent-{operation}",
            [
                _event(
                    "conversation",
                    "meeting-parent-conversation",
                    "",
                    occurred_at="2026-01-02T10:00:00Z",
                    operation=operation,
                )
            ],
        )
    )

    assert candidate.artifact_id not in {
        item.artifact_id
        for item in list_pending_distillations(artifact_settings, limit=10)
    }
    with pytest.raises(ValueError, match="current distillation candidate"):
        mark_distilled(
            artifact_settings,
            artifact_uri=candidate.artifact_uri,
            memory_id="mem-inactive-parent",
            memory_source_id="core",
            memory_path=relative.as_posix(),
            event_id=candidate.latest_event_id,
            source_digest=candidate.source_digest,
        )


def test_valid_meeting_note_can_mark_the_current_source_distilled(
    artifact_settings: Settings,
) -> None:
    candidate = _pending_meeting(artifact_settings)
    relative = Path("References/Meetings/Review Meeting.md")
    path = artifact_settings.memory_root / relative
    path.parent.mkdir(parents=True)
    cue_uri = artifact_uri(
        "transcript-cue",
        artifact_id(
            "chat-source",
            "workspace",
            "transcript-cue",
            "cue-1",
        ),
    )
    path.write_text(_meeting_note(candidate, cue_uri), encoding="utf-8")

    mark_distilled(
        artifact_settings,
        artifact_uri=candidate.artifact_uri,
        memory_id="mem-review-meeting",
        memory_source_id="core",
        memory_path=relative.as_posix(),
        event_id=candidate.latest_event_id,
        source_digest=candidate.source_digest,
    )
    assert list_pending_distillations(artifact_settings, limit=10) == []


def test_stale_distillation_cannot_clear_new_source_work(
    artifact_settings: Settings,
) -> None:
    candidate = _pending_conversation(artifact_settings)
    newer = _event(
        "message",
        "message-2",
        "A later durable decision.",
        occurred_at="2026-01-02T10:02:00Z",
        parent=("conversation", "conversation-1"),
    )
    ArtifactStore(artifact_settings).apply_batch(
        _batch("conversation-batch-2", [newer])
    )
    with pytest.raises(ValueError, match="changed"):
        mark_no_durable_memory(
            artifact_settings,
            artifact_uri=candidate.artifact_uri,
            event_id=candidate.latest_event_id,
            source_digest=candidate.source_digest,
            reason="Only scheduling messages were present.",
        )


def test_no_durable_memory_is_limited_to_conversations(
    artifact_settings: Settings,
) -> None:
    meeting = _pending_meeting(artifact_settings)
    with pytest.raises(ValueError, match="conversation"):
        mark_no_durable_memory(
            artifact_settings,
            artifact_uri=meeting.artifact_uri,
            event_id=meeting.latest_event_id,
            source_digest=meeting.source_digest,
            reason="No durable content.",
        )


def test_note_validation_rejects_transcript_like_content_and_missing_evidence(
    artifact_settings: Settings,
) -> None:
    candidate = _pending_meeting(artifact_settings)
    relative = Path("References/Meetings/Review Meeting.md")
    path = artifact_settings.memory_root / relative
    path.parent.mkdir(parents=True)
    turns = "\n".join(f"09:{index:02d} Person: Full turn" for index in range(12))
    path.write_text(
        _meeting_note(candidate, "artifact://transcript-cue/art_" + "a" * 32)
        .replace("## Evidence", "## Notes")
        .replace("The meeting resolved", turns + "\n\nThe meeting resolved"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="transcript|Evidence"):
        mark_distilled(
            artifact_settings,
            artifact_uri=candidate.artifact_uri,
            memory_id="mem-review-meeting",
            memory_source_id="core",
            memory_path=relative.as_posix(),
            event_id=candidate.latest_event_id,
            source_digest=candidate.source_digest,
        )


@pytest.mark.parametrize(
    "heading",
    [
        "### TrAnScRiPt",
        "## **Transcript**",
        "## __Transcript__:",
        "## `Transcript`",
    ],
)
def test_note_validation_rejects_a_short_transcript_section(
    artifact_settings: Settings,
    heading: str,
) -> None:
    candidate = _pending_meeting(artifact_settings)
    relative = Path("References/Meetings/Short Transcript Section.md")
    path = artifact_settings.memory_root / relative
    path.parent.mkdir(parents=True)
    cue_uri = artifact_uri(
        "transcript-cue",
        artifact_id("chat-source", "workspace", "transcript-cue", "cue-1"),
    )
    note = _meeting_note(candidate, cue_uri).replace(
        "## Evidence",
        f"{heading}\n\nPerson: One short turn.\n\n## Evidence",
    )
    path.write_text(note, encoding="utf-8")

    with pytest.raises(ValueError, match="Transcript section"):
        mark_distilled(
            artifact_settings,
            artifact_uri=candidate.artifact_uri,
            memory_id="mem-review-meeting",
            memory_source_id="core",
            memory_path=relative.as_posix(),
            event_id=candidate.latest_event_id,
            source_digest=candidate.source_digest,
        )


@pytest.mark.parametrize(
    ("field", "old", "new"),
    [
        ("type", "type: memory", "type: note"),
        ("title", "title: Review Meeting", "title: ''"),
        ("root_scope", "root_scope: work", "root_scope: ''"),
        ("primary_scope", "  kind: reference", "  kind: project"),
        (
            "primary_scope",
            "  id: artifact:{artifact_id}",
            "  id: artifact:art_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ),
        ("status", "status: active", "status: draft"),
        ("created", "created: 2026-01-02", "created: not-a-date"),
        ("updated", "updated: 2026-01-02", "updated: not-a-date"),
        (
            "artifact_kind",
            "artifact_kind: meeting",
            "artifact_kind: conversation",
        ),
        ("related", "related: []", "related: {}"),
        (
            "provenance",
            "  - source: artifact-store",
            "  - source: other-store",
        ),
        (
            "provenance",
            "    reference: {artifact_uri}",
            "    reference: artifact://meeting/art_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ),
        (
            "provenance",
            "    verified: 2026-01-02",
            "    verified: not-a-date",
        ),
    ],
)
def test_note_validation_rejects_invalid_required_frontmatter(
    artifact_settings: Settings,
    field: str,
    old: str,
    new: str,
) -> None:
    candidate = _pending_meeting(artifact_settings)
    relative = Path(f"References/Meetings/Invalid {field}.md")
    path = artifact_settings.memory_root / relative
    path.parent.mkdir(parents=True)
    cue_uri = artifact_uri(
        "transcript-cue",
        artifact_id("chat-source", "workspace", "transcript-cue", "cue-1"),
    )
    note = _meeting_note(candidate, cue_uri)
    old_value = old.format(
        artifact_id=candidate.artifact_id,
        artifact_uri=candidate.artifact_uri,
    )
    assert old_value in note
    path.write_text(note.replace(old_value, new), encoding="utf-8")

    with pytest.raises(ValueError, match=field):
        mark_distilled(
            artifact_settings,
            artifact_uri=candidate.artifact_uri,
            memory_id="mem-review-meeting",
            memory_source_id="core",
            memory_path=relative.as_posix(),
            event_id=candidate.latest_event_id,
            source_digest=candidate.source_digest,
        )


def test_note_validation_rejects_a_fully_blockquoted_transcript(
    artifact_settings: Settings,
) -> None:
    candidate = _pending_meeting(artifact_settings)
    relative = Path("References/Meetings/Quoted Transcript.md")
    path = artifact_settings.memory_root / relative
    path.parent.mkdir(parents=True)
    cue_uri = artifact_uri(
        "transcript-cue",
        artifact_id(
            "chat-source", "workspace", "transcript-cue", "cue-1"
        ),
    )
    quoted_turns = "\n".join(
        f"> 09:{index:02d} Person: Full transcript turn {index}."
        for index in range(12)
    )
    note = _meeting_note(candidate, cue_uri).replace(
        '> “Use the green setting for new deployments.”',
        quoted_turns,
    )
    path.write_text(note, encoding="utf-8")

    with pytest.raises(ValueError, match="quotation|transcript"):
        mark_distilled(
            artifact_settings,
            artifact_uri=candidate.artifact_uri,
            memory_id="mem-review-meeting",
            memory_source_id="core",
            memory_path=relative.as_posix(),
            event_id=candidate.latest_event_id,
            source_digest=candidate.source_digest,
        )


def test_meeting_evidence_link_must_be_inside_the_evidence_section(
    artifact_settings: Settings,
) -> None:
    candidate = _pending_meeting(artifact_settings)
    relative = Path("References/Meetings/Misplaced Evidence.md")
    path = artifact_settings.memory_root / relative
    path.parent.mkdir(parents=True)
    cue_uri = artifact_uri(
        "transcript-cue",
        artifact_id(
            "chat-source", "workspace", "transcript-cue", "cue-1"
        ),
    )
    note = _meeting_note(candidate, cue_uri).replace(
        "## Decisions\n\n- Use the green setting for new deployments.",
        "## Decisions\n\n"
        f"- Use the green setting. [Source]({cue_uri})",
    ).replace(f"> Source: [transcript cue]({cue_uri})", "> Source unavailable")
    path.write_text(note, encoding="utf-8")

    with pytest.raises(ValueError, match="[Ee]vidence"):
        mark_distilled(
            artifact_settings,
            artifact_uri=candidate.artifact_uri,
            memory_id="mem-review-meeting",
            memory_source_id="core",
            memory_path=relative.as_posix(),
            event_id=candidate.latest_event_id,
            source_digest=candidate.source_digest,
        )


def test_meeting_evidence_requires_a_quotation_not_only_a_source_link(
    artifact_settings: Settings,
) -> None:
    candidate = _pending_meeting(artifact_settings)
    cue_uri = artifact_uri(
        "transcript-cue",
        artifact_id(
            "chat-source", "workspace", "transcript-cue", "cue-1"
        ),
    )
    relative = Path("References/Meetings/Link Only Evidence.md")
    path = artifact_settings.memory_root / relative
    path.parent.mkdir(parents=True)
    note = _meeting_note(candidate, cue_uri).replace(
        '> “Use the green setting for new deployments.”\n>\n',
        "",
    )
    path.write_text(note, encoding="utf-8")

    with pytest.raises(ValueError, match="quoted evidence|quotation"):
        mark_distilled(
            artifact_settings,
            artifact_uri=candidate.artifact_uri,
            memory_id="mem-review-meeting",
            memory_source_id="core",
            memory_path=relative.as_posix(),
            event_id=candidate.latest_event_id,
            source_digest=candidate.source_digest,
        )


def test_every_managed_artifact_link_must_be_inside_evidence(
    artifact_settings: Settings,
) -> None:
    candidate = _pending_meeting(artifact_settings)
    cue_uri = artifact_uri(
        "transcript-cue",
        artifact_id(
            "chat-source", "workspace", "transcript-cue", "cue-1"
        ),
    )
    unrelated_uri = artifact_uri(
        "message",
        artifact_id("chat-source", "workspace", "message", "misplaced-link"),
    )
    relative = Path("References/Meetings/Extra Misplaced Link.md")
    path = artifact_settings.memory_root / relative
    path.parent.mkdir(parents=True)
    note = _meeting_note(candidate, cue_uri).replace(
        "- Use the green setting for new deployments.",
        f"- Use the green setting. [Misplaced source]({unrelated_uri})",
    )
    path.write_text(note, encoding="utf-8")

    with pytest.raises(ValueError, match="Evidence"):
        mark_distilled(
            artifact_settings,
            artifact_uri=candidate.artifact_uri,
            memory_id="mem-review-meeting",
            memory_source_id="core",
            memory_path=relative.as_posix(),
            event_id=candidate.latest_event_id,
            source_digest=candidate.source_digest,
        )


def test_meeting_evidence_link_must_belong_to_the_meeting_context(
    artifact_settings: Settings,
) -> None:
    candidate = _pending_meeting(artifact_settings)
    ArtifactStore(artifact_settings).apply_batch(
        _batch(
            "unrelated-batch",
            [
                _event(
                    "conversation",
                    "conversation-unrelated",
                    "Unrelated conversation",
                    occurred_at="2026-01-03T10:00:00Z",
                ),
                _event(
                    "message",
                    "message-unrelated",
                    "Unrelated message",
                    occurred_at="2026-01-03T10:01:00Z",
                    parent=("conversation", "conversation-unrelated"),
                ),
            ],
        )
    )
    unrelated_uri = artifact_uri(
        "message",
        artifact_id(
            "chat-source", "workspace", "message", "message-unrelated"
        ),
    )
    relative = Path("References/Meetings/Unrelated Evidence.md")
    path = artifact_settings.memory_root / relative
    path.parent.mkdir(parents=True)
    path.write_text(_meeting_note(candidate, unrelated_uri), encoding="utf-8")

    with pytest.raises(ValueError, match="context|evidence"):
        mark_distilled(
            artifact_settings,
            artifact_uri=candidate.artifact_uri,
            memory_id="mem-review-meeting",
            memory_source_id="core",
            memory_path=relative.as_posix(),
            event_id=candidate.latest_event_id,
            source_digest=candidate.source_digest,
        )


@pytest.mark.parametrize(
    "memory_path",
    [
        "../outside.md",
        "References\\Meetings\\Review.md",
        "/absolute/Review.md",
        "C:/absolute/Review.md",
    ],
)
def test_completion_rejects_unsafe_paths(
    artifact_settings: Settings,
    memory_path: str,
) -> None:
    candidate = _pending_meeting(artifact_settings)
    with pytest.raises(ValueError, match="path"):
        mark_distilled(
            artifact_settings,
            artifact_uri=candidate.artifact_uri,
            memory_id="mem-review-meeting",
            memory_source_id="core",
            memory_path=memory_path,
            event_id=candidate.latest_event_id,
            source_digest=candidate.source_digest,
        )


def test_completion_rejects_case_folded_path_collision(
    artifact_settings: Settings,
) -> None:
    candidate = _pending_meeting(artifact_settings)
    existing = artifact_settings.memory_root / "References/Meetings/Review.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("existing", encoding="utf-8")
    with pytest.raises(ValueError, match="case"):
        mark_distilled(
            artifact_settings,
            artifact_uri=candidate.artifact_uri,
            memory_id="mem-review-meeting",
            memory_source_id="core",
            memory_path="references/meetings/review.md",
            event_id=candidate.latest_event_id,
            source_digest=candidate.source_digest,
        )


def test_completion_rejects_case_folded_uppercase_extension_collision(
    artifact_settings: Settings,
) -> None:
    candidate = _pending_meeting(artifact_settings)
    existing = artifact_settings.memory_root / "References/Meetings/Review.MD"
    existing.parent.mkdir(parents=True)
    existing.write_text("existing", encoding="utf-8")
    with pytest.raises(ValueError, match="case"):
        mark_distilled(
            artifact_settings,
            artifact_uri=candidate.artifact_uri,
            memory_id="mem-review-meeting",
            memory_source_id="core",
            memory_path="references/meetings/review.md",
            event_id=candidate.latest_event_id,
            source_digest=candidate.source_digest,
        )


def test_managed_region_replacement_preserves_manual_content() -> None:
    original = """# Note

%% ai-memory:distilled-begin %%
Old managed text.
%% ai-memory:distilled-end %%

## Manual notes

Keep this exact text.
"""
    updated = replace_managed_distillation(original, "New managed text.")
    assert "Old managed text." not in updated
    assert "New managed text." in updated
    assert updated.endswith("## Manual notes\n\nKeep this exact text.\n")
    with pytest.raises(ValueError, match="marker"):
        replace_managed_distillation(
            original,
            "Injected %% ai-memory:distilled-end %% marker",
        )


def test_managed_region_rejects_duplicate_or_reversed_markers() -> None:
    duplicate = """%% ai-memory:distilled-begin %%
One
%% ai-memory:distilled-begin %%
Two
%% ai-memory:distilled-end %%
"""
    reversed_markers = """%% ai-memory:distilled-end %%
Text
%% ai-memory:distilled-begin %%
"""
    with pytest.raises(ValueError, match="marker"):
        replace_managed_distillation(duplicate, "New")
    with pytest.raises(ValueError, match="marker"):
        replace_managed_distillation(reversed_markers, "New")
