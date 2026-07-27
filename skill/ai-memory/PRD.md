# Unified Graphify Memory Skill PRD

## Status

Draft for review. This PRD defines the target behavior for one unified memory skill. It does not authorize a migration, retirement of existing skills, or movement of existing notes.

## Summary

The current memory-related skills split overlapping responsibilities across multiple roots, note schemas, and trigger paths. The unified skill will consolidate durable-memory capture, repository and ticket organization, optional session and handoff capture, lifecycle management, and Graphify refresh behavior into one workflow. Automatic capture is the default: during meaningful work, the skill evaluates and saves qualifying durable memories without waiting for the user to ask. Explicit user requests to save memory remain supported and enter the same workflow.

The canonical unified skill source is `skill/ai-memory/SKILL.md` in the `ai-memory-mcp` repository. Codex, Claude, Copilot, OpenCode, and the `j-skills` distribution repository contain only discovery stubs that copy the canonical trigger metadata and redirect to this source.

The existing `graphify-memory` skill is the behavioral backbone because it already uses a live AI-Memory corpus and Graphify refresh path. The final unified skill is named `ai-memory`; it absorbs the useful `graphify-memory` behavior and becomes the only active memory trigger after validation. The final unified skill will preserve the useful behavior from:

- `ai-memory`: landing-note and durable-root discovery.
- `graphify-memory`: concise Markdown write-back, Graphify integration, and refresh discipline.
- `memory-learning-master`: routing, promotion provenance, idempotence, conflict, staleness, and supersession rules.
- `ai-session`: search-before-create and concise session/handoff practices.

`custom-skills-master` remains a separate operational skill. Its canonical `SKILL.md`, and the canonical source for custom skills it creates, will live inside the configured Obsidian memory root at `Skills/Custom/<skill-name>/SKILL.md`. Each configured AI harness receives only a small discovery stub that preserves trigger metadata and redirects to the canonical Obsidian source. The unified memory skill must route procedural learnings to `custom-skills-master` instead of duplicating skill-authoring logic.

## Current Skill Inventory and Planned Consolidation

The unified skill is a consolidation of overlapping memory responsibilities, not a deletion campaign. "Remove" in this PRD means **retire a skill from active memory discovery after its useful behavior is preserved and tested**. The original skill folder and source content remain recoverable unless a future explicit deletion request says otherwise.

| Skill | Current role and context | Behavior retained by the unified skill | Planned outcome |
|---|---|---|---|
| `ai-memory` | The canonical unified memory skill. Its legacy helper behavior identified the durable AI-Memory root and delegated writes to `graphify-memory`; the rebuilt skill now owns the full workflow. | Durable-root discovery, automatic durable capture, retrieval, lifecycle management, sessions and handoffs, skill routing, Graphify discovery, narrow refresh, and honest indexing status. | Keep as the single active memory trigger. Store the canonical source in this repository and install redirect stubs in Codex, Claude, Copilot, OpenCode, and `j-skills`. |
| `graphify-memory` | The former working write-back path for concise AI-Memory Markdown. It is wired to the live Graphify refresh/extraction scripts and defines search-before-write, stale-memory, and refresh discipline. | Merge its durable Markdown capture, Graphify candidate discovery, narrow refresh, and honest indexing behavior into `ai-memory`. | Retire from active discovery after the new `ai-memory` source and harness stubs validate. Move its source folders and harness stubs to recoverable trash; do not remove the Graphify service or refresh scripts. |
| `memory-learning-master` | A richer conceptual lifecycle model: session notes, promotion entries, routing, provenance, idempotence, review, conflicts, and runtime-memory boundaries. | Promotion provenance (`session_id:entry_id`), routing, deduplication, conflict, stale-memory, supersession, and review concepts. | Retire from all harness skill directories and replace its `j-skills` source folder with the `ai-memory` discovery stub after validation. Keep inactive source material recoverable. |
| `ai-session` | The former session/handoff workflow. It uses separate work and personal session roots, has a proven search-before-create pattern, and existing records depend on its frontmatter and directory conventions. | Search-before-create, concise session/handoff structure, controlled properties, and external session provenance. | Retire from all harness skill directories after the unified `ai-memory` source and stubs validate. Preserve existing session records and inactive source material. |
| `custom-skills-master` | A separate procedural-memory skill that decides whether a finding belongs in a new/updated skill, a support file, durable memory, session-only context, or nowhere. | No authoring workflow is copied. The unified skill delegates procedural candidates to this skill and may store a compact pointer to the outcome. | Keep active and independent. Its canonical source is the Obsidian `Skills/Custom/custom-skills-master/SKILL.md`, with synchronized discovery stubs in Codex, Claude, Copilot, and OpenCode. Preserve the former Git-backed source until the pilot passes. |

