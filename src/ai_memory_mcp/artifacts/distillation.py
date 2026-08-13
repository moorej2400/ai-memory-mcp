from __future__ import annotations

import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from ai_memory_mcp.config import Settings

from .context import active_context_ids
from .identity import artifact_uri as make_artifact_uri
from .identity import parse_artifact_uri
from .models import ArtifactScope, DistillationCandidate
from .schema import connect_artifact_db, migrate_artifact_db

DISTILLED_BEGIN = "%% ai-memory:distilled-begin %%"
DISTILLED_END = "%% ai-memory:distilled-end %%"
TIMESTAMP_SPEAKER_RE = re.compile(
    r"^\s*(?:\[?\d{1,2}:\d{2}(?::\d{2})?\]?)\s+[^:\n]{1,80}:\s+\S"
)
SPEAKER_TURN_RE = re.compile(r"^\s*[^#>\-\d\s][^:\n]{0,60}:\s+\S")
ARTIFACT_LINK_RE = re.compile(r"\]\((artifact://[^)\s]+)\)")
MAX_QUOTED_EVIDENCE_LINES = 12
MAX_QUOTED_EVIDENCE_CHARACTERS = 2400


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _managed_region(markdown: str) -> tuple[int, int, str]:
    begin_count = markdown.count(DISTILLED_BEGIN)
    end_count = markdown.count(DISTILLED_END)
    if begin_count != 1 or end_count != 1:
        raise ValueError(
            "The Markdown note must contain one managed begin marker and one end marker."
        )
    begin = markdown.index(DISTILLED_BEGIN)
    end = markdown.index(DISTILLED_END)
    if begin >= end:
        raise ValueError("The Markdown managed markers are reversed or nested.")
    content_start = begin + len(DISTILLED_BEGIN)
    return content_start, end, markdown[content_start:end]


def replace_managed_distillation(markdown: str, distilled: str) -> str:
    """Replace only the managed region and preserve all manual Markdown."""
    if DISTILLED_BEGIN in distilled or DISTILLED_END in distilled:
        raise ValueError("Managed marker injection is not permitted.")
    content_start, content_end, _ = _managed_region(markdown)
    return (
        markdown[:content_start]
        + "\n\n"
        + distilled.strip()
        + "\n\n"
        + markdown[content_end:]
    )


def _safe_title(value: str, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_value).strip("-").casefold()
    return (slug or fallback)[:80].rstrip("-")


def recommended_distilled_note_path(candidate: DistillationCandidate) -> Path:
    """Return a stable, traversal-safe relative path for a new note."""
    suffix = candidate.artifact_id.removeprefix("art_")[:10]
    title = _safe_title(candidate.title, candidate.entity)
    if candidate.entity == "meeting":
        occurred = candidate.occurred_at or datetime.now(timezone.utc)
        date = occurred.date().isoformat()
        return Path("References") / "Meetings" / date[:4] / (
            f"{date}-{title}-{suffix}.md"
        )
    return Path("References") / "Conversations" / f"{title}-{suffix}.md"


def list_pending_distillations(
    settings: Settings,
    scope: ArtifactScope | None = None,
    limit: int = 20,
) -> list[DistillationCandidate]:
    if limit <= 0:
        raise ValueError("The pending distillation limit must be positive.")
    if not settings.artifact_db.is_file():
        return []
    selected = scope or ArtifactScope()
    conditions = [
        "d.status = 'pending'",
        "a.entity IN ('meeting', 'conversation')",
        "a.deleted_at IS NULL",
        "a.redacted_at IS NULL",
    ]
    parameters: list[Any] = []
    if selected.source is not None:
        conditions.append("a.source = ?")
        parameters.append(selected.source)
    if selected.source_instance is not None:
        conditions.append("a.source_instance = ?")
        parameters.append(selected.source_instance)
    if selected.entities:
        placeholders = ", ".join("?" for _ in selected.entities)
        conditions.append(f"a.entity IN ({placeholders})")
        parameters.extend(selected.entities)
    if selected.date_from is not None:
        conditions.append("a.occurred_at >= ?")
        parameters.append(_utc_iso(selected.date_from))
    if selected.date_to is not None:
        conditions.append("a.occurred_at <= ?")
        parameters.append(_utc_iso(selected.date_to))
    parameters.append(min(limit, 200))
    with connect_artifact_db(
        settings.artifact_db,
        read_only=True,
    ) as connection:
        rows = connection.execute(
            f"""
            SELECT a.*, d.status, d.latest_event_id,
                   d.latest_source_digest
            FROM distillation_state AS d
            JOIN artifacts AS a USING(artifact_id)
            WHERE {' AND '.join(conditions)}
            ORDER BY COALESCE(a.occurred_at, ''), a.artifact_id
            LIMIT ?
            """,
            parameters,
        ).fetchall()
    return [
        DistillationCandidate(
            artifact_id=str(row["artifact_id"]),
            artifact_uri=make_artifact_uri(
                str(row["entity"]),
                str(row["artifact_id"]),
            ),
            entity=str(row["entity"]),
            source=str(row["source"]),
            source_instance=str(row["source_instance"]),
            title=str(row["title"] or str(row["entity"]).title()),
            occurred_at=row["occurred_at"],
            latest_event_id=str(row["latest_event_id"]),
            source_digest=str(row["latest_source_digest"]),
            status=str(row["status"]),
        )
        for row in rows
    ]


