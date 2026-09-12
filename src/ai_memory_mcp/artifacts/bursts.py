from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from ai_memory_mcp.text import query_identifiers

from .identity import canonical_json
from .models import ArtifactBurst, ArtifactBurstRecord
from .ordering import source_order_key

MAX_BURST_RECORDS = 8
MAX_BURST_CHARACTERS = 2000
MAX_BURST_GAP = timedelta(minutes=15)
MIN_EMBED_CHARACTERS = 200
REPRESENTATION_VERSION = 3
MAX_SEGMENT_CHARACTERS = 1800
SEGMENT_OVERLAP_CHARACTERS = 240
MAX_CONTEXT_CHARACTERS = 5000
CONTEXT_NEIGHBORS = 2


def _render(records: list[ArtifactBurstRecord]) -> str:
    first = records[0]
    lines: list[str] = []
    if first.parent_title:
        lines.append(first.parent_title.strip())
    participants = list(
        dict.fromkeys(
            name.strip()
            for record in records
            for name in record.participant_names
            if name.strip()
        )
    )
    if participants:
        lines.append(f"Participants: {', '.join(participants)}")
    for record in records:
        prefix = record.author_name or record.author_id
        text = " ".join(record.text.split())
        lines.append(f"{prefix}: {text}" if prefix else text)
    return "\n".join(line for line in lines if line)


def _should_embed(records: list[ArtifactBurstRecord], text: str) -> bool:
    if all(record.classification.casefold() == "system" for record in records):
        return False
    return bool(any(record.text.strip() for record in records))


def _author_key(record: ArtifactBurstRecord) -> str:
    stable_id = record.author_id.strip()
    if stable_id:
        return f"id:{stable_id}"
    display_name = " ".join(record.author_name.split()).casefold()
    return f"name:{display_name}"


def _burst(records: list[ArtifactBurstRecord]) -> ArtifactBurst:
    first = records[0]
    last = records[-1]
    # Raw artifacts retain complete text. Cap only the derived semantic unit so
    # one oversized record cannot bypass the documented burst limit.
    text = _render(records)[:MAX_BURST_CHARACTERS]
    identity = canonical_json(
        {
            "parent": first.parent_artifact_id,
            "author": _author_key(first),
            "first": first.artifact_id,
            "last": last.artifact_id,
            "text": text,
        }
    )
    burst_id = "burst_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return ArtifactBurst(
        burst_id=burst_id,
        source=first.source,
        source_instance=first.source_instance,
        entity=first.entity,
        parent_artifact_id=first.parent_artifact_id,
        parent_title=first.parent_title,
        author_id=first.author_id,
        author_name=first.author_name,
        first_artifact_uri=first.artifact_uri,
        last_artifact_uri=last.artifact_uri,
        started_at=first.occurred_at,
        ended_at=last.occurred_at,
        record_count=len(records),
        text=text,
        embed=_should_embed(records, text),
    )


def group_bursts(records: list[ArtifactBurstRecord]) -> list[ArtifactBurst]:
    """Group ordered message-like records into deterministic semantic runs."""
    active = sorted(
        (
            record
            for record in records
            if not record.deleted and not record.redacted
        ),
        # Parent-first ordering prevents activity in another conversation from
        # splitting one conversation's contiguous semantic run.
        key=lambda record: (
            record.parent_artifact_id,
            _order_key(record),
            record.artifact_id,
        ),
    )
    result: list[ArtifactBurst] = []
    current: list[ArtifactBurstRecord] = []
    for record in active:
        split = False
        if current:
            previous = current[-1]
            prospective = [*current, record]
            split = bool(
                record.parent_artifact_id != previous.parent_artifact_id
                or _author_key(record) != _author_key(previous)
                or _gap_exceeded(previous.occurred_at, record.occurred_at)
                or len(current) >= MAX_BURST_RECORDS
                or len(_render(prospective)) > MAX_BURST_CHARACTERS
            )
        if split:
            result.append(_burst(current))
            current = []
        current.append(record)
    if current:
        result.append(_burst(current))
    return result