### Retirement guardrails

- No legacy skill is retired before the unified workflow passes the pilot acceptance criteria in this PRD.
- No folder, `SKILL.md`, session note, or memory note is deleted as part of normal consolidation.
- A redirect must have a deliberately narrow trigger description so it does not compete with the unified skill in discovery.
- No harness stub becomes authoritative, and no existing installed skill is replaced until its canonical Obsidian source is reachable and the stub passes trigger and load verification.
- Retiring the `ai-session` skill must not move, merge, or delete existing session records; only its active source copies and harness stubs are retired.
- `custom-skills-master` remains the sole owner of procedural skill-management decisions.

## Problem

The current system has several gaps:

- Multiple skills can plausibly trigger for the same request to save or recall memory.
- Durable memory, session notes, and Obsidian organization use incompatible root paths and schemas.
- The live AI-Memory vault has no first-class repository or ticket hierarchy.
- A project can be a Git repository, a cross-repository initiative, or a non-code personal project; the system needs to distinguish them cleanly.
- Important non-code areas such as finances, immigration, health, travel, and home/personal projects need structured, durable organization rather than being forced into repository folders.
- Graphify currently indexes several corpora, so query results can include legacy or conflicting sources unless write authority is explicit.
- Personal and work notes must not be silently mixed into a corporate-synced storage location.
- Custom skill source, Obsidian skill records, and harness-installed copies currently have split authority and no single verified deployment contract.

## Goals

- Provide one canonical, discoverable memory workflow for durable Markdown memory.
- Keep `ai-memory` as the only active memory trigger across Codex, Claude, Copilot, and OpenCode.
- Automatically capture qualifying durable learnings during meaningful work and before completion without requiring a user prompt, while also honoring explicit save requests.
- Make Markdown notes the canonical source of truth and Graphify the query, indexing, and relationship-discovery layer.
- Organize work by repository and, when useful, ticket ID.
- Organize non-code personal and work knowledge by project and durable area of life or responsibility.
- Preserve a safe boundary between work and personal data.
- Search before writing, prefer precise updates, and prevent duplicate or conflicting durable notes.
- Preserve short, auditable provenance without storing transcripts or command logs as memory.
- Support optional session and handoff records without forcing a bulk migration of existing session vaults.
- Refresh only the necessary Graphify corpus after material writes and report refresh failure honestly.
- Store canonical executable custom skills inside the configured Obsidian memory root and deploy only synchronized discovery stubs to each configured harness.
- Retire duplicate trigger paths only after the unified workflow has passed a real pilot.

## Non-Goals

- Do not treat Graphify search results as the authority for where a note is written.
- Do not create a second `AI Brain` vault or depend on the currently unconfigured `AI_BRAIN_OBSIDIAN_DIR`.
- Do not automatically migrate, merge, move, or delete existing session notes, skills, or memory notes.
- Do not place personal material in a work or corporate-synced root unless the user explicitly permits it.
- Do not make every ticket, conversation, project, or correction a durable memory record.
- Do not require the user to explicitly request each qualifying durable-memory write.
- Do not replace `custom-skills-master` or embed full skill-authoring procedures in durable memory notes.
- Do not treat a harness discovery stub as the canonical skill source.
- Do not permanently delete legacy skill sources or harness-installed copies; move explicitly retired artifacts to recoverable trash.
- Do not use the global/all-corpora Graphify extractor for routine memory writes.

## Design Principles

