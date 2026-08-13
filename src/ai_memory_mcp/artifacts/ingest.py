from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from io import TextIOBase
from typing import Any, TextIO
from urllib.parse import parse_qsl, urlsplit

from pydantic import ValidationError

from ai_memory_mcp.config import Settings

from .models import (
    ArtifactBatchManifest,
    ArtifactEvent,
    ArtifactIngestReceipt,
    ParsedArtifactBatch,
)

SECRET_KEYS = frozenset(
    {
        "access_token",
        "authorization",
        "cookie",
        "cookies",
        "refresh_token",
        "tempauth",
        "temporarydownloadurl",
    }
)
AUTH_QUERY_KEYS = frozenset(
    {
        "access_token",
        "auth",
        "authorization",
        "key",
        "sig",
        "signature",
        "tempauth",
        "token",
    }
)


def _reject_secret_material(value: Any, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).replace("-", "_").casefold()
            if normalized in SECRET_KEYS:
                raise ValueError(
                    f"Artifact input contains a secret field at {path}.{key}."
                )
            _reject_secret_material(child, f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for index, child in enumerate(value):
            _reject_secret_material(child, f"{path}[{index}]")
        return
    if not isinstance(value, str):
        return
    parsed = urlsplit(value)
    if parsed.scheme.casefold() not in {"http", "https"}:
        return
    query_keys = {key.casefold() for key, _ in parse_qsl(parsed.query)}
    prohibited = sorted(query_keys & AUTH_QUERY_KEYS)
    if prohibited:
        raise ValueError(
            "Artifact input contains an authentication query parameter: "
            f"{prohibited[0]}."
        )


def read_artifact_batch(
    stream: TextIO,
    *,
    max_bytes: int | None = None,
) -> ParsedArtifactBatch:
    """Parse and validate a complete artifact JSONL batch before storage."""
    raw = stream.read()
    if not isinstance(raw, str):
        raise TypeError("Artifact batch input must be a text stream.")
    raw_bytes = raw.encode("utf-8")
    if max_bytes is not None:
        if max_bytes <= 0:
            raise ValueError("The artifact batch size limit must be positive.")
        if len(raw_bytes) > max_bytes:
            raise ValueError("The artifact batch exceeds the configured size limit.")

    records: list[tuple[int, dict[str, Any]]] = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Artifact batch line {line_number} contains invalid JSON."
            ) from exc
        if not isinstance(value, dict):
            raise ValueError(
                f"Artifact batch line {line_number} must contain a JSON object."
            )
        _reject_secret_material(value)
        records.append((line_number, value))

    if not records:
        raise ValueError("Artifact batch input does not contain a manifest.")
    first_line, first = records[0]
    if first.get("record") != "batch":
        raise ValueError(
            f"Artifact batch line {first_line} must contain the manifest."
        )
    try:
        manifest = ArtifactBatchManifest.model_validate(first)
    except ValidationError as exc:
        raise ValueError("Artifact batch manifest is invalid.") from exc

    events: list[ArtifactEvent] = []
    for line_number, value in records[1:]:
        if value.get("record") == "batch":
            raise ValueError(
                f"Artifact batch line {line_number} contains another manifest."
            )
        if value.get("record") != "event":
            raise ValueError(
                f"Artifact batch line {line_number} must contain an event."
            )
        try:
            events.append(ArtifactEvent.model_validate(value))
        except ValidationError as exc:
            raise ValueError(
                f"Artifact event on line {line_number} is invalid."
            ) from exc

    if len(events) != manifest.event_count:
        raise ValueError(
            "Artifact batch event count does not match the manifest."
        )
    return ParsedArtifactBatch(
        manifest=manifest,
        events=events,
        input_sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )


def ingest_artifact_batch(
    settings: Settings,
    batch: ParsedArtifactBatch,
) -> ArtifactIngestReceipt:
    """Store one fully parsed artifact batch."""
    from .store import ArtifactStore

    return ArtifactStore(settings).apply_batch(batch)