def _safe_markdown_path(settings: Settings, memory_path: str) -> Path:
    if (
        not memory_path
        or "\0" in memory_path
        or "\\" in memory_path
        or re.match(r"^[A-Za-z]:", memory_path)
    ):
        raise ValueError("The Markdown note path has an invalid separator or prefix.")
    relative = PurePosixPath(memory_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("The Markdown note path must stay inside the memory root.")
    if relative.suffix.casefold() != ".md":
        raise ValueError("The Markdown note path must end with .md.")
    root = settings.memory_root.expanduser().resolve()
    target = (root / Path(*relative.parts)).resolve()
    if not target.is_relative_to(root):
        raise ValueError("The Markdown note path escapes the memory root.")
    folded = relative.as_posix().casefold()
    if root.is_dir():
        for existing in root.rglob("*.md"):
            existing_relative = existing.relative_to(root).as_posix()
            if (
                existing_relative.casefold() == folded
                and existing_relative != relative.as_posix()
            ):
                raise ValueError(
                    "The Markdown note path has a case-folded path collision."
                )
    return target


def _frontmatter(markdown: str) -> tuple[dict[str, Any], str]:
    if not markdown.startswith("---"):
        raise ValueError("The Markdown note must contain frontmatter.")
    parts = markdown.split("---", 2)
    if len(parts) != 3:
        raise ValueError("The Markdown note frontmatter is incomplete.")
    try:
        metadata = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        raise ValueError("The Markdown note frontmatter is invalid.") from exc
    if not isinstance(metadata, dict):
        raise ValueError("The Markdown note frontmatter must be a mapping.")
    return metadata, parts[2].lstrip()


def _looks_like_transcript(body: str) -> bool:
    lines = [
        line
        for line in body.splitlines()
        if line.strip() and not line.lstrip().startswith(">")
    ]
    if not lines:
        return False
    timestamp_turns = sum(bool(TIMESTAMP_SPEAKER_RE.match(line)) for line in lines)
    speaker_turns = sum(bool(SPEAKER_TURN_RE.match(line)) for line in lines)
    if timestamp_turns >= 12 or speaker_turns / len(lines) >= 0.30:
        return True

    quoted = [
        line.lstrip()[1:].lstrip()
        for line in body.splitlines()
        if line.lstrip().startswith(">") and line.lstrip()[1:].strip()
    ]
    quoted_characters = sum(len(line) for line in quoted)
    quoted_timestamp_turns = sum(
        bool(TIMESTAMP_SPEAKER_RE.match(line)) for line in quoted
    )
    # Quoted evidence stays concise. This separate bound prevents a complete
    # transcript from bypassing detection through Markdown blockquotes.
    return bool(
        len(quoted) > MAX_QUOTED_EVIDENCE_LINES
        or quoted_characters > MAX_QUOTED_EVIDENCE_CHARACTERS
        or quoted_timestamp_turns >= 12
    )


def _validate_note(
    markdown: str,
    *,
    entity: str,
    artifact_reference: str,
    memory_id: str,
    event_id: str,
    source_digest: str,
    allowed_references: set[str],
) -> None:
    metadata, body = _frontmatter(markdown)
    expected = {
        "memory_id": memory_id,
        "source_artifact": artifact_reference,
        "distilled_through_event": event_id,
        "source_digest": source_digest,
    }
    for field, value in expected.items():
        if str(metadata.get(field) or "") != value:
            raise ValueError(f"The Markdown note {field} does not match current state.")
    _, _, managed = _managed_region(body)
    opening = re.split(r"(?m)^##\s+", managed, maxsplit=1)[0]
    summary_lines = [
        line.strip()
        for line in opening.splitlines()
        if line.strip()
        and not line.lstrip().startswith(("#", "-", ">", "%"))
    ]
    if not summary_lines:
        raise ValueError("The Markdown note needs a summary paragraph.")
    evidence_heading = re.search(r"(?m)^## Evidence\s*$", managed)
    evidence_region = ""
    if evidence_heading is not None:
        evidence_region = managed[evidence_heading.end() :]
        next_heading = re.search(r"(?m)^##\s+", evidence_region)
        if next_heading is not None:
            evidence_region = evidence_region[: next_heading.start()]
    links = ARTIFACT_LINK_RE.findall(evidence_region)
    for link in links:
        parse_artifact_uri(link)
        if link not in allowed_references:
            raise ValueError(
                "The Markdown evidence link is outside the artifact context."
            )
    if entity == "meeting" and evidence_heading is None:
        raise ValueError("A meeting Markdown note needs an Evidence section.")
    if entity == "meeting" and not links:
        raise ValueError("A meeting Markdown note needs an artifact evidence link.")
    if evidence_heading is not None and not links:
        raise ValueError("The Evidence section needs an artifact link.")
    if _looks_like_transcript(body):
        raise ValueError("The Markdown note looks like a transcript.")


def _current_state(
    connection: sqlite3.Connection,
    artifact_id: str,
    entity: str,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT a.entity, d.*
        FROM artifacts AS a
        JOIN distillation_state AS d USING(artifact_id)
        WHERE a.artifact_id = ? AND a.entity = ?
          AND a.deleted_at IS NULL AND a.redacted_at IS NULL
        """,
        (artifact_id, entity),
    ).fetchone()
    if row is None:
        raise ValueError("The artifact has no current distillation candidate.")
    return row


def _require_current(
    state: sqlite3.Row,
    event_id: str,
    source_digest: str,
) -> None:
    if (
        state["latest_event_id"] != event_id
        or state["latest_source_digest"] != source_digest
    ):
        raise ValueError("The artifact source changed after this distillation began.")


def mark_distilled(
    settings: Settings,
    artifact_uri: str,
    memory_id: str,
    memory_source_id: str,
    memory_path: str,
    event_id: str,
    source_digest: str,
) -> None:
    entity, artifact_id = parse_artifact_uri(artifact_uri)
    if entity not in {"meeting", "conversation"}:
        raise ValueError("Only a meeting or conversation can be distilled.")
    if memory_source_id != settings.primary_source_id:
        raise ValueError("The Markdown note must use the writable memory source.")
    target = _safe_markdown_path(settings, memory_path)
    if not target.is_file():
        raise ValueError("The Markdown note does not exist under the memory root.")
    markdown = target.read_text(encoding="utf-8-sig")
    migrate_artifact_db(settings)
    with connect_artifact_db(settings.artifact_db) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            state = _current_state(connection, artifact_id, entity)
            _require_current(state, event_id, source_digest)
            context_ids = active_context_ids(
                connection,
                artifact_id,
                entity,
            )
            placeholders = ",".join("?" for _ in context_ids)
            allowed_references = {
                make_artifact_uri(
                    str(row["entity"]),
                    str(row["artifact_id"]),
                )
                for row in connection.execute(
                    "SELECT artifact_id, entity FROM artifacts "
                    f"WHERE artifact_id IN ({placeholders})",
                    sorted(context_ids),
                ).fetchall()
            }
            _validate_note(
                markdown,
                entity=entity,
                artifact_reference=artifact_uri,
                memory_id=memory_id,
                event_id=event_id,
                source_digest=source_digest,
                allowed_references=allowed_references,
            )
            connection.execute(
                """
                UPDATE distillation_state
                SET status = 'distilled',
                    distilled_through_event_id = ?,
                    distilled_source_digest = ?, memory_id = ?,
                    memory_source_id = ?, memory_path = ?,
                    outcome_reason = NULL, updated_at = ?
                WHERE artifact_id = ?
                """,
                (
                    event_id,
                    source_digest,
                    memory_id,
                    memory_source_id,
                    PurePosixPath(memory_path).as_posix(),
                    datetime.now(timezone.utc).isoformat(),
                    artifact_id,
                ),
            )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()


def mark_no_durable_memory(
    settings: Settings,
    artifact_uri: str,
    event_id: str,
    source_digest: str,
    reason: str,
) -> None:
    entity, artifact_id = parse_artifact_uri(artifact_uri)
    if entity != "conversation":
        raise ValueError("Only a conversation can have no durable memory.")
    normalized_reason = reason.strip()
    if not normalized_reason or len(normalized_reason) > 1000:
        raise ValueError("The no-durable-memory reason has an invalid length.")
    migrate_artifact_db(settings)
    with connect_artifact_db(settings.artifact_db) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            state = _current_state(connection, artifact_id, entity)
            _require_current(state, event_id, source_digest)
            connection.execute(
                """
                UPDATE distillation_state
                SET status = 'no-durable-memory',
                    distilled_through_event_id = ?,
                    distilled_source_digest = ?, memory_id = NULL,
                    memory_source_id = NULL, memory_path = NULL,
                    outcome_reason = ?, updated_at = ?
                WHERE artifact_id = ?
                """,
                (
                    event_id,
                    source_digest,
                    normalized_reason,
                    datetime.now(timezone.utc).isoformat(),
                    artifact_id,
                ),
            )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