1. **One workflow, not one physical location for all data.** The unified skill owns the workflow. Work and personal data may use separate roots when privacy, retention, or sync boundaries require it.
2. **Markdown is canonical.** Graphify discovers candidates and relationships. Before any write, the skill must inspect the canonical Markdown path, status, and provenance.
3. **Scope before storage.** Determine whether knowledge is repository, ticket, cross-repository project, personal project, durable life area, tool, person, decision, reference, session-only, or a skill candidate before choosing a path.
4. **Search before create.** Update, merge, or supersede an existing precise note rather than creating a near-duplicate.
5. **Automatic capture is the baseline.** When the skill is active and the appropriate root is configured, it automatically saves qualifying durable memory without per-write confirmation. An explicit user request is an additional trigger, not a prerequisite.
6. **Personal data is opt-in to personal storage.** A configured personal root enables automatic capture of qualifying personal memory there. Never infer that a personal health, immigration, finance, or travel record belongs in the work root.
7. **Recovery first.** Migration and retirement are staged, reversible, and never delete sources without separate explicit approval.
8. **Small, verified writes.** A memory write is complete only when the note change succeeds. Indexing is a separate verified outcome.
9. **One canonical skill, many discovery stubs.** Executable custom skill content lives once in Obsidian. Harness-local `SKILL.md` files contain only the metadata and redirect instructions required for discovery and loading.

## Storage Roots and Data Boundaries

### Root policy

The final skill must resolve roots through explicit configuration:

- `AI_MEMORY_WORK_DIR`: canonical root for shared/work durable memory. It replaces the practical role currently served by `AI_MEMORY_DIR`.
- `AI_MEMORY_PERSONAL_DIR`: optional canonical root for personal durable memory. It must not default to a corporate-synced path.
- `AI_MEMORY_DIR`: legacy compatibility fallback for work memory only. It must never be interpreted as a personal root.
- `AI_CUSTOM_SKILLS_DIR`: canonical root for executable custom skills. It must resolve inside an explicitly approved Obsidian memory root, normally `<memory-root>/Skills/Custom`, and must not point to a harness installation directory.
- `GRAPHIFY_OPENAI_BASE_URL`, `GRAPHIFY_OPENAI_API_KEY`, `GRAPHIFY_OPENAI_MODEL`, and `GRAPHIFY_OPENAI_TOKEN_BUDGET`: explicit semantic-extraction backend configuration used by the narrow AI-Memory refresh.

If a request is personal and `AI_MEMORY_PERSONAL_DIR` is unset, the skill must stop and ask for a safe target. It must not fall back to the work root.

Harness stub locations must be configured explicitly per platform. The implementation may support Codex, Claude Code, GitHub Copilot CLI, or other harnesses, but it must not guess an installation directory or create a stub in an unconfigured target. If `AI_CUSTOM_SKILLS_DIR` is unset or unsafe, a procedural candidate may be recorded for review, but no canonical skill or harness stub is created.

### Session boundary

Existing work and personal session vaults remain read-only legacy sources until separately audited. The unified skill may reference them through stable source metadata, but it must not move them or assume that their paths are synchronized with Graphify.

New optional session or handoff notes may be written into the appropriate configured root only after the user has chosen the data domain:

- Work session or handoff -> `AI_MEMORY_WORK_DIR`.
- Personal session or handoff -> `AI_MEMORY_PERSONAL_DIR`.

### Legacy-session safety gate

Before the unified skill claims that legacy sessions are searchable or safe to promote, the implementation must audit:

- unresolved `*.conflict*` artifacts;
- drift between the local and network-backed session vaults;
- the exact corpus Graphify indexes versus the corpus `ai-session` writes;
- whether personal sessions may be queried by default.

## Canonical Layout

Each configured root follows the same broad shape where relevant. Folders are created lazily, not as an empty taxonomy dump.

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
        <topic>.md
      Tickets/
        <ticket-id>/
          _ticket.md
          Notes/
            <topic>.md
          Sessions/
            <session-id>.md
          Handoffs/
            <yyyy-mm-dd>-<slug>.md

  Projects/
    <project-key>/
      _project.md
      Notes/
        <topic>.md

  Areas/
    Finance/
      <topic-or-account-context>.md
    Immigration/
      <topic-or-case-context>.md
    Health/
      <topic>.md
    Travel/
      <trip-key>/
        _project.md
    Home/
    Learning/
    Relationships/
    Other/

  Skills/
    Custom/
      <skill-name>/
        SKILL.md
        references/
        templates/
        scripts/
        assets/
    Candidates/
      <skill-name>.md

  People/
  Tools/
  Workflows/
  Decisions/
  References/
  Templates/