def _order_key(record: ArtifactBurstRecord) -> tuple[int, object, str]:
    """Use provider position before time and a stable identity fallback."""
    return source_order_key(record.source_position, record.relative_start_ms,
                            record.occurred_at, record.artifact_id)


def _gap_exceeded(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return False
    return right - left > MAX_BURST_GAP


def segment_source_text(
    text: str,
    *,
    maximum: int = MAX_SEGMENT_CHARACTERS,
    overlap: int = SEGMENT_OVERLAP_CHARACTERS,
) -> list[tuple[int, int]]:
    """Return deterministic spans that cover all nonempty source text."""
    if maximum <= 0 or overlap < 0 or overlap >= maximum:
        raise ValueError("The segment size and overlap are not valid.")
    if not text:
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        hard_end = min(len(text), start + maximum)
        end = hard_end
        if hard_end < len(text):
            minimum = start + maximum // 2
            candidates = [
                text.rfind("\n\n", minimum, hard_end),
                text.rfind("\n", minimum, hard_end),
                max(
                    (
                        start + match.end()
                        for match in re.finditer(r"[.!?](?:\s+|$)", text[start:hard_end])
                        if start + match.end() >= minimum
                    ),
                    default=-1,
                ),
                text.rfind(" ", minimum, hard_end),
            ]
            boundary = max(candidates)
            if boundary >= minimum:
                end = boundary if boundary > start else hard_end
        if end <= start:
            end = hard_end
        spans.append((start, end))
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return spans


def _dependency_digest(records: list[ArtifactBurstRecord]) -> str:
    value = "\n".join(
        f"{record.artifact_id}:{record.source_revision}:{hashlib.sha256(record.text.encode('utf-8')).hexdigest()}"
        for record in records
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _representation(
    anchor: ArtifactBurstRecord,
    records: list[ArtifactBurstRecord],
    *,
    kind: str,
    start: int,
    end: int,
    text: str,
) -> ArtifactBurst:
    dependencies = tuple(record.artifact_uri for record in records)
    digest = _dependency_digest(records)
    identity = canonical_json(
        {
            "anchor": anchor.artifact_id,
            "kind": kind,
            "version": REPRESENTATION_VERSION,
            "start": start,
            "end": end,
            "dependencies": dependencies,
            "dependency_digest": digest,
        }
    )
    return ArtifactBurst(
        burst_id="segment_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32],
        source=anchor.source,
        source_instance=anchor.source_instance,
        entity=anchor.entity,
        parent_artifact_id=anchor.parent_artifact_id,
        parent_title=anchor.parent_title,
        author_id=anchor.author_id,
        author_name=anchor.author_name,
        first_artifact_uri=anchor.artifact_uri,
        last_artifact_uri=anchor.artifact_uri,
        started_at=anchor.occurred_at,
        ended_at=anchor.occurred_at,
        record_count=len(records),
        text=text,
        embed=_should_embed([anchor], text),
        representation_kind=kind,
        representation_version=REPRESENTATION_VERSION,
        anchor_artifact_uri=anchor.artifact_uri,
        dependency_artifact_uris=dependencies,
        dependency_digest=digest,
        segment_start=start,
        segment_end=end,
        source_position=anchor.source_position,
        relative_start_ms=anchor.relative_start_ms,
        relative_end_ms=anchor.relative_end_ms,
        exclusion_reason=(
            "system_noise"
            if anchor.classification.casefold() == "system"
            else "empty_content" if not anchor.text.strip() else ""
        ),
        meeting_artifact_uri=(
            f"artifact://meeting/{anchor.meeting_artifact_id}"
            if anchor.meeting_artifact_id
            else None
        ),
        meeting_occurred_at=anchor.meeting_occurred_at,
    )


def build_representations(
    records: list[ArtifactBurstRecord],
    *,
    anchor_artifact_uris: set[str] | None = None,
) -> list[ArtifactBurst]:
    """Build complete base segments and bounded reply-aware context."""
    active = [
        record for record in records if not record.deleted and not record.redacted
    ]
    by_parent: dict[str, list[ArtifactBurstRecord]] = defaultdict(list)
    by_id = {record.artifact_id: record for record in active}
    for record in active:
        by_parent[record.parent_artifact_id].append(record)
    result: list[ArtifactBurst] = []
    for parent_records in by_parent.values():
        ordered = sorted(parent_records, key=_order_key)
        for position, anchor in enumerate(ordered):
            if (
                anchor_artifact_uris is not None
                and anchor.artifact_uri not in anchor_artifact_uris
            ):
                continue
            spans = segment_source_text(anchor.text)
            for start, end in spans:
                label = anchor.author_name or anchor.author_id
                source_text = anchor.text[start:end]
                rendered = f"{label}: {source_text}" if label else source_text
                result.append(
                    _representation(
                        anchor,
                        [anchor],
                        kind="base",
                        start=start,
                        end=end,
                        text=rendered,
                    )
                )
            if not spans or anchor.classification.casefold() == "system":
                continue
            context_records: list[ArtifactBurstRecord] = []
            target = by_id.get(anchor.reply_target_artifact_id or "")
            if target is not None and target.parent_artifact_id == anchor.parent_artifact_id:
                context_records.append(target)
            lower = max(0, position - CONTEXT_NEIGHBORS)
            upper = min(len(ordered), position + CONTEXT_NEIGHBORS + 1)
            for neighbor in ordered[lower:upper]:
                if neighbor.artifact_id == anchor.artifact_id:
                    context_records.append(neighbor)
                elif (
                    anchor.occurred_at is not None
                    and neighbor.occurred_at is not None
                    and abs(anchor.occurred_at - neighbor.occurred_at)
                    <= MAX_BURST_GAP
                ):
                    context_records.append(neighbor)
                elif anchor.occurred_at is None or neighbor.occurred_at is None:
                    if anchor.relative_start_ms is not None and neighbor.relative_start_ms is not None:
                        nearby = abs(anchor.relative_start_ms - neighbor.relative_start_ms) <= MAX_BURST_GAP.total_seconds() * 1000
                    else:
                        # Missing wall-clock times do not erase adjacent source
                        # context. The same-parent window remains at most two rows.
                        nearby = anchor.source_position is not None and neighbor.source_position is not None
                    if nearby:
                        context_records.append(neighbor)
            context_records = list(
                {record.artifact_id: record for record in context_records}.values()
            )
            if len(context_records) <= 1:
                continue
            context_records.sort(key=_order_key)
            neighbors = [record for record in context_records if record != anchor]
            if target in neighbors:
                neighbors.remove(target)
                neighbors.insert(0, target)
            label = (anchor.author_name or anchor.author_id or "Unknown speaker")[:128]
            prefix = f"Anchor | {label}: "
            # Context uses the same complete source spans as base retrieval.
            # Reserve room for the explicit reply instead of embedding a whole
            # long record again and silently losing its conversational context.
            context_spans = segment_source_text(
                anchor.text, maximum=MAX_CONTEXT_CHARACTERS - len(prefix) - 1000
            )
            for start, end in context_spans:
                context_text = prefix + anchor.text[start:end]
                included = [anchor]
                for record in neighbors:
                    remaining = MAX_CONTEXT_CHARACTERS - len(context_text) - 1
                    neighbor_label = (
                        record.author_name or record.author_id or "Unknown speaker"
                    )[:128]
                    line = f"Context | {neighbor_label}: {record.text}"
                    if remaining <= len(f"Context | {neighbor_label}: "):
                        break
                    context_text += "\n" + line[:remaining]
                    included.append(record)
                result.append(
                    _representation(
                        anchor,
                        sorted(included, key=_order_key),
                        kind="context",
                        start=start,
                        end=end,
                        text=context_text,
                    )
                )
    return sorted(result, key=lambda item: item.burst_id)
