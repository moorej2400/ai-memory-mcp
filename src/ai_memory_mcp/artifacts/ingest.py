from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
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
        "api_key",
        "authorization",
        "bearer_token",
        "client_secret",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "encrypted_token",
        "id_token",
        "password",
        "private_key",
        "refresh_token",
        "sas_token",
        "secret",
        "session_token",
        "shared_access_signature",
        "tempauth",
        "temporarydownloadurl",
    }
)
AUTH_QUERY_KEYS = frozenset(
    {
        "access_token",
        "auth",
        "authorization",
        "client_secret",
        "code",
        "credential",
        "key",
        "password",
        "sig",
        "signature",
        "tempauth",
        "token",
        "x_amz_credential",
        "x_amz_security_token",
        "x_amz_signature",
        "x_goog_credential",
        "x_goog_signature",
    }
)
HTTP_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
MAX_NESTED_URL_DEPTH = 8


def _normalize_security_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


# Connector envelopes are untrusted. Normalize key spelling before comparison.
SECRET_KEY_TOKENS = frozenset(_normalize_security_key(key) for key in SECRET_KEYS)
AUTH_QUERY_KEY_TOKENS = frozenset(
    _normalize_security_key(key) for key in AUTH_QUERY_KEYS
)


def _reject_url_credentials(value: str, path: str, *, depth: int) -> None:
    if depth > MAX_NESTED_URL_DEPTH:
        raise ValueError("Artifact input contains excessive nested URLs.")
    try:
        parsed = urlsplit(value)
    except ValueError:
        return
    if parsed.scheme.casefold() not in {"http", "https"}:
        return
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"Artifact input contains URL credentials at {path}.")
    for key, child in parse_qsl(parsed.query, keep_blank_values=True):
        if _normalize_security_key(key) in AUTH_QUERY_KEY_TOKENS:
            raise ValueError(
                f"Artifact input contains an authentication query parameter: {key}."
            )
        _reject_nested_query_material(child, path, depth=depth + 1)
    fragment_query = parsed.fragment.partition("?")[2]
    for key, child in parse_qsl(fragment_query, keep_blank_values=True):
        if _normalize_security_key(key) in AUTH_QUERY_KEY_TOKENS:
            raise ValueError(
                f"Artifact input contains an authentication query parameter: {key}."
            )
        _reject_nested_query_material(child, path, depth=depth + 1)
    _reject_secret_text(parsed.fragment, path, depth=depth + 1)


def _reject_nested_query_material(value: str, path: str, *, depth: int) -> None:
    if depth > MAX_NESTED_URL_DEPTH:
        raise ValueError("Artifact input contains excessive nested URLs.")
    # Redirect parameters can hide encoded absolute or relative signed URLs.
    _reject_secret_text(value, path, depth=depth)
    _, marker, query_text = value.partition("?")
    if (
        not marker
        and "=" in value
        and not any(character.isspace() for character in value)
    ):
        query_text = value
    if not query_text:
        return
    for key, child in parse_qsl(query_text, keep_blank_values=True):
        if _normalize_security_key(key) in AUTH_QUERY_KEY_TOKENS:
            raise ValueError(
                f"Artifact input contains an authentication query parameter: {key}."
            )
        _reject_nested_query_material(child, path, depth=depth + 1)


def _reject_secret_text(value: str, path: str, *, depth: int = 0) -> None:
    for match in HTTP_URL_RE.finditer(value):
        candidate = match.group(0).rstrip(".,;!)]}")
        _reject_url_credentials(candidate, path, depth=depth)


def _reject_secret_material(value: Any, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _normalize_security_key(str(key)) in SECRET_KEY_TOKENS:
                raise ValueError(
                    f"Artifact input contains a secret field at {path}.{key}."
                )
            _reject_secret_material(child, f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            _reject_secret_material(child, f"{path}[{index}]")
        return
    if not isinstance(value, str):
        return
    _reject_secret_text(value, path)


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
        raise ValueError(f"Artifact batch line {first_line} must contain the manifest.")
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
        raise ValueError("Artifact batch event count does not match the manifest.")
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