```

### Organization rules

| Knowledge type | Canonical location | Notes |
|---|---|---|
| Repository architecture, conventions, stable facts | `Repos/<repo-key>/` | One anchor per canonical repository identity. |
| Ticket-specific implementation context | `Repos/<repo-key>/Tickets/<ticket-id>/` | Create only when there is a durable note, session, or handoff worth retaining. |
| Ticket spanning multiple repositories | A ticket anchor in the primary work context plus explicit links to all repos/projects | Do not duplicate the same durable facts into every repository. |
| Cross-repository initiative | `Projects/<project-key>/` | A project is not a substitute for a repository. |
| Non-code personal project | `Projects/<project-key>/` in the personal root | Examples: home renovation, hobby build, family project, education plan. |
| Ongoing personal area | `Areas/<area>/` in the personal root | Examples: Finance, Immigration, Health, Travel, Home, Learning. |
| Time-bound vacation or trip | `Areas/Travel/<trip-key>/` | Treat as a personal project with an anchor and scoped notes. |
| Tool behavior or setup | `Tools/` | Only stable, reusable facts or known workarounds. |
| Decision spanning scopes | `Decisions/` | Local decisions stay with the relevant repo/project/ticket and link outward only when cross-cutting. |
| Repeatable procedure | `Skills/Custom/<skill-name>/SKILL.md` through `custom-skills-master` | Memory may retain a short pointer to the resulting canonical skill. |

`Projects/` is the canonical nested convention for all new project records. Flat legacy project notes may remain in place during the pilot, but new records must use `Projects/<project-key>/_project.md`.

### Canonical skill source and harness stubs

- The canonical source for every managed custom skill is `<AI_CUSTOM_SKILLS_DIR>/<skill-name>/SKILL.md` with optional `references/`, `templates/`, `scripts/`, and `assets/` siblings.
- `custom-skills-master` itself follows the same rule. Its canonical source lives at `<AI_CUSTOM_SKILLS_DIR>/custom-skills-master/SKILL.md`.
- A harness-local `<skill-name>/SKILL.md` is a discovery stub, not a second full copy. It contains the canonical skill's trigger metadata plus instructions to validate and load the canonical Obsidian `SKILL.md`.
- Folder names and `name` metadata must match across the canonical source and every stub.
- Creating or materially updating a canonical skill triggers stub synchronization for each configured harness. The workflow validates the canonical skill first, updates only the required stubs, then verifies that every stub resolves and loads the canonical source.
- A canonical write and each harness deployment are reported separately. If stub synchronization fails, report `skill saved, harness not synchronized` for the affected harness; do not claim that the skill is available there.
- Graphify may index canonical skill sources and their relationships. It must not treat harness stubs as separate authoritative skills.
- Existing Git-backed skill sources and installed full copies remain recoverable during the pilot. Authority changes only after content-equivalence and harness-load verification; nothing is deleted as part of this transition.

## Identity, Links, and Schemas

### Immutable identifiers

Paths and display titles can change. The skill must use immutable identities:

- `memory_id`: globally unique identifier for every durable note.
- `repo_id`: normalized remote identity when available, for example `github:owner/repository`; otherwise an explicit local-only identity.
- `ticket_id`: tracker-native ID, preserving its tracker and casing, for example `jira:DEMO-1430`.
- `project_id`: stable project identity independent of its display title.
- `promotion_id`: `session_id:entry_id` when a durable note was promoted from a session entry.

Folder names are derived from stable identities and may include readable aliases, but a repository basename alone is never a unique key. Forks, mirrors, worktrees, renamed repositories, and local-only repositories must not collide.

### Structural anchors

`_repo.md`, `_ticket.md`, and `_project.md` are structural anchors, not general fact notes. They use:

```yaml
---
type: memory-anchor
anchor_kind: repo | ticket | project
memory_id: mem-...
scope_id: github:owner/repository | jira:DEMO-1430 | project:...
title: Human-readable name
status: active | closed | archived | needs-review
created: YYYY-MM-DD
updated: YYYY-MM-DD
related: []
provenance: []
---
```

The anchor filename is an explicit exception to the ordinary filename/title/H1 rule. Its `title` and H1 must agree; its filename remains structural. Anchor notes hold navigation, scope metadata, active-summary context, and links to scoped records. Detailed facts belong in child notes.

### Ordinary note schema

```yaml
---
type: memory | ai-session | handoff | memory-index
memory_id: mem-...
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

