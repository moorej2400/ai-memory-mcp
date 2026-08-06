# Intake Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Define what a note produced by an external pipeline must contain to be retrieval-ready, and give the operator a command that reports every note that does not meet the contract.

**Architecture:** Two parts. `docs/intake-contract.md` states the contract for any producer that writes Markdown into a retrieval-only vault. `src/ai_memory_mcp/intake.py` checks a vault against that contract and returns machine-readable issues, exposed as the `ai-memory-check-intake` console script. The checker reads Markdown only. It never writes to a vault, which keeps a retrieval-only source read-only.

**Tech Stack:** Python 3, standard library plus PyYAML (already a dependency). pytest. Markdown docs in ASD-STE100.

## Global Constraints

- Branch: `retrieval-quality`.
- All new or changed documentation must use ASD-STE100 Simplified Technical English (AGENTS.md). Read `docs/writing-standard.md` before writing prose.
- This repository is public. Use only neutral synthetic values ("Log Console", `logs.example.internal`, `DEMO-1430`). Never a real organization name, private domain, machine name, account name, or ticket prefix.
- Do not write a literal email address into any file. The privacy test rejects one.
- Do not delete a file. Do not delete existing prose unless the task shows the exact replacement.
- The checker must never write to a memory source. A retrieval-only vault stays read-only.
- Run the privacy tests and inspect the staged diff before each commit (AGENTS.md).
- Commit messages follow the existing style (`docs:` or `feat:` plus an imperative summary), with the standard `Co-Authored-By` trailer that the harness supplies.

**Scope note:** this plan covers the producer half of distillation. It states that a raw transcript is not a durable note. It does not add the agent-side promotion workflow (distill a chat export into a durable note during a session). That work stays open.

---

### Task 1: Write the intake contract

**Files:**
- Create: `docs/intake-contract.md`
- Modify: `docs/README.md` (add one bullet under `## Agent integration`)
- Modify: `docs/architecture.md` (add a paragraph at the end of `## Source authority`, currently line 20)
- Modify: `skill/ai-memory/SKILL.md` (add one sentence at the end of the `## Note Content` section, after the `### Searchable Terms` list)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: the rule names that Task 2 implements. Task 2 uses these exact identifiers as its `rule` values, so the names in the contract table and in `intake.py` must match character for character: `frontmatter`, `required-field`, `memory-id-unique`, `title-h1`, `summary`, `link`, `date-format`, `transcript`.

- [ ] **Step 1: Read the writing standard**

Run: read `docs/writing-standard.md` fully. It governs every sentence in this task.

- [ ] **Step 2: Create the contract document**

Create `docs/intake-contract.md` with this content:

````markdown
# Intake Contract

An external pipeline can write Markdown notes into a retrieval-only vault. This
document states what each note must contain. A note that meets this contract is
retrieval-ready. A note that does not meet it can stay invisible to recall.

The AI Memory MCP server does not write these notes. The producer owns them.
Use [`ai-memory-check-intake`](operations.md) to find the notes that do not meet
the contract.

## Required frontmatter

Each note starts with YAML frontmatter. These fields are mandatory:

| Field | Rule |
|---|---|
| `memory_id` | Globally unique. Stable across a repeated run of the producer. |
| `title` | Human-readable. Identical to the H1 in the body. |
| `type` | `memory`, `ai-session`, `handoff`, or `memory-index`. |
| `root_scope` | `work` or `personal`. |
| `primary_scope` | A map with a `kind` and an `id`. |
| `status` | `active`, `needs-review`, `superseded`, or `archived`. |
| `created` | `YYYY-MM-DD`. |
| `updated` | `YYYY-MM-DD`. |
| `provenance` | The source system, the source identifier, and the date. |

Add `review_after` with a `YYYY-MM-DD` value when the fact is likely to change.

See [storage-and-schemas.md](../skill/ai-memory/references/storage-and-schemas.md)
for the complete schema.

## Required body

- Start the body with the H1. Use the same text as the `title` field.
- Put a summary of one to three sentences directly after the H1. A reader who
  stops after the summary must still get the core fact.
- Put the detail after the summary.
- Give each note at least one link. Use a `related` entry, a body wikilink, or
  both.

## Searchable terms

Retrieval is lexical first. A word that is not in the note cannot match a query.

