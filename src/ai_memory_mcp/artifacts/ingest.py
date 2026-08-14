from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, BinaryIO
from urllib.parse import parse_qsl, unquote, urlsplit

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
        "auth",
        "authorization",
        "bearer_token",
        "client_secret",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "encrypted_token",
        "id_token",
        "jwt",
        "password",
        "private_key",
        "refresh_token",
        "sas_token",
        "secret",
        "session_token",
        "shared_access_signature",
        "tempauth",
        "temporarydownloadurl",
        "token",
        "x_amz_credential",
        "x_amz_security_token",
        "x_amz_signature",
        "x_goog_credential",
        "x_goog_signature",
    }
)
URL_AUTH_QUERY_KEYS = frozenset(
    {
        "code",
        "key",
        "se",
        "sig",
        "signature",
        "ske",
        "skoid",
        "skt",
        "sktid",
        "skv",
        "sp",
        "spr",
        "sr",
        "sv",
        "x_amz_algorithm",
        "x_amz_date",
        "x_amz_expires",
        "x_amz_signedheaders",
        "x_goog_algorithm",
        "x_goog_date",
        "x_goog_expires",
        "x_goog_signedheaders",
    }
)
AUTH_QUERY_KEYS = SECRET_KEYS | URL_AUTH_QUERY_KEYS
HTTP_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
PROTOCOL_RELATIVE_URL_RE = re.compile(
    r"(?<![A-Za-z0-9:/])//[^\s<>\"']+",
    re.IGNORECASE,
)
SPECIAL_HTTP_URL_RE = re.compile(r"https?:[\\/]+[^\s<>\"']+", re.IGNORECASE)
URL_QUERY_ASSIGNMENT_RE = re.compile(
    r"(?:[?&#])[ \t]*(?:amp;)?[\"']?([A-Za-z0-9_.-]+)[\"']?[ \t]*=",
    re.IGNORECASE,
)
MAX_NESTED_URL_DEPTH = 8


def _normalize_security_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _decode_html_url_entities(value: str) -> str:
    candidate = value
    for _ in range(MAX_NESTED_URL_DEPTH):
        decoded = html.unescape(candidate)
        if decoded == candidate:
            return candidate
        candidate = decoded
    if html.unescape(candidate) != candidate:
        raise ValueError("Artifact input contains excessive nested URL encoding.")
    return candidate


def _normalize_special_http_url(value: str) -> str:
    """Normalize browser-compatible slash forms for security inspection only."""
    scheme, separator, remainder = value.partition(":")
    if not separator:
        return value
    normalized_remainder = remainder.lstrip("\\/").replace("\\", "/")
    return f"{scheme}://{normalized_remainder}"


# Connector envelopes are untrusted. Normalize key spelling before comparison.
SECRET_KEY_TOKENS = frozenset(_normalize_security_key(key) for key in SECRET_KEYS)
AUTH_QUERY_KEY_TOKENS = frozenset(
    _normalize_security_key(key) for key in AUTH_QUERY_KEYS
)


def _assignment_key_pattern(key: str) -> str:
    return r"[-_.]?".join(re.escape(part) for part in key.split("_"))


def _assignment_key_choices(keys: set[str] | frozenset[str]) -> str:
    return "|".join(
        _assignment_key_pattern(key)
        for key in sorted(keys, key=lambda item: (-len(item), item))
    )


_AUTH_HEADER_KEYS = frozenset({"auth", "authorization"})
_COOKIE_HEADER_KEYS = frozenset({"cookie", "cookies"})
_COMPACT_SECRET_KEYS = SECRET_KEYS - _AUTH_HEADER_KEYS - _COOKIE_HEADER_KEYS
_AUTH_HEADER_ASSIGNMENT_KEYS = _assignment_key_choices(_AUTH_HEADER_KEYS)
_COOKIE_HEADER_ASSIGNMENT_KEYS = _assignment_key_choices(_COOKIE_HEADER_KEYS)
_COMPACT_SECRET_ASSIGNMENT_KEYS = _assignment_key_choices(_COMPACT_SECRET_KEYS)
# Only treat line- or field-delimited text as an assignment. This boundary lets
# normal prose discuss a token without making the connector batch invalid.
SECRET_ASSIGNMENT_RE = re.compile(
    rf"(?im)(?P<prefix>^|[\r\n{{\[,;])(?P<indent>[ \t]*)"
    r"(?:"
    rf"[\"']?(?:{_AUTH_HEADER_ASSIGNMENT_KEYS})[\"']?"
    r"(?![A-Za-z0-9_.-])[ \t]*(?:=|:)[ \t]*"
    r"(?:Bearer|Basic|Digest|Negotiate|Token|AWS4-HMAC-SHA256)"
    r"[ \t]+[^\s,;]+"
    r"|"
    rf"[\"']?(?:{_COOKIE_HEADER_ASSIGNMENT_KEYS})[\"']?"
    r"(?![A-Za-z0-9_.-])"
    r"[ \t]*(?:=|:)[ \t]*[^\s,;]+"
    r"|"
    rf"[\"']?(?:{_COMPACT_SECRET_ASSIGNMENT_KEYS})[\"']?"
    r"(?![A-Za-z0-9_.-])"
    r"[ \t]*(?:=|:)[ \t]*[^\s,;]+"
    r")"
)