For new ordinary notes, filename, frontmatter `title`, and H1 must align. Each ordinary note has exactly one primary scope and links upward to its applicable anchor or area index.

### Link grammar

- Same-root navigation uses path-qualified Obsidian links, for example `[[Repos/github--owner--repo/_repo|repository]]`.
- Anchor links may never use a bare filename such as `[[_repo]]` or `[[_ticket]]`.
- Cross-root or legacy links use portable metadata references, not relative Obsidian links. Format: `memory://<root-scope>/<memory-id>` plus `source_path` and `source_vault` in provenance metadata.
- Graphify results must expose source corpus, source path, note status, and freshness before they are used as a candidate write target.
- Links to managed skills resolve to the canonical Obsidian `SKILL.md`, never to a harness stub.

### Idempotence and duplication

The unified skill must use both identity and source provenance:

- Reprocessing the same `promotion_id` must create no duplicate durable claim.
- Reprocessing a fact with matching canonical scope and provenance should update or no-op after canonical-note validation.
- A `memory_id` identifies a record; it is not inferred from title or path.
- One promotion may intentionally produce multiple scoped notes only when the routing result explicitly records each target. Repeating the promotion must recognize all targets.

## Classification and Write Workflow

1. At meaningful checkpoints and before completing substantive work, automatically determine whether the task produced durable-memory candidates, needs retrieval, needs a session/handoff update, contains a skill candidate, or warrants no write. Do not wait for the user to request memory capture. An explicit save request triggers this evaluation immediately.
2. Determine data domain: work or personal. Never infer personal -> work.
3. Determine primary scope: repository, ticket, project, area, tool, person, decision, reference, session-only, or skill candidate.
4. Query Graphify for likely candidates in the relevant canonical corpus, then inspect the canonical Markdown notes directly.
5. Choose one action: update, create, merge, mark superseded, mark needs-review, route to `custom-skills-master`, retain session-only, or intentionally skip.
6. Automatically write concise Markdown when the candidate is durable and the chosen root is configured and safe. No per-write confirmation is required. Also perform the same write workflow when the user explicitly asks to save a memory.
7. Update review/index surfaces when needed.
8. Run the narrow Graphify refresh once after a material batch, then verify corpus and service health.
9. Report separately: note write result, supersession/merge result, and Graphify indexing result.

### Automatic capture cadence

- Evaluate for durable memory when a meaningful reusable fact, decision, correction, constraint, relationship, or verified workaround is learned.
- Evaluate again before the final response of substantive work so useful memory is not lost merely because the user did not ask to save it.
- Batch related automatic writes and run one narrow Graphify refresh after the material batch.
- Create no note when the information is transient, trivial to rediscover, duplicative, unsafe, or better routed to a skill or session-only record.
- If the appropriate root is missing or unsafe, do not silently fall back to another domain; report the blocked memory capture separately from completion of the user's main task.

## Graphify Integration

### Authority model

- Markdown in the configured canonical root is authoritative for memory content.
- Canonical `Skills/Custom/<skill-name>/SKILL.md` files in the configured Obsidian skill root are authoritative for managed custom skill content; harness stubs are discovery artifacts only.
- Graphify is authoritative only for search, relationships, traversal, and candidate discovery.
- A Graphify result from `ai-sessions`, `legacy-vault`, or another legacy corpus must not override a canonical active memory note.
- During coexistence, the skill must define corpus priority and whether session corpora are searched by default.

### Routine refresh policy

Routine AI-Memory writes must use the configured narrow AI-Memory refresh path, currently expected to be equivalent to:

```powershell
scripts/graphify/refresh-ai-memory-graph.ps1
```

The unified skill must explicitly prohibit routine use of the global/all-corpora extractor. That extractor may only be used through a separate, explicit maintenance workflow.

The narrow refresh is a scoped full-corpus rebuild and MCP restart, not an incremental write. The implementation must define:

