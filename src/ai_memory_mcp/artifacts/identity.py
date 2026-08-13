from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

SOURCE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
INSTANCE_PATTERN = SOURCE_PATTERN
ENTITY_PATTERN = SOURCE_PATTERN
ARTIFACT_ID_PATTERN = re.compile(r"^art_[a-z2-7]{32}$")
EVENT_ID_PATTERN = re.compile(r"^evt_[a-z2-7]{32}$")
BATCH_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _validated_label(value: str, pattern: re.Pattern[str], name: str) -> str:
    if not pattern.fullmatch(value):
        raise ValueError(
            f"Artifact {name} must start with a lowercase letter and contain "
            "only lowercase letters, numbers, or hyphens."
        )
    return value


def _encoded_digest(payload: bytes) -> str:
    digest = hashlib.sha256(payload).digest()[:20]
    return base64.b32encode(digest).decode("ascii").rstrip("=").lower()


def artifact_id(
    source: str,
    source_instance: str,
    entity: str,
    external_id: str,
) -> str:
    """Return a path-safe identity for one connector artifact."""
    _validated_label(source, SOURCE_PATTERN, "source")
    _validated_label(source_instance, INSTANCE_PATTERN, "source instance")
    _validated_label(entity, ENTITY_PATTERN, "entity")
    if not external_id or len(external_id) > 2048:
        raise ValueError(
            "Artifact external ID must contain between 1 and 2048 characters."
        )
    payload = "\0".join((source, source_instance, entity, external_id))
    return "art_" + _encoded_digest(payload.encode("utf-8"))


def event_id(
    source: str,
    source_instance: str,
    event: Mapping[str, Any],
) -> str:
    """Return a replay-stable event ID that excludes delivery metadata."""
    _validated_label(source, SOURCE_PATTERN, "source")
    _validated_label(source_instance, INSTANCE_PATTERN, "source instance")
    required = ("entity", "external_id", "operation", "payload_sha256")
    missing = [name for name in required if not event.get(name)]
    if missing:
        raise ValueError(f"Artifact event identity is missing: {', '.join(missing)}")
    ordering = (
        event.get("source_sequence")
        if event.get("source_sequence") is not None
        else event.get("source_updated_at")
        or event.get("source_version")
        or event["payload_sha256"]
    )
    values = (
        source,
        source_instance,
        str(event["entity"]),
        str(event["external_id"]),
        str(event["operation"]),
        str(ordering),
        str(event["payload_sha256"]),
    )
    return "evt_" + _encoded_digest("\0".join(values).encode("utf-8"))


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def artifact_uri(entity: str, value: str) -> str:
    _validated_label(entity, ENTITY_PATTERN, "entity")
    if not ARTIFACT_ID_PATTERN.fullmatch(value):
        raise ValueError("Artifact ID has an invalid format.")
    return f"artifact://{entity}/{value}"


def parse_artifact_uri(value: str) -> tuple[str, str]:
    parsed = urlparse(value)
    entity = parsed.netloc
    artifact = parsed.path.removeprefix("/")
    if (
        parsed.scheme != "artifact"
        or not ENTITY_PATTERN.fullmatch(entity)
        or not ARTIFACT_ID_PATTERN.fullmatch(artifact)
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Artifact URI has an invalid format.")
    return entity, artifact
