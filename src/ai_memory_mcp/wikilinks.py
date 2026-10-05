from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Iterable, Mapping


# A target stops at the first pipe. Obsidian escapes that pipe as `\|` inside
# tables, so the lazy target must not keep the escape backslash. A link never
# spans lines, and a backslash before `[[` makes the brackets literal text.
WIKILINK_RE = re.compile(r"(?<!\\)\[\[([^\[\]\n|]+?)(?:\\?\|[^\[\]\n]*)?\]\]")
FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.DOTALL)
# Obsidian `%%` comments and HTML comments are hidden from the rendered note.
COMMENT_RE = re.compile(r"<!--.*?-->|%%.*?%%", re.DOTALL)
BLANK_LINE_RE = re.compile(r"\n[ \t]*\n")
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


def _without_fenced_code(text: str) -> list[str]:
    """Return the text segments outside fenced code blocks.

    This follows the CommonMark fence rules that Obsidian uses: a closing fence
    uses the same character and is at least as long as the opening fence. An
    unclosed fence continues to the end of the note.
    """
    segments: list[str] = []
    current: list[str] = []
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if fence is None:
            opening = FENCE_OPEN_RE.match(stripped)
            # A backtick fence cannot have a backtick in its info string.
            if opening and not (
                opening.group(1)[0] == "`" and "`" in opening.group(2)
            ):
                fence = opening.group(1)
                segments.append("".join(current))
                current = []
                continue
            current.append(line)
            continue
        candidate = stripped.lstrip(" ")
        if (
            len(stripped) - len(candidate) <= 3
            and candidate.rstrip()
            and set(candidate.rstrip()) == {fence[0]}
            and len(candidate.rstrip()) >= len(fence)
        ):
            fence = None
    if fence is None:
        segments.append("".join(current))
    return segments


def linkable_text(value: str) -> str:
    """Remove the Markdown regions in which link syntax is literal text.

    Fenced code, inline code, HTML comments, and Obsidian comments do not create
    links. An inline code span cannot cross a blank line, so each paragraph is
    scanned separately; a stray backtick cannot hide links in later paragraphs.
    """
    kept: list[str] = []
    for segment in _without_fenced_code(value):
        paragraphs = BLANK_LINE_RE.split(segment)
        segment = "\n\n".join(
            INLINE_CODE_RE.sub(" ", paragraph) for paragraph in paragraphs
        )
        kept.append(COMMENT_RE.sub(" ", segment))
    return "\n".join(kept)


def wikilink_targets(values: Iterable[str]) -> list[str]:
    targets: list[str] = []
    for value in values:
        targets.extend(
            match.group(1).strip()
            for match in WIKILINK_RE.finditer(linkable_text(value))
        )
    return [target for target in targets if target]


def _is_scalar(value: object) -> bool:
    return value is not None and not isinstance(value, (list, dict))


def related_values(value: object) -> list[str]:
    """Return the `related` frontmatter entries as link strings.

    YAML reads an unquoted `[[Note]]` as a sequence nested in a sequence. That
    form must become the link `[[Note]]` again, not the text `['Note']`.
    """
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    results: list[str] = []
    for item in items:
        if isinstance(item, list):
            # `related: [[A]]` gives the item ["A"]. A block item `- [[A]]` adds
            # one more level and gives [["A"]].
            groups = item if item and all(isinstance(part, list) for part in item) else [item]
            for group in groups:
                if group and all(_is_scalar(part) for part in group):
                    # `[[Decisions, 2026]]` is one title that contains a comma.
                    results.append(f"[[{', '.join(str(part) for part in group)}]]")
            continue
        if not _is_scalar(item):
            continue
        text = str(item).strip()
        if text:
            results.append(text)
    return results


def related_link_targets(values: Iterable[str]) -> list[str]:
    """Return one link target for each note that a `related` entry names.

    An entry can hold several wikilinks. An entry without wikilink syntax is a
    plain note name, path, or alias.
    """
    targets: list[str] = []
    for value in values:
        links = wikilink_targets([value])
        if links:
            targets.extend(links)
        elif value.strip():
            targets.append(value.strip())
    return list(dict.fromkeys(targets))


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