- Name each system, tool, and product with its real name. Add the alternate
  names that people use for it.
- Include exact strings verbatim: commands, URLs, hostnames, settings, error
  text, and ticket identifiers.
- Write the fact with the words a future question will use.

The full-text index contains the `title`, the headings, the body text, and the
extracted identifiers. It does not contain the other frontmatter fields.

## Distillation

A raw transcript is source material. It is not a durable note.

- Extract the durable facts. Write one note for each fact.
- Do not copy a complete chat log, meeting log, or document into a note.
- Keep the link to the source in `provenance`.

A note that holds many speaker lines or many timestamp lines fails the
`transcript` rule.

## Idempotence

A repeated run must update a note. It must not create a second note for the same
fact.

- Derive `memory_id` from the source identity, not from the run time.
- Keep `created` unchanged after the first write.
- Set `updated` to the date of the material change.

## Example

```markdown
---
memory_id: mem-log-console-access
title: Log Console Access
type: memory
root_scope: work
primary_scope:
  kind: tool
  id: tool:log-console
status: active
created: 2026-01-14
updated: 2026-02-02
review_after: 2026-08-02
related:
  - "[[Tools/Log Console|Log Console]]"
provenance:
  - source: meeting-notes
    reference: meeting-2026-02-02
    verified: 2026-02-02
---

# Log Console Access

Sign in to the Log Console at `https://logs.example.internal` with the single
sign-on account. The `log-reader` role is necessary. Request the role in ticket
queue `DEMO`.

## Procedure

1. Open `https://logs.example.internal`.
2. Select **Sign in with SSO**.
3. Select the `production` index.

An account without the `log-reader` role gets the error
`403 index access denied`.

See [[Tools/Log Console|Log Console]] for the alert rules.
```

## Rules

The checker reports one of these rule names for each issue:

| Rule | Meaning |
|---|---|
| `frontmatter` | The file has no frontmatter, or the YAML does not parse. |
| `required-field` | A mandatory field is absent or empty. |
| `memory-id-unique` | Two notes in the vault use the same `memory_id`. |
| `title-h1` | The `title` field and the H1 are different. |
| `summary` | No summary text comes between the H1 and the next heading. |
| `link` | The note has no `related` entry and no body wikilink. |
| `date-format` | A date field does not use `YYYY-MM-DD`. |
| `transcript` | The body looks like a raw transcript. |
````

- [ ] **Step 3: Add the navigation pointer**

In `docs/README.md`, under `## Agent integration`, after the `AI Memory skill` bullet, insert:

```markdown
- [Intake contract](intake-contract.md) states what an external producer must write.
```

- [ ] **Step 4: Point the architecture document at the contract**

In `docs/architecture.md`, at the end of `## Source authority` (after the line "A failed refresh does not change the Markdown authority."), insert:

```markdown
An external pipeline can write notes into a retrieval-only vault.
The [intake contract](intake-contract.md) states what those notes must contain.
The server does not repair a note that does not meet the contract.
```

- [ ] **Step 5: Point the skill at the contract**

In `skill/ai-memory/SKILL.md`, at the end of the `## Note Content` section (after the last bullet of `### Searchable Terms`, before `## Sessions and Handoffs`), insert:

```markdown
An external pipeline that writes into a retrieval-only vault must meet the same goals. See [intake-contract.md](../../docs/intake-contract.md).
```

- [ ] **Step 6: Verify the edits**

Run: `grep -n "intake-contract" docs/README.md docs/architecture.md skill/ai-memory/SKILL.md`
Expected: one match in each file.

Run: `grep -c "^| \`" docs/intake-contract.md`
Expected: 8 rule rows in the `## Rules` table.

Check each link target resolves: `docs/operations.md`, `../skill/ai-memory/references/storage-and-schemas.md`, `intake-contract.md`.

Self-check the new prose against `docs/writing-standard.md`.

- [ ] **Step 7: Run the privacy tests and the full suite**

Run: `.venv/bin/python -m pytest tests/test_portability.py -q`
Expected: all pass.

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 8: Inspect the staged diff and commit**

```bash
git add docs/intake-contract.md docs/README.md docs/architecture.md skill/ai-memory/SKILL.md
git diff --cached
```

Confirm the diff contains no private term. Then:

