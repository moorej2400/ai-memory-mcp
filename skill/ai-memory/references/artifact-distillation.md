# Artifact Distillation

Use this workflow for pending meeting and conversation artifacts.
SQLite is the authority for the complete source material.
Markdown is the authority for the durable summary.

## Workflow

1. List pending artifacts with `ai-memory-artifact pending`.
2. Read the current Markdown note before any write.
3. Read source context with `memory_artifact_read`.
4. Remove greetings, banter, repetition, and unrelated discussion.
5. Preserve all manual Markdown outside the managed region.
6. Replace only the content between the managed markers.
7. Add short quotations with `artifact://` evidence links.
8. Write or update one concise Markdown note.
9. Run `memory_sync` after the Markdown write.
10. Verify recall for the distilled claim.
11. Mark the current event and source digest as distilled.

Do not mark an artifact before the Markdown note and source digest pass validation.
If the source digest changes, read the new source context and update the note.

## Managed region

Use these markers once in each distilled note:

```markdown
%% ai-memory:distilled-begin %%

Managed summary content.

%% ai-memory:distilled-end %%
```

Do not put a marker inside managed content.
Do not change text outside the markers unless the user requests that change.

## Meeting notes

A meeting note contains a summary, decisions, actions, open questions, important context, and evidence when applicable.
The `Evidence` section contains short quotations and stable artifact links.
The note does not contain a transcript section or the complete transcript.

Use this default path:

```text
References/Meetings/<YYYY>/<YYYY-MM-DD>-<safe-title>-<short-artifact-id>.md
```

Derive the stable suffix from the artifact ID.
Do not derive the suffix from the provider external ID.

## Conversation notes

Create a conversation note only when the conversation contains durable information.
Use resolutions, decisions, reusable context, open questions, and evidence when applicable.

Use this default path:

```text
References/Conversations/<safe-title>-<short-artifact-id>.md
```

For greetings, scheduling, acknowledgements, or banter, mark the reviewed conversation as `no-durable-memory`.
Do not use `no-durable-memory` for a meeting.

## Required frontmatter

Add these fields to the ordinary durable-note schema:

```yaml
artifact_kind: meeting
source_artifact: artifact://meeting/<artifact-id>
distilled_through_event: evt_<event-id>
source_digest: <sha256>
```

Use `artifact_kind: conversation` for a conversation note.
Keep artifact-store provenance in the ordinary `provenance` list.
