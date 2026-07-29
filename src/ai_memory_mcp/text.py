from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import yaml

from .models import MemoryChunk, MemoryDocument

TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/#@+-]*")
IDENTIFIER_RE = re.compile(
    r"\b(?:[A-Z][A-Z0-9]{1,12}-\d+|PR\s*#?\d+|#[0-9]{2,}|"
    r"[A-Za-z]:\\[^\s`]+|(?:[\w.-]+/)+[\w.-]+)\b",
    re.IGNORECASE,
)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
WIKILINK_RE = re.compile(r"\[\[([^]|]+)(?:\|[^]]+)?]]")


def normalize_token(value: str) -> str:
    return value.casefold().strip("._:/#@+-")


def tokenize(value: str) -> list[str]:
    return [
        normalized
        for match in TOKEN_RE.finditer(value)
        if (normalized := normalize_token(match.group(0)))
    ]


def query_identifiers(value: str) -> list[str]:
    return list(dict.fromkeys(match.group(0).strip() for match in IDENTIFIER_RE.finditer(value)))


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _as_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) != 3:
        return {}, raw
    try:
        metadata = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        metadata = {}
    return metadata if isinstance(metadata, dict) else {}, parts[2].lstrip()


def parse_document(path: Path, root: Path, source_id: str = "core") -> MemoryDocument:
    raw = path.read_text(encoding="utf-8-sig")
    metadata, body = _frontmatter(raw)
    relative = path.relative_to(root).as_posix()
    source_path = f"{source_id}/{relative}"
    h1 = re.search(r"^#\s+(.+?)\s*$", body, re.MULTILINE)
    title = str(metadata.get("title") or (h1.group(1) if h1 else path.stem))
    primary = metadata.get("primary_scope") or {}
    if not isinstance(primary, dict):
        primary = {}
    related = _as_strings(metadata.get("related"))
    identifiers = query_identifiers(raw)
    identifiers.extend(
        str(primary.get("id", "")).split()
        if primary.get("id")
        else []
    )
    return MemoryDocument(
        memory_id=str(metadata.get("memory_id") or f"path:{source_path.casefold()}"),
        source_id=source_id,
        path=source_path,
        title=title,
        body=body,
        status=str(metadata.get("status") or "active"),
        root_scope=str(metadata.get("root_scope") or "work"),
        scope_kind=str(primary.get("kind") or "reference"),
        scope_id=str(primary.get("id") or ""),
        updated=str(metadata.get("updated") or ""),
        review_after=str(metadata.get("review_after") or ""),
        related=related,
        identifiers=list(dict.fromkeys(identifiers)),
        projects=_as_strings(metadata.get("related_projects")),
        repos=_as_strings(metadata.get("related_repos")),
        tools=_as_strings(metadata.get("related_tools")),
        content_hash=content_hash(raw),
        mtime_ns=path.stat().st_mtime_ns,
    )


def split_sections(body: str) -> list[tuple[str, str]]:
    matches = list(HEADING_RE.finditer(body))
    if not matches:
        return [("", body.strip())] if body.strip() else []
    sections: list[tuple[str, str]] = []
    preface = body[: matches[0].start()].strip()
    if preface:
        sections.append(("", preface))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        text = body[match.end() : end].strip()
        if text or match.group(2):
            sections.append((match.group(2).strip(), text))
    return sections


def semantic_vector(text: str, dimensions: int) -> dict[int, float]:
    """Create deterministic sparse semantic features without a model dependency.

    Word unigrams preserve entities while character trigrams recover spelling and
    inflection variants. An embedding provider can replace this function later
    without changing the retrieval or MCP contracts.
    """
    words = tokenize(text)
    features: Counter[int] = Counter()
    for word in words:
        variants = [f"w:{word}"]
        padded = f"  {word} "
        variants.extend(f"c:{padded[i:i + 3]}" for i in range(max(1, len(padded) - 2)))
        for feature in variants:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            features[int.from_bytes(digest, "little") % dimensions] += 1
    norm = math.sqrt(sum(value * value for value in features.values())) or 1.0
    return {key: value / norm for key, value in features.items()}


def chunk_document(document: MemoryDocument, dimensions: int) -> list[MemoryChunk]:
    chunks: list[MemoryChunk] = []
    for ordinal, (heading, text) in enumerate(split_sections(document.body)):
        contextual = "\n".join(
            part for part in (document.title, heading, text) if part
        )
        chunks.append(
            MemoryChunk(
                chunk_id=f"{document.memory_id}:{ordinal}",
                memory_id=document.memory_id,
                source_id=document.source_id,
                path=document.path,
                title=document.title,
                heading=heading,
                ordinal=ordinal,
                text=text,
                vector=semantic_vector(contextual, dimensions),
            )
        )
    return chunks


def cosine_sparse(left: dict[int, float], right: dict[int, float]) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(key, 0.0) for key, value in left.items())


def wikilink_targets(values: Iterable[str]) -> list[str]:
    targets: list[str] = []
    for value in values:
        targets.extend(match.group(1) for match in WIKILINK_RE.finditer(value))
    return targets
