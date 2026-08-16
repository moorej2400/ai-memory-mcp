---
name: ai-memory
description: Use proactively when prior knowledge could improve the current work, and when new knowledge could help future sessions. Search memory and save useful memories automatically without waiting for the user. Also use for explicit memory requests, reconciliation, organization, conflict handling, and refresh.
---

# AI Memory

Use this as the single canonical workflow for durable AI memory. Treat Markdown in the configured primary vault as the write authority. Treat additional configured vaults as retrieval-only sources. Use the AI Memory MCP facade for retrieval across all configured sources.

Read [architecture.md](../../docs/architecture.md) when changing the retrieval system, index, provider boundary, refresh pipeline, or benchmark.

## Core Contract

- Automatically save memories that will be useful in future sessions.
- Treat automatic capture as an action, not a recommendation or optional follow-up.
- Do not require a user request, reminder, or per-write confirmation for a safe automatic write.
- Treat an explicit save request as a fallback or additional trigger, not a prerequisite.
- Also run the same workflow immediately when the user explicitly asks to remember, save, retrieve, consolidate, correct, or forget through supersession.
- Use judgment to determine future value. Do not limit memory capture to predefined categories or examples.
- Search before creating. Prefer updating, merging, or superseding a precise existing note over creating a parallel note.
- Never permanently delete memory or skill content. Preserve prior truth through `superseded`, `archived`, or recoverable trash only when the user explicitly requests removal.
- Keep memory writes and Graphify indexing as separate verified outcomes.

## Load Configuration

Read `.env` at the repository root (two directories above this `SKILL.md`) when
shell access is available. Resolve configuration in this order:

1. Use `AI_MEMORY_WORK_DIR` as the only writable memory vault.
2. Use `AI_MEMORY_PRIMARY_SOURCE_ID` as the primary vault identifier.
3. Use `AI_MEMORY_RETRIEVAL_SOURCES` for named retrieval-only vaults.
4. Use `AI_MEMORY_PERSONAL_DIR` as the optional `personal` retrieval-only source.
5. Use legacy `AI_MEMORY_DIR` only as a primary-vault fallback.
6. Use `AI_CUSTOM_SKILLS_DIR` for canonical executable custom skills.
7. Use `GRAPHIFY_MEMORY_REFRESH_SCRIPT` for the routine narrow refresh.
8. Use `GRAPHIFY_MEMORY_EXTRACT_SCRIPT` only for an explicitly chosen extraction-only maintenance action.
9. Use `GRAPHIFY_GLOBAL_MCP_URL` to verify Graphify availability after refresh.
10. Use the configured Graphify backend values only for full extraction.

Write new memories only under `AI_MEMORY_WORK_DIR`. Never write to an additional retrieval source.
Use `AI_MEMORY_WORK_DIR/.ai-memory/` for raw data, backups, indexes, provider state, migration data, and logs.

Before writing, verify that the selected root exists or that creating the narrow required folder is within the user's authorized scope. Do not create an empty taxonomy.

## Proactive Recall

Use memory as a normal source of context, not only after an explicit memory request.

- Before substantive work, determine whether prior knowledge could improve the result.
- When prior knowledge could help, search memory early with the narrowest useful scope.
- Use relevant memory to guide discovery, decisions, and task execution.
- Verify retrieved information when it could have changed.
- If memory is incomplete, continue through the best available sources.
- Automatically save or reconcile useful knowledge produced by the work.
- Do not require the user to initiate memory retrieval or capture.

## Automatic Capture Workflow

Run this workflow at meaningful checkpoints and before the final response of substantive work:

1. **Classify the outcome.** Choose retrieval, durable memory, optional session or handoff, skill candidate, session-only context, or no write.
2. **Choose the domain.** Select `work` or `personal` metadata before selecting a path.
3. **Choose one primary scope.** Use repository, ticket, project, area, tool, person, decision, reference, or session-only.
4. **Recall memory.** Use `memory_recall` across all configured sources with the narrowest safe scope.
5. **Inspect canonical Markdown.** Read candidates from their reported source before choosing a write action.
6. **Choose one action.** Update, create, enrich, merge, mark `needs-review`, supersede, route to `custom-skills-master`, retain session-only, or skip with a reason.
7. **Find the links.** Before you write, identify the existing notes that give context. Use the step 4 recall results.
8. **Write a small verified batch.** Apply concise Markdown changes that meet the note content rules. Preserve identity, provenance, links, and predecessor state.
9. **Update navigation surfaces.** Touch only the relevant anchor, map, review queue, conflict, or stale-memory index.
10. **Refresh once.** After the material batch, run the narrow AI-Memory refresh and verify Graphify health.
11. **Report separate outcomes.** State what was written, merged or superseded, and whether indexing succeeded.

