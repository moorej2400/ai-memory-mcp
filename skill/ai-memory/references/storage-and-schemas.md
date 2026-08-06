# Storage and Schema Contract

Read this reference only when choosing a new canonical path, creating or changing a note schema, linking across roots, or handling identity and provenance.

## Canonical Layout

Create folders lazily under the selected configured memory root:

```text
<memory-root>/
  AI Memory.md
  Indexes/
    Memory Map.md
    Repo Map.md
    Project Map.md
    Review Queue.md
    Stale Memory.md
    Conflicts.md
  Repos/
    <repo-key>/
      _repo.md
      Notes/
      Tickets/
        <ticket-id>/
          _ticket.md
          Notes/
          Sessions/
          Handoffs/
  Projects/
    <project-key>/
      _project.md
      Notes/
  Areas/
    Finance/
    Immigration/
    Health/
    Travel/
    Home/
    Learning/
    Relationships/
    Other/
  People/
  Tools/
  Workflows/
  Decisions/
  References/
  Templates/
  Skills/
    Custom/
      <skill-name>/
        SKILL.md
        references/
        templates/
        scripts/
        assets/
    Candidates/
```

Use `Projects/<project-key>/_project.md` for all new project anchors. Keep flat legacy project notes in place until a separately approved migration.

## Stable Identity

- `memory_id`: globally unique durable-note identity, independent of path and title.
- `repo_id`: normalized remote identity such as `github:owner/repository`; otherwise an explicit local-only identity.
- `ticket_id`: tracker-native identity such as `jira:DEMO-1430`.
- `project_id`: stable project identity independent of display title.
- `promotion_id`: `session_id:entry_id` when promoted from a session entry.
- `skill_name`: lowercase hyphenated canonical skill identity matching its folder.

Never use a repository basename alone as canonical identity. Account for forks, mirrors, worktrees, renames, and local-only repositories.

## Structural Anchors

Use `_repo.md`, `_ticket.md`, and `_project.md` only as navigation and scope anchors:

```yaml
---
type: memory-anchor
anchor_kind: repo | ticket | project
memory_id: mem-<unique-id>
scope_id: github:owner/repository | jira:DEMO-1430 | project:<stable-id>
title: Human-readable name
status: active | closed | archived | needs-review
created: YYYY-MM-DD
updated: YYYY-MM-DD
related: []
provenance: []
---
```

The structural filename is an explicit exception to filename/title/H1 alignment. Keep the frontmatter `title` and H1 aligned. Put detailed facts in child notes.

## Ordinary Notes

Use this contract for durable memory, new sessions, handoffs, and indexes:

```yaml
---
type: memory | ai-session | handoff | memory-index
memory_id: mem-<unique-id>
title: Human-readable title
root_scope: work | personal
primary_scope:
  kind: repo | ticket | project | area | tool | person | decision | reference
  id: stable-scope-id
status: active | needs-review | superseded | archived
created: YYYY-MM-DD
updated: YYYY-MM-DD
review_after: null
related: []
provenance: []
supersedes: []
superseded_by: null
---
```

For ordinary notes, keep the filename, `title`, and H1 aligned. Assign exactly one primary scope and link upward to the applicable anchor or index.

### Identifier Extraction

The full-text index contains the `title`, the headings, the body text, and the extracted identifiers. It does not contain the other frontmatter fields. A term in `related_tools` or `related_projects` alone is not searchable.

The index extracts identifiers from the complete file automatically. It finds ticket identifiers, pull-request references, and file paths. The frontmatter has no separate keyword field.

Write each name, alias, and exact string into the title, a heading, or the body.

## Session and Handoff Additions

For `type: ai-session`, add stable `session_id` and concise source-thread metadata when available. Preserve only useful sections such as:

- Current State
- Task Specification
- Important Files and Systems
- Decisions and Corrections
- Durable Candidates
- Promotion Entries

Use promotion-entry provenance in the form `session_id:entry_id`. Reprocessing that value must update or no-op.

For `type: handoff`, keep the body limited to:

- Purpose
- Current State
- Important Artifacts
- Decisions
- Blockers
- Next Action

## Provenance

Keep provenance short and auditable. Include only what helps a future agent judge the claim:

- source session, task, ticket, document, or verified system
- promotion ID when available
- verification date or method
- whether the change was user-requested or automatically captured
- reason the content is durable

Do not copy transcripts, long logs, or full promotion entries into durable notes.

## Link Grammar

- Use path-qualified same-root Obsidian links, such as `[[Repos/github--owner--repository/_repo|repository]]`.
- Never use bare anchor links like `[[_repo]]`, `[[_ticket]]`, or `[[_project]]`.
- Use `memory://<root-scope>/<memory-id>` for cross-root or legacy references.
- Add `source_path` and `source_vault` to provenance for portable cross-root references.
- Link managed skills to canonical Obsidian `SKILL.md` files, never to harness stubs.

## Deduplication and Supersession

- Match by stable scope, canonical identity, provenance, and promotion ID before considering title similarity.
- Reprocessing one promotion may target multiple notes only when the routing record explicitly lists each target.
- Merge overlapping active notes when one precise note can own the claim.
- Preserve unresolved contradictions and mark them `needs-review`.
- When resolved, update both predecessor and successor links before marking the predecessor `superseded`.
- Never delete the predecessor during ordinary consolidation.

## Index Updates

Update only indexes affected by the change:

- `Memory Map.md` for top-level discovery
- `Repo Map.md` for repository anchors
- `Project Map.md` for projects
- `Review Queue.md` for unresolved candidates
- `Stale Memory.md` for review-due facts
- `Conflicts.md` for contradictory active claims

Do not rebuild unrelated indexes for a narrow write.