```bash
git commit -m "docs: state the intake contract for external producers"
```

---

### Task 2: Build the intake checker

**Files:**
- Create: `src/ai_memory_mcp/intake.py`
- Create: `tests/test_intake.py`
- Modify: `src/ai_memory_mcp/text.py` (add a public `read_frontmatter` wrapper after `_frontmatter`, currently ends line 64)
- Modify: `pyproject.toml` (add one console script under `[project.scripts]`, currently lines 23-26)
- Modify: `docs/operations.md` (add a section for the new command)

**Interfaces:**
- Consumes: the rule names defined in Task 1 (`frontmatter`, `required-field`, `memory-id-unique`, `title-h1`, `summary`, `link`, `date-format`, `transcript`); `wikilink_targets(values: Iterable[str]) -> list[str]` from `ai_memory_mcp.text`.
- Produces:
  - `ai_memory_mcp.text.read_frontmatter(raw: str) -> tuple[dict[str, Any], str]`
  - `ai_memory_mcp.intake.IntakeIssue` — frozen dataclass with `path: str`, `rule: str`, `detail: str`
  - `ai_memory_mcp.intake.check_document(path: Path, root: Path) -> list[IntakeIssue]`
  - `ai_memory_mcp.intake.check_source(root: Path) -> dict[str, Any]`
  - `ai_memory_mcp.intake.main() -> int`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_intake.py`:

```python
from __future__ import annotations

from pathlib import Path

from ai_memory_mcp.intake import check_document, check_source

GOOD = """---
memory_id: mem-log-console-access
title: Log Console Access
type: memory
root_scope: work
primary_scope:
  kind: tool
  id: tool:log-console
status: active
created: 2026-01-14
updated: 2026-02-02
provenance:
  - source: meeting-notes
    reference: meeting-2026-02-02
related:
  - "[[Log Console]]"
---

# Log Console Access

Sign in at `https://logs.example.internal` with the single sign-on account.

## Procedure

1. Open the console.
"""


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _rules(root: Path, path: Path) -> set[str]:
    return {issue.rule for issue in check_document(path, root)}


def test_a_conforming_note_has_no_issue(tmp_path: Path) -> None:
    path = _write(tmp_path, "Tools/Log Console Access.md", GOOD)
    assert _rules(tmp_path, path) == set()


def test_missing_frontmatter_is_reported(tmp_path: Path) -> None:
    path = _write(tmp_path, "Tools/Bare.md", "# Bare\n\nNo frontmatter here.\n")
    assert "frontmatter" in _rules(tmp_path, path)


def test_missing_required_field_is_reported(tmp_path: Path) -> None:
    text = GOOD.replace("status: active\n", "")
    path = _write(tmp_path, "Tools/No Status.md", text)
    assert "required-field" in _rules(tmp_path, path)


def test_title_and_h1_must_agree(tmp_path: Path) -> None:
    text = GOOD.replace("# Log Console Access", "# Something Else")
    path = _write(tmp_path, "Tools/Mismatch.md", text)
    assert "title-h1" in _rules(tmp_path, path)


def test_a_note_without_a_summary_is_reported(tmp_path: Path) -> None:
    text = GOOD.replace(
        "Sign in at `https://logs.example.internal` with the single sign-on account.\n\n",
        "",
    )
    path = _write(tmp_path, "Tools/No Summary.md", text)
    assert "summary" in _rules(tmp_path, path)


def test_a_note_without_a_link_is_reported(tmp_path: Path) -> None:
    text = GOOD.replace('related:\n  - "[[Log Console]]"\n', "")
    path = _write(tmp_path, "Tools/No Link.md", text)
    assert "link" in _rules(tmp_path, path)


def test_a_body_wikilink_satisfies_the_link_rule(tmp_path: Path) -> None:
    text = GOOD.replace('related:\n  - "[[Log Console]]"\n', "")
    text = text.replace("1. Open the console.\n", "1. Open [[Log Console]].\n")
    path = _write(tmp_path, "Tools/Body Link.md", text)
    assert "link" not in _rules(tmp_path, path)


def test_a_bad_date_is_reported(tmp_path: Path) -> None:
    text = GOOD.replace("updated: 2026-02-02", "updated: 02/02/2026")
    path = _write(tmp_path, "Tools/Bad Date.md", text)
    assert "date-format" in _rules(tmp_path, path)


