---
name: ai-memory
description: Use when meaningful work produces durable knowledge that future agents should retain, when the user asks to remember or recall something, or when Graphify-backed Markdown memory needs retrieval, organization, consolidation, conflict handling, session or handoff capture, or refresh. Invoke automatically during substantive work and before completion; do not wait for the user to ask.
---

# AI Memory

Use this as the single canonical workflow for durable AI memory. Treat Markdown in the configured Obsidian memory root as the source of truth. Use the AI Memory MCP facade for retrieval; it combines lexical, semantic, and Graphify relationship signals without making Graphify the public contract.

Read [architecture.md](../../docs/architecture.md) when changing the retrieval system, index, provider boundary, refresh pipeline, or benchmark.

## Core Contract

- Evaluate memory automatically when meaningful reusable knowledge appears and again before completing substantive work.
- Also run the same workflow immediately when the user explicitly asks to remember, save, retrieve, consolidate, correct, or forget through supersession.
- Save durable facts, decisions, constraints, corrections, relationships, and verified workarounds without requiring per-write confirmation when the appropriate root is configured and safe.
- Keep transient progress, raw transcripts, command logs, obvious code facts, and easily rediscovered details out of durable memory.
- Search before creating. Prefer updating, merging, or superseding a precise existing note over creating a parallel note.
- Never permanently delete memory or skill content. Preserve prior truth through `superseded`, `archived`, or recoverable trash only when the user explicitly requests removal.
- Keep memory writes and Graphify indexing as separate verified outcomes.

## Load Configuration

Read `.env` at the repository root (two directories above this `SKILL.md`) when
shell access is available. Resolve configuration in this order:

1. Use `AI_MEMORY_WORK_DIR` for work or shared durable memory.
2. Use `AI_MEMORY_PERSONAL_DIR` for personal durable memory.
3. Use legacy `AI_MEMORY_DIR` only as a work-memory fallback.
4. Use `AI_CUSTOM_SKILLS_DIR` for canonical executable custom skills.
5. Use `GRAPHIFY_MEMORY_REFRESH_SCRIPT` for the routine narrow refresh.
6. Use `GRAPHIFY_MEMORY_EXTRACT_SCRIPT` only for an explicitly chosen extraction-only maintenance action.
7. Use `GRAPHIFY_GLOBAL_MCP_URL` to verify Graphify availability after refresh.
8. Use `GRAPHIFY_OPENAI_BASE_URL`, `GRAPHIFY_OPENAI_API_KEY`, `GRAPHIFY_OPENAI_MODEL`, and `GRAPHIFY_OPENAI_TOKEN_BUDGET` for semantic extraction through the configured OpenAI-compatible backend.

Do not infer that a personal record belongs in the work root. If personal memory is relevant and `AI_MEMORY_PERSONAL_DIR` is unset, continue the user's main task, skip the unsafe memory write, and ask for or report the missing safe target.

Before writing, verify that the selected root exists or that creating the narrow required folder is within the user's authorized scope. Do not create an empty taxonomy.

## Automatic Capture Workflow

Run this workflow at meaningful checkpoints and before the final response of substantive work:

1. **Classify the outcome.** Choose retrieval, durable memory, optional session or handoff, skill candidate, session-only context, or no write.
2. **Choose the domain.** Select `work` or `personal` before selecting a path.
3. **Choose one primary scope.** Use repository, ticket, project, area, tool, person, decision, reference, or session-only.
4. **Recall memory.** Use `memory_recall` with the narrowest safe scope. The tool selects exact, search, neighbor, or relationship behavior internally. Treat legacy-corpus results as candidates, not write authority.
5. **Inspect canonical Markdown.** Search the selected root with `rg` and read the exact candidate notes before deciding where to write.
6. **Choose one action.** Update, create, merge, mark `needs-review`, supersede, route to `custom-skills-master`, retain session-only, or skip with a reason.
7. **Write a small verified batch.** Apply concise Markdown changes and preserve identity, provenance, links, and predecessor state.
8. **Update navigation surfaces.** Touch only the relevant anchor, map, review queue, conflict, or stale-memory index.
9. **Refresh once.** After the material batch, run the narrow AI-Memory refresh and verify Graphify health.
10. **Report separate outcomes.** State what was written, merged or superseded, and whether indexing succeeded.

An explicit user request to save memory enters at step 1; it does not bypass search, scope, safety, deduplication, or verification.

## Decide What Is Durable

Promote information when it is likely to save future agents meaningful rediscovery or prevent repeated mistakes:

- stable repository architecture, identity, or conventions
- durable ticket or project decisions and rationale
- user corrections and stable operating boundaries
- non-obvious tool or environment behavior
- verified failure modes and workarounds
- cross-repository, system, person, or document relationships
- external context that is not derivable from the current codebase

Skip or keep session-only:

- ordinary task progress and temporary plans
- commit hashes, PR numbers, or ticket numbers without durable context
- raw command output or transcript excerpts
- speculative conclusions not supported by evidence
- details obvious from the currently inspected source
- duplicate facts already represented precisely

Create no memory merely to prove that automatic capture ran.

## Route Storage

Read [storage-and-schemas.md](references/storage-and-schemas.md) before creating a new scope, anchor, ordinary memory note, session, handoff, or cross-root link.

Use these routing rules:

