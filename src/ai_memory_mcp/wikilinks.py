from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Iterable, Mapping


WIKILINK_RE = re.compile(r"\[\[([^]|]+)(?:\|[^]]+)?]]")
NON_MARKDOWN_SUFFIXES = {
    ".base",
    ".canvas",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
}


def wikilink_targets(values: Iterable[str]) -> list[str]:
    targets: list[str] = []
    for value in values:
        targets.extend(match.group(1) for match in WIKILINK_RE.finditer(value))
    return targets


def normalized_link_target(value: str) -> str:
    """Return the note portion of an Obsidian link.

    Obsidian resolves the target before the display label. Headings and block
    references identify content inside the target note, not a separate note.
    """
    normalized = value.strip()
    if normalized[:2] == "[[" and normalized[-2:] == "]]":
        normalized = normalized[2:-2]
    normalized = normalized.partition("|")[0].strip().replace("\\", "/")
    normalized = normalized.partition("#")[0].strip()
    normalized = normalized.partition("^")[0].strip()
    if PurePosixPath(normalized).suffix.casefold() in NON_MARKDOWN_SUFFIXES:
        return ""
    if normalized.casefold().endswith(".md"):
        normalized = normalized[:-3]
    return normalized.strip("/")


def identity_keys(
    *,
    memory_id: str,
    title: str,
    path: str,
    identifiers: Iterable[str] = (),
    aliases: Iterable[str] = (),
) -> set[str]:
    normalized_path = path.replace("\\", "/").strip("/")
    relative = normalized_path.split("/", 1)[-1]
    path_without_extension = (
        normalized_path[:-3]
        if normalized_path.casefold().endswith(".md")
        else normalized_path
    )
    relative_without_extension = (
        relative[:-3] if relative.casefold().endswith(".md") else relative
    )
    keys = {
        memory_id.casefold(),
        title.casefold(),
        normalized_path.casefold(),
        relative.casefold(),
        path_without_extension.casefold(),
        relative_without_extension.casefold(),
        PurePosixPath(relative_without_extension).name.casefold(),
    }
    keys.update(str(value).strip().casefold() for value in identifiers)
    keys.update(str(value).strip().casefold() for value in aliases)
    return {key for key in keys if key}


def resolve_link(
    value: str,
    identity_candidates: Mapping[str, set[str]],
) -> tuple[str | None, str]:
    target = normalized_link_target(value)
    if not target:
        return None, "ignored"
    exact_keys = [target.casefold()]
    candidates: set[str] = set()
    for key in exact_keys:
        matches = identity_candidates.get(key, set())
        if len(matches) == 1:
            return next(iter(matches)), "resolved"
        candidates.update(matches)
    if "/" not in target:
        basename = PurePosixPath(target).name.casefold()
        matches = identity_candidates.get(basename, set())
        if len(matches) == 1:
            return next(iter(matches)), "resolved"
        candidates.update(matches)
    if not candidates:
        return None, "unresolved"
    if len(candidates) > 1:
        return None, "ambiguous"
    return next(iter(candidates)), "resolved"
