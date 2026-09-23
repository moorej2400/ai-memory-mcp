from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import yaml

from .wikilinks import identity_keys, resolve_link, wikilink_targets


CURRENT_MEMORY_SCHEMA_VERSION = 2
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class IntakeIssue:
    path: str
    rule: str
    severity: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class InspectedNote:
    path: str
    metadata: dict[str, Any]
    body: str
    issues: tuple[IntakeIssue, ...]


def parse_markdown(raw: str) -> tuple[dict[str, Any], str, str | None]:
    if not raw.startswith("---"):
        return {}, raw, "The note has no YAML frontmatter."
    parts = raw.split("---", 2)
    if len(parts) != 3:
        return {}, raw, "The YAML frontmatter is incomplete."
    try:
        metadata = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        return {}, parts[2].lstrip(), f"The YAML frontmatter is invalid: {exc}"
    if not isinstance(metadata, dict):
        return {}, parts[2].lstrip(), "The YAML frontmatter must be a mapping."
    return metadata, parts[2].lstrip(), None


def _valid_date(value: Any) -> bool:
    if type(value) is date:
        return True
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _issue(path: str, rule: str, message: str, severity: str = "error") -> IntakeIssue:
    return IntakeIssue(path=path, rule=rule, severity=severity, message=message)


def inspect_markdown(
    raw: str,
    *,
    path: str,
    require_current: bool = False,
) -> InspectedNote:
    metadata, body, frontmatter_error = parse_markdown(raw)
    issues: list[IntakeIssue] = []
    if frontmatter_error:
        issues.append(_issue(path, "frontmatter", frontmatter_error))
        return InspectedNote(path, metadata, body, tuple(issues))

    schema_version = metadata.get("schema_version")
    if schema_version != CURRENT_MEMORY_SCHEMA_VERSION:
        issues.append(
            _issue(
                path,
                "schema-version",
                f"The note uses schema version {schema_version!r}; version 2 is current.",
                "error" if require_current else "warning",
            )
        )

    required = ["memory_id", "title", "type", "status", "created", "updated"]
    if schema_version == CURRENT_MEMORY_SCHEMA_VERSION or require_current:
        required.extend(("domain", "record_type", "provenance"))
    for field in required:
        value = metadata.get(field)
        if value is None or value == "" or value == []:
            issues.append(_issue(path, "required-field", f"The {field} field is required."))

    for field in ("created", "updated", "review_after"):
        value = metadata.get(field)
        if value not in (None, "") and not _valid_date(value):
            issues.append(_issue(path, "date-format", f"The {field} field must use YYYY-MM-DD."))

    title = metadata.get("title")
    h1 = H1_RE.search(body)
    if isinstance(title, str) and title.strip():
        if h1 is None or h1.group(1).strip() != title.strip():
            issues.append(_issue(path, "title-h1", "The title and H1 must match."))

    if h1 is not None:
        after_h1 = body[h1.end() :]
        next_heading = re.search(r"(?m)^#{1,6}\s+", after_h1)
        summary = after_h1[: next_heading.start()] if next_heading else after_h1
        if not any(line.strip() for line in summary.splitlines()):
            issues.append(_issue(path, "summary", "The note needs a summary after its H1."))

    for field in ("related", "aliases", "provenance"):
        value = metadata.get(field)
        if value is not None and not isinstance(value, list):
            issues.append(_issue(path, "field-type", f"The {field} field must be a list."))

    scope_kind = metadata.get("scope_kind")
    scope_id = metadata.get("scope_id")
    if bool(scope_kind) != bool(scope_id):
        issues.append(
            _issue(path, "scope-pair", "The scope_kind and scope_id fields must occur together.")
        )
    return InspectedNote(path, metadata, body, tuple(issues))


def _markdown_paths(root: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in root.rglob("*.md")
            if not any(part.startswith(".") for part in path.relative_to(root).parts)
            and not any(part.casefold() == "restricted" for part in path.relative_to(root).parts)
        ),
        key=lambda value: value.as_posix().casefold(),
    )


def inspect_vault(root: Path, *, require_current: bool = False) -> dict[str, Any]:
    notes: list[InspectedNote] = []
    issues: list[IntakeIssue] = []
    for note_path in _markdown_paths(root):
        relative = note_path.relative_to(root).as_posix()
        inspected = inspect_markdown(
            note_path.read_text(encoding="utf-8-sig"),
            path=relative,
            require_current=require_current,
        )
        notes.append(inspected)
        issues.extend(inspected.issues)

    ids = Counter(
        str(note.metadata.get("memory_id"))
        for note in notes
        if note.metadata.get("memory_id")
    )
    for note in notes:
        memory_id = str(note.metadata.get("memory_id") or "")
        if memory_id and ids[memory_id] > 1:
            issues.append(_issue(note.path, "memory-id-unique", f"The memory_id {memory_id!r} is not unique."))

    candidates: dict[str, set[str]] = defaultdict(set)
    for note in notes:
        metadata = note.metadata
        memory_id = str(metadata.get("memory_id") or f"path:{note.path.casefold()}")
        identifiers = metadata.get("identifiers") if isinstance(metadata.get("identifiers"), list) else []
        aliases = metadata.get("aliases") if isinstance(metadata.get("aliases"), list) else []
        for key in identity_keys(
            memory_id=memory_id,
            title=str(metadata.get("title") or Path(note.path).stem),
            path=note.path,
            identifiers=identifiers,
            aliases=aliases,
        ):
            candidates[key].add(memory_id)

    for note in notes:
        values: list[str] = [note.body]
        related = note.metadata.get("related")
        if isinstance(related, list):
            values.extend(str(item) for item in related)
        for target in dict.fromkeys(wikilink_targets(values)):
            _, state = resolve_link(target, candidates)
            if state not in {"resolved", "ignored"}:
                issues.append(
                    _issue(
                        note.path,
                        f"link-{state}",
                        f"The link target {target!r} is {state}.",
                        "warning",
                    )
                )

    counts = Counter(issue.rule for issue in issues)
    severity_counts = Counter(issue.severity for issue in issues)
    return {
        "root": str(root),
        "notes": len(notes),
        "current_schema_notes": sum(
            note.metadata.get("schema_version") == CURRENT_MEMORY_SCHEMA_VERSION
            for note in notes
        ),
        "issues": [issue.to_dict() for issue in issues],
        "issue_counts": dict(sorted(counts.items())),
        "error_count": severity_counts["error"],
        "warning_count": severity_counts["warning"],
    }


def validate_new_markdown(raw: str, *, path: str) -> dict[str, Any]:
    inspected = inspect_markdown(raw, path=path, require_current=True)
    errors = [issue for issue in inspected.issues if issue.severity == "error"]
    if errors:
        details = "; ".join(f"{issue.rule}: {issue.message}" for issue in errors)
        raise ValueError(f"The memory record is invalid: {details}")
    return inspected.metadata