- Repository knowledge -> `Repos/<repo-key>/`
- Ticket knowledge -> `Repos/<repo-key>/Tickets/<ticket-id>/`
- Cross-repository or non-code project -> `Projects/<project-key>/`
- Ongoing personal responsibility -> `Areas/<area>/` in the personal root
- Tool behavior -> `Tools/`
- Cross-cutting decision -> `Decisions/`; otherwise keep the decision with its primary scope
- Repeatable procedure -> `custom-skills-master`

Create repository, ticket, and project folders only when there is a durable record worth retaining. Do not duplicate a cross-scope fact into every linked repository.

## Retrieve Memory

For recall requests:

1. Determine work or personal scope and the likely repository, ticket, project, area, person, tool, or decision.
2. Call `memory_recall` first with work/personal, repository, ticket/project, status, or path scope when known.
3. Inspect the returned Markdown source path, corpus, status, freshness, and provenance.
4. Prefer an active canonical memory note over legacy session or vault results.
5. Search canonical Markdown directly when the facade is unavailable, stale, ambiguous, or missing expected results.
6. State when an answer came only from legacy, stale, or unverified memory.

Do not treat a Graphify node as authority for a write target until the canonical Markdown exists and has been inspected.

## Write and Reconcile Memory

- Give every new durable note a globally unique `memory_id`.
- Give each note exactly one primary scope and link it to the applicable anchor or index.
- Preserve concise provenance: source session or task, promotion ID when available, verification source, and reason the fact is durable.
- Reprocessing the same `promotion_id` must update or no-op; it must not create a duplicate claim.
- Keep filename, frontmatter `title`, and H1 aligned for ordinary notes.
- Keep `_repo.md`, `_ticket.md`, and `_project.md` as structural anchor exceptions.
- Use `review_after` for paths, URLs, tool availability, provider behavior, policies, and other facts likely to drift.

When new verified information conflicts with active memory:

1. Preserve both claims and their provenance.
2. Mark unresolved material `needs-review` and link the records.
3. After resolution, mark the predecessor `superseded` and set reciprocal `supersedes` and `superseded_by` links.
4. Keep the predecessor readable; never erase it during normal consolidation.

## Sessions and Handoffs

Treat sessions and handoffs as optional scoped context, not the default destination for every task.

- Search configured legacy session roots before creating a new session record.
- Do not move or rewrite legacy sessions until their work/personal boundaries and conflict artifacts have been audited.
- Store a new work session or handoff under the relevant repository, ticket, or project in `AI_MEMORY_WORK_DIR`.
- Store a new personal session or handoff only in `AI_MEMORY_PERSONAL_DIR`.
- Keep handoffs concise: task, current state, important artifacts, decisions, blockers, and next action.
- Preserve `session_id:entry_id` when promoting a session entry into durable memory.

Do not load full session archives into runtime context by default.

## Route Procedural Knowledge

When a finding changes how agents should repeatedly perform a workflow, invoke `custom-skills-master` instead of storing the full procedure as ordinary memory.

- Pass the durable finding, evidence, source session or task, and related existing skills.
- Let `custom-skills-master` classify it as a patch, support file, new skill, memory instead, session-only, or ignored.
- Expect canonical managed skills under `<AI_CUSTOM_SKILLS_DIR>/<skill-name>/SKILL.md` with harness-local discovery stubs.
- Retain only a compact memory pointer to the resulting skill or candidate when useful.
- If `custom-skills-master` or `AI_CUSTOM_SKILLS_DIR` is unavailable, preserve a candidate pointer for review and report that skill promotion did not complete.

Do not duplicate skill-authoring instructions inside ordinary memory notes.

## Refresh Graphify

After one or more material memory writes:

1. Call `memory_sync` after an ordinary Markdown batch.
2. Run the Graphify maintenance script only when the Graphify graph must be rebuilt.
3. Do not run the global or all-corpora extractor for routine memory writes.
4. Verify the configured Graphify endpoint or query path after the refresh finishes.
5. Confirm that the canonical corpus is registered and the changed note can be retrieved when practical.
6. If the note write succeeded but refresh or retrieval failed, report `saved, not indexed` and keep the canonical Markdown unchanged.

Pure retrievals, no-op deduplication, and non-material timestamp-only changes do not require refresh.

## Guardrails

- Do not write personal information to a work or corporate-synced root.
- Do not silently create a second memory root.
- Do not automatically migrate legacy notes, sessions, or skills.
- Do not write into a vault that has unresolved conflict artifacts without an explicitly approved recovery plan.
- Do not use bare `[[_repo]]`, `[[_ticket]]`, or `[[_project]]` links.
- Do not claim a note is indexed unless refresh and health verification succeeded.
- Do not let automatic capture delay or block completion of the user's main task when only the memory backend is unavailable.
- Do not permanently delete files. Use supersession, archive state, or recoverable trash after explicit user authorization.

## Verification

Before considering a memory operation complete, check the relevant cases:

- **Automatic capture:** A substantive task with durable learning creates or updates memory without a prompt.
- **Noise boundary:** A task with no durable learning creates nothing.
- **Explicit capture:** A direct save request follows the same classification and deduplication path.
- **Identity:** Repository forks, worktrees, same-basename repositories, and local-only repositories do not collide.
- **Idempotence:** Repeating the same source and routing does not duplicate memory.
- **Domain safety:** Missing personal configuration never falls back to work storage.
- **Conflict:** Contradictory facts remain linked and reviewable until resolved.
- **Skill boundary:** Repeatable procedures route to `custom-skills-master`.
- **Indexing:** Refresh failure is reported as `saved, not indexed`.

In the final response, summarize only material memory actions and indexing state. If nothing qualified for durable capture, do not invent memory noise.
