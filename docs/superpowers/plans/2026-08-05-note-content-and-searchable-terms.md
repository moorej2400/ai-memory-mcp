# Note Content and Searchable Terms Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teach the ai-memory skill what a durable note must contain — a stand-alone summary and the exact terms a future query will use — without forcing one rigid template onto every note.

**Architecture:** Doc-only change. A new `## Note Content` section in `skill/ai-memory/SKILL.md` states outcome goals (summary first, self-sufficient, detail below) and explicitly frees the format: the writer chooses prose, lists, tables, or code blocks per note. A `### Searchable Terms` subsection makes the retrieval mechanics explicit (lexical-first, identifiers auto-extracted from text, no keyword field) so the writer includes real names, aliases, and verbatim strings. `references/storage-and-schemas.md` gains a short subsection documenting identifier auto-extraction. The Verification checklist gains two matching bullets.

**Tech Stack:** Markdown docs only. No code changes. Verified with pytest (privacy + full suite).

## Global Constraints

- Branch: `retrieval-quality` (already pushed; continue on it).
- All new or changed documentation must use ASD-STE100 Simplified Technical English (AGENTS.md). Read `docs/writing-standard.md` before writing prose.
- This repository is public. Use only neutral synthetic values in examples ("the log console", `logs.example.internal`). Never a real organization name, private domain, machine name, or ticket prefix.
- Do not delete a file. Do not delete existing prose unless the task shows the exact replacement.
- Run the privacy tests and inspect the staged diff before each commit (AGENTS.md).
- Commit messages follow the existing style: `docs: <imperative summary>`, with the standard `Co-Authored-By` trailer that the harness supplies. Do not write a literal email address into a file. The privacy test rejects one.

**Design intent (from the user, binding):** the formatting guide must NOT strangle the writer into one fixed format. State goals the note must meet; leave structure free. If a step in this plan and this intent ever conflict, the intent wins.

---

### Task 1: Add the Note Content section to SKILL.md

**Files:**
- Modify: `skill/ai-memory/SKILL.md` (insert new section after the conflict list that ends `## Write and Reconcile Memory`, currently line 149; modify workflow step 8, currently line 67; add one Verification bullet after the `**Links:**` bullet, currently line 215)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: the `## Note Content` section heading. Task 2 appends its `### Searchable Terms` subsection inside this section, so the heading text must be exactly `## Note Content`.

- [ ] **Step 1: Read the writing standard**

Run: read `docs/writing-standard.md` fully. It governs every sentence written in the next steps.

- [ ] **Step 2: Insert the Note Content section**

In `skill/ai-memory/SKILL.md`, find the end of `## Write and Reconcile Memory` — the conflict-handling numbered list that ends with:

```markdown
4. Keep the predecessor readable; never erase it during normal consolidation.
```

Immediately after that line (and its trailing blank line), and before `## Sessions and Handoffs`, insert:

```markdown
## Note Content

Choose the format that fits the material. Use prose, lists, tables, or code blocks as the content requires. Do not force one template onto every note.

Every durable note must meet these goals:

- Start the body with a summary of one to three sentences. A reader who stops after the summary must still get the core fact.
- Put the detail after the summary. Include the steps, values, and background that make the fact usable.
- Make the note self-sufficient. A future agent must get the answer from this note without the source conversation.
- Keep the note as short as the fact permits. Record detail that changes the outcome. Do not record narrative that does not.
```

- [ ] **Step 3: Update workflow step 8**

In the `## Automatic Capture Workflow` numbered list, replace:

```markdown
8. **Write a small verified batch.** Apply concise Markdown changes and preserve identity, provenance, links, and predecessor state.
```

with:

```markdown
8. **Write a small verified batch.** Apply concise Markdown changes that meet the note content rules. Preserve identity, provenance, links, and predecessor state.
```

- [ ] **Step 4: Add the Verification bullet**

In the `## Verification` checklist, immediately after:

```markdown
- **Links:** Each new note has at least one link to a related note or to its anchor.
```

insert:

```markdown
- **Summary:** Each new note starts with a summary that can stand alone as the answer.
```

- [ ] **Step 5: Verify the edit**

Run: `grep -n "## Note Content" skill/ai-memory/SKILL.md`
Expected: one match, between `## Write and Reconcile Memory` and `## Sessions and Handoffs`.

