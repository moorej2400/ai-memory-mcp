from __future__ import annotations

import json
from pathlib import Path

from ai_memory_mcp.config import Settings
from ai_memory_mcp.index import build_index
from ai_memory_mcp.provider_graph import build_provider_graph


def _note(
    root: Path,
    relative: str,
    memory_id: str,
    title: str,
    body: str,
    related: list[str] | None = None,
) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"memory_id: {memory_id}", f"title: {title}", "status: active"]
    if related:
        lines.append("related:")
        lines.extend(f"  - {item}" for item in related)
    lines.extend(("---", "", f"# {title}", "", body, ""))
    path.write_text("\n".join(lines), encoding="utf-8")


def _build(tmp_path: Path) -> dict:
    settings = Settings(
        memory_root=tmp_path / "vault",
        state_dir=tmp_path / "state",
        graph_path=tmp_path / "out" / "graph.json",
        graphify_mcp_url="",
        embedding_provider="hashed",
    )
    build_index(settings, force=True)
    return build_provider_graph(settings, tmp_path / "out")


def _links(tmp_path: Path) -> list[dict]:
    graph = json.loads((tmp_path / "out" / "graph.json").read_text(encoding="utf-8"))
    return graph["links"]


def test_body_wikilink_becomes_a_graph_edge(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _note(
        vault,
        "Tools/Runbook.md",
        "mem-runbook",
        "Deploy Runbook",
        "Follow [[Access Policy]] before you deploy.",
    )
    _note(vault, "Tools/Access Policy.md", "mem-access", "Access Policy", "Request the role.")

    summary = _build(tmp_path)
    relations = [link["relation"] for link in _links(tmp_path)]
    assert "body-link" in relations, "body wikilinks must produce graph edges"
    assert summary["body_links"] == 1
    assert summary["unresolved_body_links"] == 0


def test_body_link_does_not_duplicate_a_declared_edge(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _note(
        vault,
        "Tools/Runbook.md",
        "mem-runbook",
        "Deploy Runbook",
        "Follow [[Access Policy]] before you deploy.",
        related=["[[Access Policy]]"],
    )
    _note(vault, "Tools/Access Policy.md", "mem-access", "Access Policy", "Request the role.")

    _build(tmp_path)
    pairs = [
        tuple(sorted((link["source"], link["target"])))
        for link in _links(tmp_path)
    ]
    assert len(pairs) == len(set(pairs)), "one pair must not get two edges"


def test_unresolved_and_ambiguous_links_are_reported(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _note(
        vault,
        "Tools/Runbook.md",
        "mem-runbook",
        "Deploy Runbook",
        "See [[Nowhere Note]] and [[Duplicate Title]].",
    )
    # Two notes share a title, so the link target is ambiguous.
    _note(vault, "A/Duplicate Title.md", "mem-dup-a", "Duplicate Title", "One.")
    _note(vault, "B/Duplicate Title.md", "mem-dup-b", "Duplicate Title", "Two.")

    summary = _build(tmp_path)
    assert summary["unresolved_body_links"] == 1
    assert summary["ambiguous_body_links"] == 1
    assert summary["body_links"] == 0


def test_self_links_are_ignored(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _note(
        vault,
        "Tools/Runbook.md",
        "mem-runbook",
        "Deploy Runbook",
        "This is [[Deploy Runbook]] itself.",
    )
    summary = _build(tmp_path)
    assert summary["body_links"] == 0
    assert summary["unresolved_body_links"] == 0
