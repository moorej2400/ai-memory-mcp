from __future__ import annotations

import hashlib
from datetime import timedelta

from ai_memory_mcp.text import query_identifiers

from .identity import canonical_json
from .models import ArtifactBurst, ArtifactBurstRecord

MAX_BURST_RECORDS = 8
MAX_BURST_CHARACTERS = 2000
MAX_BURST_GAP = timedelta(minutes=15)
MIN_EMBED_CHARACTERS = 200


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
    source_text = "\n".join(record.text for record in records)
    return bool(
        len(text) >= MIN_EMBED_CHARACTERS
        or query_identifiers(source_text)
        or any(record.reactions for record in records)
        or any(record.attachment_link for record in records)
    )


def _burst(records: list[ArtifactBurstRecord]) -> ArtifactBurst:
    first = records[0]
    last = records[-1]
    text = _render(records)
    identity = canonical_json(
        {
            "parent": first.parent_artifact_id,
            "author": first.author_id,
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
        key=lambda record: (record.occurred_at, record.artifact_id),
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
                or record.author_id != previous.author_id
                or record.occurred_at - previous.occurred_at > MAX_BURST_GAP
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