- refresh serialization or locking;
- health verification after the restart;
- a material-write threshold that triggers refresh;
- maximum acceptable indexing staleness;
- an honest degraded state: `saved, not indexed` when refresh fails;
- recovery behavior verified against the real scripts, not assumed rollback behavior.

## Lifecycle, Review, and Safety

### Staleness and conflicts

When a new verified claim contradicts an active durable note:

1. Do not silently overwrite the active note.
2. Link both records and preserve their provenance.
3. Mark unresolved material `needs-review`.
4. When resolved, update the predecessor to `superseded`, set `superseded_by`, and set the successor's `supersedes` field.
5. Keep the predecessor readable; never delete it as part of ordinary consolidation.

Use `review_after` for facts likely to drift, including tool availability, local paths, provider behavior, external URLs, and policy-sensitive context.

### Session and handoff records

Session records are optional detailed context, not default durable memory. They may retain promotion entries with `session_id:entry_id` provenance. A handoff should be concise and scoped to the repo, ticket, project, or area it supports.

The skill must not turn all sessions into durable notes, nor load full session archives into runtime context by default.

### Skill boundary

If a finding changes how agents should repeatedly perform a workflow, the unified memory skill routes it to `custom-skills-master`. It may retain only a compact pointer to the related canonical skill or skill candidate. The memory skill must not replicate `custom-skills-master`'s detailed classification workflow.

`custom-skills-master` owns creation and maintenance of `<AI_CUSTOM_SKILLS_DIR>/<skill-name>/SKILL.md`, its support files, and its configured harness stubs. It must search canonical Obsidian skills before creating a new one, validate the canonical source before deployment, synchronize trigger metadata into each stub, and report canonical-save and per-harness deployment outcomes separately.

## Migration and Consolidation Plan

### Phase 0: authority and boundaries (no writes)

- Use this repository as the canonical `ai-memory` source and treat every harness installation plus the `j-skills` distribution copy as a discovery stub.
- Confirm the exact Obsidian memory root that owns `Skills/Custom/` and configure `AI_CUSTOM_SKILLS_DIR` there. Obsidian is the canonical authoring home; harness-local copies are discovery stubs only.
- Inventory the harness skill directories that may receive stubs and configure each target explicitly.
- Decide whether personal memory is supported now and, if so, configure `AI_MEMORY_PERSONAL_DIR` outside the work root.
- Audit and explicitly resolve or quarantine session-vault conflicts and local/network drift.
- Inventory registered Graphify corpora and establish corpus priority and default search scope.

### Phase 1: contracts and unified skill text

- Write the unified `ai-memory` behavior, schemas, root-resolution rules, Graphify authority model, and refresh policy.
- Adjust `custom-skills-master` so it writes canonical skills under `AI_CUSTOM_SKILLS_DIR` and synchronizes verified discovery stubs into configured harness directories.
- Define the stub schema, metadata synchronization rules, deployment failure states, and canonical-source validation.
- Add templates and validation guidance without moving notes or retiring skills.

### Phase 2: empty structure and pilot

- Add the minimum `Repos/`, `Projects/`, `Areas/`, `Indexes/`, and `Skills/Custom/` structure only in the configured pilot root.
- Pilot one repository and one ticket end-to-end.
- Pilot one custom skill end-to-end: canonical creation in Obsidian, stub creation in one configured harness, trigger discovery, and canonical load.
- Include a second identical write to prove idempotence.
- Prove Graphify can retrieve the newly indexed canonical note.

### Phase 3: validated consolidation

- Keep `ai-memory` as the canonical unified skill and the only active memory trigger.
- Retire `graphify-memory` sources and harness stubs to recoverable trash after `ai-memory` validation; keep the Graphify service and refresh scripts active.
- Retire `memory-learning-master` and `ai-session` sources and harness stubs to recoverable trash after `ai-memory` validation. Replace the `j-skills/memory-learning-master` source with the `j-skills/ai-memory` discovery stub; do not create competing compatibility redirects.
- Keep `custom-skills-master` active and independent. Its canonical authority is Obsidian; retain the former Git-backed source until the skill-source and harness-stub pilot passes.
- Do not permanently delete legacy skill folders or notes; move explicitly retired artifacts to recoverable trash.

### Phase 4: separately approved legacy migration