An explicit user request to save memory enters at step 1; it does not bypass search, scope, safety, deduplication, or verification.

Do not finish substantive work with an unsaved qualifying candidate when the configured write target is safe.

## Decide What Is Durable

Save information when it will be useful to know in future sessions. Use judgment from the task context and available evidence.

Do not restrict this decision to a fixed list of information types. Create no memory only to prove that automatic capture ran.

## Refresh Existing Facts

Use the primary scope, entity, and property as the claim identity.

- If a verified value is unchanged, update only material freshness or provenance data.
- If an authoritative source confirms a changed value, update the matching canonical memory.
- Do not create a parallel active note for the same claim.
- Preserve the prior value in provenance or concise change history when it remains useful.
- If the prior value has a separate record, mark that record `superseded` and link both records.
- If authoritative sources conflict, preserve both claims and mark them `needs-review`.

## Enrich Existing Notes

`Refresh Existing Facts` applies when a value changes. This section applies when the fact stays correct, but the note was difficult to find or incomplete. Enrich automatically. Do not wait for a request.

Enrich when one of these occurs:

- Recall returned `no_answer`, and you then found the answer by a direct search.
- You needed more than one query to find the note.
- The note answered only part of the question.
- You found a related note that this note does not link to.

Make the smallest change that closes the gap:

- Add the words of the query that worked. Add the names and aliases that were absent.
- Add the missing detail below the summary.
- Add the missing link.
- Set `updated` to the current date. A term change is material because it changes retrieval.

If the answer is only in a source vault, write a new note in the primary vault. Put the source path in `provenance`. Never edit a source vault.

### Enrichment Guardrails

- Enrich only after you used the note and found a specific gap.
- Do not rewrite text that works. Do not enrich for style.
- Keep the summary short. Put new detail after it.
- Do not add a term that the note content does not support.
- Do not create a second note for a fact that an existing note holds.
- Refresh the index after the batch. An unindexed change does not help the next agent.

## Route Storage

Read [storage-and-schemas.md](references/storage-and-schemas.md) before creating a new scope, anchor, ordinary memory note, session, handoff, or cross-root link.

Use these routing rules:

- Repository knowledge -> `Repos/<repo-key>/`
- Ticket knowledge -> `Repos/<repo-key>/Tickets/<ticket-id>/`
- Cross-repository or non-code project -> `Projects/<project-key>/`
- Ongoing responsibility -> `Areas/<area>/` in the primary root when its privacy policy permits the write
- Tool behavior -> `Tools/`
- Cross-cutting decision -> `Decisions/`; otherwise keep the decision with its primary scope
- Repeatable procedure -> `custom-skills-master`

Create repository, ticket, and project folders only when there is a durable record worth retaining. Do not duplicate a cross-scope fact into every linked repository.

## Retrieve Memory

For recall requests:

1. Determine work or personal scope and the likely repository, ticket, project, area, person, tool, or decision.
2. Call `memory_recall` first with source, domain, repository, ticket, project, status, or path scope when known.
3. Inspect the returned source ID, Markdown path, status, freshness, and provenance.
4. Prefer an active canonical memory note over legacy session or vault results.
5. Treat `no_answer` evidence as unverified leads. Read the cited Markdown before you use a lead, and state when no lead survived verification.
6. Search canonical Markdown directly when the facade is unavailable, stale, ambiguous, or missing expected results.
7. State when an answer came only from legacy, stale, or unverified memory.

Do not treat a Graphify node as authority for a write target until the canonical Markdown exists and has been inspected.

Raw artifact evidence is a lead unless recall reports an exact match.
Use `memory_artifact_read` to inspect ordered context from an `artifact://` citation.

## Distill Raw Artifacts

Read [artifact-distillation.md](references/artifact-distillation.md) before you process a pending meeting or conversation.

Keep the complete source material in SQLite.
Put only durable summaries, decisions, actions, open questions, context, and short evidence in Markdown.
Preserve manual Markdown outside the managed distillation markers.
Mark the current event and source digest only after the note passes validation and recall verification.

## Write and Reconcile Memory

- Give every new durable note a globally unique `memory_id`.
- Give each note exactly one primary scope and link it to the applicable anchor or index.
- Preserve concise provenance: source session or task, promotion ID when available, verification source, and reason the fact is durable.
- Reprocessing the same `promotion_id` must update or no-op; it must not create a duplicate claim.
- Keep filename, frontmatter `title`, and H1 aligned for ordinary notes.
- Keep `_repo.md`, `_ticket.md`, and `_project.md` as structural anchor exceptions.
- Use `review_after` for information that is likely to change.

Every new note must connect to the memory graph. An isolated note is very difficult to find again.