def _reject_url_credentials(value: str, path: str, *, depth: int) -> None:
    if depth > MAX_NESTED_URL_DEPTH:
        raise ValueError("Artifact input contains excessive nested URLs.")
    value = _decode_html_url_entities(value)
    parse_value = f"https:{value}" if value.startswith("//") else value
    try:
        parsed = urlsplit(parse_value)
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
    if not fragment_query and "=" in parsed.fragment:
        fragment_query = parsed.fragment.lstrip("#")
    for key, child in parse_qsl(fragment_query, keep_blank_values=True):
        if _normalize_security_key(key) in AUTH_QUERY_KEY_TOKENS:
            raise ValueError(
                f"Artifact input contains an authentication query parameter: {key}."
            )
        _reject_nested_query_material(child, path, depth=depth + 1)
    _reject_nested_query_material(parsed.fragment, path, depth=depth + 1)


def _reject_nested_query_material(value: str, path: str, *, depth: int) -> None:
    if depth > MAX_NESTED_URL_DEPTH:
        raise ValueError("Artifact input contains excessive nested URLs.")
    value = _decode_html_url_entities(value)
    # Redirect parameters can hide encoded absolute or relative signed URLs.
    _reject_secret_text(value, path, depth=depth)
    _reject_structured_secret_text(value, path, depth=depth)
    decoded = unquote(value)
    if decoded != value:
        _reject_nested_query_material(decoded, path, depth=depth + 1)
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


def _reject_json_secret_text(value: str, path: str) -> None:
    stripped = value.strip()
    if stripped.startswith(("{", "[")):
        try:
            document = json.loads(stripped)
        except (TypeError, ValueError):
            document = None
        if document is not None:
            _reject_secret_material(document, path)


def _reject_structured_secret_text(value: str, path: str, *, depth: int) -> None:
    """Reject encoded JSON or colon assignments that contain secret keys."""
    _reject_json_secret_text(value, path)
    for match in re.finditer(r"[\"']?([A-Za-z0-9_.-]+)[\"']?\s*:", value):
        key = match.group(1)
        if _normalize_security_key(key) in AUTH_QUERY_KEY_TOKENS:
            raise ValueError(
                f"Artifact input contains an authentication query parameter: {key}."
            )


def _reject_secret_text(value: str, path: str, *, depth: int = 0) -> None:
    if depth > MAX_NESTED_URL_DEPTH:
        raise ValueError("Artifact input contains excessive nested encoding.")
    value = _decode_html_url_entities(value)
    _reject_json_secret_text(value, path)
    if SECRET_ASSIGNMENT_RE.search(value):
        raise ValueError(f"Artifact input contains a secret assignment at {path}.")
    # Query delimiters identify URL parameters in absolute, relative, Markdown,
    # HTML, schemeless, and browser-normalized URL forms.
    for match in URL_QUERY_ASSIGNMENT_RE.finditer(value):
        key = match.group(1)
        if _normalize_security_key(key) in AUTH_QUERY_KEY_TOKENS:
            raise ValueError(
                "Artifact input contains an authentication query parameter: "
                f"{key}."
            )
    decoded = unquote(value)
    if decoded != value:
        _reject_secret_text(decoded, path, depth=depth + 1)
    for match in HTTP_URL_RE.finditer(value):
        candidate = match.group(0).rstrip(".,;!)]}")
        _reject_url_credentials(candidate, path, depth=depth)
    for match in PROTOCOL_RELATIVE_URL_RE.finditer(value):
        candidate = match.group(0).rstrip(".,;!)]}")
        _reject_url_credentials(candidate, path, depth=depth)
    for match in SPECIAL_HTTP_URL_RE.finditer(value):
        candidate = match.group(0).rstrip(".,;!)]}")
        _reject_url_credentials(
            _normalize_special_http_url(candidate),
            path,
            depth=depth,
        )


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
    stream: BinaryIO,
    *,
    max_bytes: int | None = None,
) -> ParsedArtifactBatch:
    """Parse and validate a complete artifact JSONL batch before storage."""
    if max_bytes is not None:
        if max_bytes <= 0:
            raise ValueError("The artifact batch size limit must be positive.")
        raw_bytes = stream.read(max_bytes + 1)
    else:
        raw_bytes = stream.read()
    if not isinstance(raw_bytes, bytes):
        raise TypeError("Artifact batch input must be a binary stream.")
    if max_bytes is not None and len(raw_bytes) > max_bytes:
        raise ValueError("The artifact batch exceeds the configured size limit.")
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("The artifact batch must use UTF-8 encoding.") from exc

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