Run: `grep -c "^## " skill/ai-memory/SKILL.md`
Expected: exactly one more `## ` heading than before the edit (was 14, now 15).

Self-check the new prose against `docs/writing-standard.md`: short sentences, one instruction per sentence, imperative mood for instructions.

- [ ] **Step 6: Run the privacy tests and the full suite**

Run: `python -m pytest tests/test_portability.py -q`
Expected: all pass.

Run: `python -m pytest -q`
Expected: all pass (101 tests at plan time).

- [ ] **Step 7: Inspect the staged diff and commit**

```bash
git add skill/ai-memory/SKILL.md docs/superpowers/plans/2026-08-05-note-content-and-searchable-terms.md
git diff --cached
```

Confirm the diff contains no private term. Then:

```bash
git commit -m "docs: state note content goals without a fixed template"
```

Add the standard `Co-Authored-By` trailer that the harness supplies.

---

### Task 2: Add the Searchable Terms rules

**Files:**
- Modify: `skill/ai-memory/SKILL.md` (append a `### Searchable Terms` subsection at the end of the `## Note Content` section from Task 1; add one Verification bullet after the `**Summary:**` bullet from Task 1)
- Modify: `skill/ai-memory/references/storage-and-schemas.md` (insert a subsection after `## Ordinary Notes`, before `## Session and Handoff Additions`)

**Interfaces:**
- Consumes: the `## Note Content` section heading created in Task 1.
- Produces: nothing later tasks rely on.

- [ ] **Step 1: Append the Searchable Terms subsection**

In `skill/ai-memory/SKILL.md`, at the end of the `## Note Content` section (after the goals list, before `## Sessions and Handoffs`), insert:

```markdown
### Searchable Terms

Retrieval is lexical first. A word that is not in the note cannot match a query. The index extracts identifiers from the note text automatically. There is no separate keyword field.

- Name each system, tool, and product with its real name. Add the alternate names that people use for it.
- Include exact strings verbatim: commands, URLs, hostnames, settings, error text, and ticket identifiers.
- Write the fact with the words a future question will use. State a procedure as the user would ask for it.
- Do not hide a key term behind a paraphrase. A note that only says "the log console" cannot answer a query that uses the product name.
```

- [ ] **Step 2: Add the Verification bullet**

In the `## Verification` checklist, immediately after the `**Summary:**` bullet from Task 1, insert:

```markdown
- **Searchable terms:** Each new note contains the names, aliases, and exact strings a future query will use.
```

- [ ] **Step 3: Document identifier extraction in the schema reference**

In `skill/ai-memory/references/storage-and-schemas.md`, after the `## Ordinary Notes` section (its closing paragraph is "For ordinary notes, keep the filename, `title`, and H1 aligned. Assign exactly one primary scope and link upward to the applicable anchor or index.") and before `## Session and Handoff Additions`, insert:

```markdown
### Identifier Extraction

The index extracts identifiers from the note text automatically. It finds ticket identifiers, pull-request references, and file paths. The frontmatter has no separate keyword field. A term must appear in the title, frontmatter, or body before a query can match it. Write names, aliases, and exact strings into the note body.
```

- [ ] **Step 4: Verify the edits**

Run: `grep -n "### Searchable Terms" skill/ai-memory/SKILL.md`
Expected: one match, inside the `## Note Content` section.

Run: `grep -n "### Identifier Extraction" skill/ai-memory/references/storage-and-schemas.md`
Expected: one match, between `## Ordinary Notes` and `## Session and Handoff Additions`.

Cross-check accuracy against the code: `src/ai_memory_mcp/text.py` `IDENTIFIER_RE` matches ticket identifiers, PR references, and paths; `parse_document` runs it over the raw file. The prose must not promise more than the regex does.

Self-check the new prose against `docs/writing-standard.md`.

- [ ] **Step 5: Run the privacy tests and the full suite**

Run: `python -m pytest tests/test_portability.py -q`
Expected: all pass.

Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 6: Inspect the staged diff and commit**

```bash
git add skill/ai-memory/SKILL.md skill/ai-memory/references/storage-and-schemas.md
git diff --cached
```

Confirm the diff contains no private term. Then:

```bash
git commit -m "docs: require searchable terms in every durable note"
```

Add the standard `Co-Authored-By` trailer that the harness supplies.
