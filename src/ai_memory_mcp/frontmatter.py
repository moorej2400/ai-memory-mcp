from __future__ import annotations

OPENING_FENCE = "---"
CLOSING_FENCES = frozenset({"---", "..."})


def split_frontmatter(raw: str) -> tuple[str | None, str]:
    """Split a note into its YAML frontmatter text and its body.

    Return `(None, raw)` when the note has no complete frontmatter block. The
    fences must be whole lines. A `---` inside a YAML value or a longer `----`
    rule is content, not a fence. The body keeps the old contract: leading
    whitespace after the closing fence is removed.
    """
    lines = raw.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != OPENING_FENCE:
        return None, raw
    for index in range(1, len(lines)):
        if lines[index].rstrip() in CLOSING_FENCES:
            return "".join(lines[1:index]), "".join(lines[index + 1 :]).lstrip()
    return None, raw
