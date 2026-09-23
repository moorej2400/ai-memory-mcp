from __future__ import annotations

from pathlib import Path

import pytest

from ai_memory_mcp.intake import inspect_markdown, inspect_vault, validate_new_markdown


def _record(title: str = "Pancakes") -> str:
    return f"""---
schema_version: 2
memory_id: mem-pancakes
title: {title}
type: memory
domain: personal
record_type: recipe
collection: Recipes
status: active
created: 2026-09-18
updated: 2026-09-18
related: []
aliases: []
provenance:
  - source: user
    verified: 2026-09-18
---

# {title}

Pancakes use flour, milk, and eggs.
"""


def test_current_record_passes_strict_intake() -> None:
    inspected = inspect_markdown(
        _record(),
        path="Collections/Recipes/Records/Pancakes.md",
        require_current=True,
    )
    assert inspected.issues == ()


def test_legacy_record_stays_readable_but_needs_migration() -> None:
    inspected = inspect_markdown(
        "---\nmemory_id: mem-old\ntitle: Old\ntype: memory\nstatus: active\n"
        "created: 2026-01-01\nupdated: 2026-01-01\n---\n\n# Old\n\nUseful text.\n",
        path="Notes/Old.md",
    )
    assert [(issue.rule, issue.severity) for issue in inspected.issues] == [
        ("schema-version", "warning")
    ]


def test_strict_intake_rejects_missing_generic_fields() -> None:
    with pytest.raises(ValueError, match="domain.*record_type.*provenance"):
        validate_new_markdown(
            "---\nschema_version: 2\nmemory_id: mem-x\ntitle: X\ntype: memory\n"
            "status: active\ncreated: 2026-01-01\nupdated: 2026-01-01\n---\n\n# X\n\nText.\n",
            path="Notes/X.md",
        )


def test_vault_reports_duplicate_identity_and_broken_link(tmp_path: Path) -> None:
    first = tmp_path / "Notes" / "First.md"
    second = tmp_path / "Notes" / "Second.md"
    first.parent.mkdir(parents=True)
    first.write_text(
        _record("First").replace(
            "Pancakes use flour, milk, and eggs.",
            "See [[Missing]].",
        ),
        encoding="utf-8",
    )
    second.write_text(_record("Second"), encoding="utf-8")

    report = inspect_vault(tmp_path)

    assert report["issue_counts"]["memory-id-unique"] == 2
    assert report["issue_counts"]["link-unresolved"] == 1


def test_vault_ignores_links_to_obsidian_bases(tmp_path: Path) -> None:
    raw = _record("Home").replace(
        "Pancakes use flour, milk, and eggs.",
        "This page shows active memory views.\n\n![[Views/Action Dashboard.base]]",
    )
    (tmp_path / "Home.md").write_text(raw, encoding="utf-8")

    report = inspect_vault(tmp_path, require_current=True)
    assert "link-unresolved" not in report["issue_counts"]