- Audit legacy notes and session vaults.
- Migrate only approved records in reversible batches.
- Preserve holding copies, links, and supersession metadata.
- Treat personal-session migration as a separate decision from work-memory migration.

## Acceptance Criteria

### Routing and organization

- A substantive task that produces qualifying durable learning automatically creates or updates the appropriate memory without requiring the user to ask.
- A task with no qualifying durable learning creates no memory noise, and an explicit save request enters the same classification and write workflow immediately.
- A Git repository with a canonical remote resolves to one stable `repo_id` and anchor; a fork, same-basename repository, worktree, and local-only repository do not collide.
- Ticket-specific durable content, sessions, and handoffs route beneath the appropriate ticket scope when retained; unrelated repository facts route to repository notes.
- A ticket spanning multiple repositories or a cross-repository project can link to all relevant scopes without duplicating facts or assigning a false single parent.
- A non-code personal project routes to `Projects/<project-key>/` in the configured personal root.
- Finance, immigration, health, travel, home, and learning material route to the appropriate `Areas/` location in the configured personal root.
- If a personal root is not configured, the skill does not write personal information to the work root.

### Identity, links, and idempotence

- All anchors satisfy the `memory-anchor` contract; all ordinary notes satisfy filename = title = H1.
- Bare anchor links fail validation; path-qualified same-root links and portable cross-root references pass validation.
- Reprocessing the same `promotion_id` and target routing produces an update/no-op rather than duplicate notes.
- Existing canonical notes are updated rather than duplicated when exact scope and provenance match.

### Retrieval and Graphify

- The skill searches Graphify for candidates, then validates canonical Markdown before writing.
- When the same fact exists in AI-Memory and a legacy session corpus, the canonical AI-Memory record is preferred and the legacy source remains visible as provenance.
- Routine writes invoke only the narrow AI-Memory refresh command.
- A failed refresh is reported as `saved, not indexed`; the skill does not claim that the new memory is queryable.
- A successful refresh verifies corpus registration and MCP health.

### Canonical skills and harness stubs

- A managed custom skill's complete authoritative content exists at `<AI_CUSTOM_SKILLS_DIR>/<skill-name>/SKILL.md`, not in a harness installation directory.
- Creating or updating a canonical skill synchronizes the required trigger metadata into every configured harness stub without copying the full skill body.
- Each stub uses the exact canonical folder name and resolves to the current canonical Obsidian path.
- A stub metadata mismatch, unreachable canonical path, or failed load prevents successful deployment status for that harness.
- `custom-skills-master` can bootstrap itself from an existing harness stub to its canonical Obsidian source without creating a circular or missing reference.
- Graphify returns the canonical skill as the authoritative result and does not treat harness stubs as distinct skills.
- The former Git-backed source remains recoverable until the pilot proves discovery and canonical loading. Verified Codex, Claude, and Copilot stubs point to the Obsidian canonical source.

### Migration and safety

- The pilot includes a second-write no-duplicate test, a live Graphify retrieval test, a stale/conflict scenario, and harness-stub trigger verification.
- `ai-memory` remains the only canonical active memory trigger. `graphify-memory`, `memory-learning-master`, and `ai-session` are moved out of active discovery into recoverable trash after replacement validation.
- No migration writes to a vault with unresolved conflict artifacts unless the user explicitly approves an audited recovery plan.

## Resolved Decisions and Open Decisions

- Resolved: ignored `.env` supplies the machine-specific `AI_CUSTOM_SKILLS_DIR` value.
- Resolved: the canonical `ai-memory` source is `skill/ai-memory/SKILL.md` in the `ai-memory-mcp` repository.
- Resolved: `AI_MEMORY_WORK_DIR` is supplied by the repository-local `.env` and points to the user's canonical Markdown vault.
- Resolved: the supported harness stub targets are Codex, Claude, Copilot, and OpenCode under their configured user skill directories; `j-skills/ai-memory` is also a distribution stub to the same canonical source.
- Confirm whether personal memory support is in the first implementation and provide the approved `AI_MEMORY_PERSONAL_DIR` if so.
- Define the material-write threshold and maximum Graphify indexing staleness.
- Define default Graphify corpus priority and whether personal/legacy session corpora are searchable by default.
- Assign an owner and target date for the legacy session-vault conflict and drift audit.