- Search for related notes before you write. Use the recall results from the capture workflow.
- Add a `[[Note Title]]` wikilink in the body for each note that gives useful context.
- Link to the applicable anchor or index note.
- Use the exact title of the target note. An unknown title makes a broken link. A title that two notes share makes an ambiguous link. The graph build discards both.
- Add a wikilink in the other note also when the relation is important in both directions.
- Do not add a link that gives no context. Three good links are better than ten weak links.

The graph build makes an edge from a body wikilink and from a frontmatter `related` entry. Use `related` for the primary relation. Use body wikilinks for context inside the text.

When new verified information conflicts with active memory:

1. Preserve both claims and their provenance.
2. Mark unresolved material `needs-review` and link the records.
3. After resolution, mark the predecessor `superseded` and set reciprocal `supersedes` and `superseded_by` links.
4. Keep the predecessor readable; never erase it during normal consolidation.

## Note Content

Choose the format that fits the material. Use prose, lists, tables, or code blocks as the content requires. Do not force one template onto every note.

Every durable note must meet these goals:

- Start the body with a summary of one to three sentences. A reader who stops after the summary must still get the core fact.
- Put the detail after the summary. Include the steps, values, and background that make the fact usable.
- Make the note self-sufficient. A future agent must get the answer from this note without the source conversation.
- Keep the note as short as the fact permits. Record detail that changes the outcome. Do not record narrative that does not.

### Searchable Terms

Retrieval is lexical first. A word that is not in the note cannot match a query. The index extracts identifiers from the note text automatically. There is no separate keyword field.

- Name each system, tool, and product with its real name. Add the alternate names that people use for it.
- Include exact strings verbatim: commands, URLs, hostnames, settings, error text, and ticket identifiers.
- Write the fact with the words a future question will use. State a procedure as the user would ask for it.
- Do not hide a key term behind a paraphrase. A note that only says "the log console" cannot answer a query that uses the product name.

## Sessions and Handoffs

Treat sessions and handoffs as optional scoped context, not the default destination for every task.

- Search configured legacy session roots before creating a new session record.
- Do not move or rewrite legacy sessions until their work/personal boundaries and conflict artifacts have been audited.
- Store each new session or handoff under its relevant scope in `AI_MEMORY_WORK_DIR`.
- Use `root_scope` metadata to distinguish work and personal records.
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
2. Confirm that synchronization reads all configured sources without writing to them.
3. Run the Graphify maintenance script only when the Graphify graph must be rebuilt.
4. Do not run the global or all-corpora extractor for routine memory writes.
5. Verify the configured Graphify endpoint or query path after the refresh finishes.
6. Confirm that each configured memory source is registered.
7. If refresh fails, report `saved, not indexed` and keep all Markdown unchanged.

Pure retrievals, no-op deduplication, and non-material timestamp-only changes do not require refresh.

## Guardrails

- Do not silently create a second memory root.
- Do not write to a retrieval-only source.
- Do not automatically migrate legacy notes, sessions, or skills.
- Do not write into a vault that has unresolved conflict artifacts without an explicitly approved recovery plan.
- Do not use bare `[[_repo]]`, `[[_ticket]]`, or `[[_project]]` links.
- Do not claim a note is indexed unless refresh and health verification succeeded.
- Do not let automatic capture delay or block completion of the user's main task when only the memory backend is unavailable.
- Do not permanently delete files. Use supersession, archive state, or recoverable trash after explicit user authorization.

## Verification

Before considering a memory operation complete, check the relevant cases:

- **Automatic capture:** A substantive task with durable learning creates or updates memory without a prompt.
- **Fact refresh:** A verified fact change updates the matching memory and preserves the prior value.
- **Noise boundary:** A task with no durable learning creates nothing.
- **Explicit capture:** A direct save request follows the same classification and deduplication path.
- **Identity:** Repository forks, worktrees, same-basename repositories, and local-only repositories do not collide.
- **Idempotence:** Repeating the same source and routing does not duplicate memory.
- **Write authority:** All new records go to the primary writable vault.
- **Source safety:** Synchronization does not modify retrieval-only vaults.
- **Conflict:** Contradictory facts remain linked and reviewable until resolved.
- **Skill boundary:** Repeatable procedures route to `custom-skills-master`.
- **Links:** Each new note has at least one link to a related note or to its anchor.
- **Summary:** Each new note starts with a summary that can stand alone as the answer.
- **Searchable terms:** Each new note contains the names, aliases, and exact strings a future query will use.
- **Enrichment:** A note that was difficult to find gets the missing terms, detail, or links.
- **Indexing:** Refresh failure is reported as `saved, not indexed`.

In the final response, summarize only material memory actions and indexing state. If nothing qualified for durable capture, do not invent memory noise.