def test_a_transcript_is_reported(tmp_path: Path) -> None:
    speakers = "\n".join(
        f"Speaker {index % 3}: This is turn {index} of the conversation."
        for index in range(12)
    )
    text = GOOD.replace("1. Open the console.\n", speakers + "\n")
    path = _write(tmp_path, "Tools/Transcript.md", text)
    assert "transcript" in _rules(tmp_path, path)


def test_a_duplicate_memory_id_is_reported(tmp_path: Path) -> None:
    # Both notes carry the same memory_id, which is what a non-idempotent
    # producer emits on a second run.
    _write(tmp_path, "Tools/One.md", GOOD)
    _write(tmp_path, "Tools/Two.md", GOOD)
    summary = check_source(tmp_path)
    assert any(issue["rule"] == "memory-id-unique" for issue in summary["issues"])


def test_check_source_counts_conforming_notes(tmp_path: Path) -> None:
    _write(tmp_path, "Tools/Good.md", GOOD)
    _write(tmp_path, "Tools/Bare.md", "# Bare\n\nNo frontmatter here.\n")
    summary = check_source(tmp_path)
    assert summary["documents"] == 2
    assert summary["conforming"] == 1


def test_check_source_does_not_write_to_the_vault(tmp_path: Path) -> None:
    path = _write(tmp_path, "Tools/Good.md", GOOD)
    before = path.stat().st_mtime_ns
    check_source(tmp_path)
    assert path.stat().st_mtime_ns == before
    assert sorted(item.name for item in tmp_path.rglob("*")) == ["Good.md", "Tools"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_intake.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'ai_memory_mcp.intake'`.

- [ ] **Step 3: Add the public frontmatter reader**

In `src/ai_memory_mcp/text.py`, directly after the `_frontmatter` function (its last line is `return metadata if isinstance(metadata, dict) else {}, parts[2].lstrip()`), insert:

```python
def read_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Split a Markdown file into frontmatter and body.

    The intake checker needs the raw metadata. `parse_document` supplies
    defaults for an absent field, which would hide the exact gap the checker
    must report.
    """
    return _frontmatter(raw)
```

- [ ] **Step 4: Write the checker**

Create `src/ai_memory_mcp/intake.py`:

```python
from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .text import read_frontmatter, wikilink_targets

REQUIRED_FIELDS = (
    "memory_id",
    "title",
    "type",
    "root_scope",
    "primary_scope",
    "status",
    "created",
    "updated",
    "provenance",
)
DATE_FIELDS = ("created", "updated", "review_after")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
SPEAKER_RE = re.compile(r"^\s*(?:\*\*)?[A-Z][\w .'-]{1,40}(?:\*\*)?\s*:\s+\S")
TIMESTAMP_RE = re.compile(r"^\s*\[?\d{1,2}:\d{2}")
# A distilled note quotes a speaker now and then. A transcript is mostly
# speaker lines, so the check needs both a floor and a proportion.
TRANSCRIPT_MIN_LINES = 8
TRANSCRIPT_MIN_SHARE = 0.25


@dataclass(frozen=True)
class IntakeIssue:
    path: str
    rule: str
    detail: str


def _summary_text(body: str) -> str:
    after_h1 = H1_RE.split(body, maxsplit=1)
    if len(after_h1) < 3:
        return ""
    remainder = after_h1[2]
    heading = HEADING_RE.search(remainder)
    section = remainder[: heading.start()] if heading else remainder
    return section.strip()


def _looks_like_a_transcript(body: str) -> bool:
    lines = [line for line in body.splitlines() if line.strip()]
    if not lines:
        return False
    marked = sum(
        1
        for line in lines
        if SPEAKER_RE.match(line) or TIMESTAMP_RE.match(line)
    )
    return (
        marked >= TRANSCRIPT_MIN_LINES
        and marked / len(lines) >= TRANSCRIPT_MIN_SHARE
    )


def check_document(path: Path, root: Path) -> list[IntakeIssue]:
    relative = path.relative_to(root).as_posix()
    issues: list[IntakeIssue] = []

    def report(rule: str, detail: str) -> None:
        issues.append(IntakeIssue(path=relative, rule=rule, detail=detail))

    raw = path.read_text(encoding="utf-8-sig")
    metadata, body = read_frontmatter(raw)
    if not metadata:
        report("frontmatter", "The file has no frontmatter, or the YAML does not parse.")
        return issues

    for field in REQUIRED_FIELDS:
        value = metadata.get(field)
        if value is None or (isinstance(value, (str, list, dict)) and not value):
            report("required-field", f"`{field}` is absent or empty.")

    for field in DATE_FIELDS:
        value = metadata.get(field)
        if value and not DATE_RE.match(str(value)):
            report("date-format", f"`{field}` is not a YYYY-MM-DD date.")

    title = str(metadata.get("title") or "").strip()
    h1 = H1_RE.search(body)
    if not h1:
        report("title-h1", "The body has no H1.")
    elif title and h1.group(1).strip() != title:
        report("title-h1", f"The H1 `{h1.group(1).strip()}` and the title `{title}` differ.")

    if not _summary_text(body):
        report("summary", "No summary text comes between the H1 and the next heading.")

    related = metadata.get("related") or []
    if not related and not wikilink_targets([body]):
        report("link", "The note has no `related` entry and no body wikilink.")

    if _looks_like_a_transcript(body):
        report("transcript", "The body looks like a raw transcript. Distil it first.")

    return issues


def check_source(root: Path) -> dict[str, Any]:
    """Report every note in one vault that does not meet the intake contract.

    This function only reads. A retrieval-only vault must stay unchanged.
    """
    paths = sorted(
        (path for path in root.rglob("*.md") if path.is_file()),
        key=lambda item: item.as_posix().casefold(),
    )
    issues: list[IntakeIssue] = []
    seen: dict[str, str] = {}
    failing: set[str] = set()
    for path in paths:
        found = check_document(path, root)
        relative = path.relative_to(root).as_posix()
        metadata, _ = read_frontmatter(path.read_text(encoding="utf-8-sig"))
        memory_id = str(metadata.get("memory_id") or "").strip()
        if memory_id:
            first = seen.setdefault(memory_id, relative)
            if first != relative:
                found.append(
                    IntakeIssue(
                        path=relative,
                        rule="memory-id-unique",
                        detail=f"`{memory_id}` is also used by `{first}`.",
                    )
                )
        issues.extend(found)
        if found:
            failing.add(relative)
    return {
        "root": str(root),
        "documents": len(paths),
        "conforming": len(paths) - len(failing),
        "issues": [asdict(issue) for issue in issues],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check a memory vault against the intake contract."
    )
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    summary = check_source(args.root)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 1 if summary["issues"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_intake.py -q`
Expected: all pass.

If `test_a_conforming_note_has_no_issue` fails, print the issues before you
change a threshold. The good fixture must be genuinely conforming; do not
weaken a rule to make it pass.

- [ ] **Step 6: Register the console script**

In `pyproject.toml`, under `[project.scripts]`, after the `ai-memory-benchmark` line, insert:

```toml
ai-memory-check-intake = "ai_memory_mcp.intake:main"
```

- [ ] **Step 7: Verify the command runs**

Run: `.venv/bin/pip install -e . -q && .venv/bin/ai-memory-check-intake --help`
Expected: the usage text prints, and it names the `root` argument.

- [ ] **Step 8: Document the command**

In `docs/operations.md`, add this section at the end of the file. The outer
fence below is four backticks; write the inner `bash` block with three:

````markdown
## Check an intake vault

An external pipeline can write notes into a retrieval-only vault. Check that
vault against the [intake contract](intake-contract.md):

```bash
ai-memory-check-intake /path/to/vault
```

The command prints JSON with the document count, the conforming count, and each
issue. The exit status is 1 when the vault has an issue. The command only reads.
It does not change the vault.
````

- [ ] **Step 9: Run the privacy tests and the full suite**

Run: `.venv/bin/python -m pytest tests/test_portability.py -q`
Expected: all pass.

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 10: Inspect the staged diff and commit**

```bash
git add src/ai_memory_mcp/intake.py src/ai_memory_mcp/text.py tests/test_intake.py pyproject.toml docs/operations.md
git diff --cached
```

Confirm the diff contains no private term. Then:

```bash
git commit -m "feat: check an intake vault against the contract"
```
