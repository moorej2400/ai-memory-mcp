"""Parse note links and graph snapshots the way the native graph needs them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ai_memory_mcp.config import Settings
from ai_memory_mcp.frontmatter import split_frontmatter
from ai_memory_mcp.generation import load_current_generation
from ai_memory_mcp.graph_builder import build_memory_graph
from ai_memory_mcp.index import build_index
from ai_memory_mcp.intake import inspect_vault
from ai_memory_mcp.memory_graph import GraphSnapshotError, MemoryGraph, parse_graph_snapshot
from ai_memory_mcp.text import parse_document
from ai_memory_mcp.wikilinks import (
    linkable_text,
    related_link_targets,
    related_values,
    wikilink_targets,
)


# -- wikilink extraction ------------------------------------------------------


def test_wikilinks_keep_targets_and_drop_display_labels() -> None:
    body = "See [[Access Policy]], ![[Diagram.png]], and [[Runbook#Steps|the steps]]."
    assert wikilink_targets([body]) == ["Access Policy", "Diagram.png", "Runbook#Steps"]


def test_table_escaped_pipe_does_not_keep_the_escape() -> None:
    row = "| [[Access Policy\\|policy]] | [[Folder/Note\\|note]] |"
    assert wikilink_targets([row]) == ["Access Policy", "Folder/Note"]


def test_fenced_code_does_not_create_links() -> None:
    body = "\n".join(
        (
            "Before [[Real Link]].",
            "```markdown",
            "Example [[Code Link]]",
            "```",
            "~~~~",
            "```",
            "[[Nested Fence Link]]",
            "~~~~",
            "After [[Second Link]].",
        )
    )
    assert wikilink_targets([body]) == ["Real Link", "Second Link"]


def test_a_shorter_closing_fence_does_not_end_the_block() -> None:
    body = "````\n[[Inside]]\n```\n[[Still Inside]]\n````\n[[Outside]]"
    assert wikilink_targets([body]) == ["Outside"]


def test_an_unclosed_fence_hides_the_rest_of_the_note() -> None:
    assert wikilink_targets(["[[Before]]\n```\n[[After]]"]) == ["Before"]


def test_indented_fence_markers_follow_commonmark_limits() -> None:
    # Four spaces make an indented code line, not a fence opener.
    assert wikilink_targets(["    ```\n[[Visible]]"]) == ["Visible"]


def test_inline_code_and_comments_do_not_create_links() -> None:
    body = "\n".join(
        (
            "Use `[[Inline Code]]` and ``a `[[Double Tick]]` b``.",
            "<!-- [[Html Comment]] -->",
            "%% [[Obsidian",
            "Comment]] %%",
            "Keep [[Visible Link]].",
        )
    )
    assert wikilink_targets([body]) == ["Visible Link"]


def test_a_stray_backtick_cannot_hide_a_later_paragraph() -> None:
    body = "A lone ` backtick.\n\nThen [[Later Link]] and a ` tick."
    assert wikilink_targets([body]) == ["Later Link"]


def test_escaped_brackets_and_multiline_text_are_not_links() -> None:
    assert wikilink_targets(["\\[[Literal]] and [[Split\nLink]]"]) == []


def test_linkable_text_keeps_plain_content() -> None:
    assert "[[Plain]]" in linkable_text("Text [[Plain]] text")


# -- related frontmatter --------------------------------------------------------


@pytest.mark.parametrize(
    ("yaml_text", "expected"),
    [
        ('related:\n  - "[[Quoted]]"\n', ["[[Quoted]]"]),
        ("related:\n  - [[Unquoted]]\n", ["[[Unquoted]]"]),
        ("related: [[Inline]]\n", ["[[Inline]]"]),
        ("related:\n  - [[Folder/Note|Label]]\n", ["[[Folder/Note|Label]]"]),
        ("related:\n  - [[Decisions, 2026]]\n", ["[[Decisions, 2026]]"]),
        ("related: Plain Note\n", ["Plain Note"]),
        ("related:\n  - Plain Note\n  - \n", ["Plain Note"]),
        ("related:\n", []),
    ],
)
def test_related_values_read_every_yaml_link_form(
    yaml_text: str,
    expected: list[str],
) -> None:
    assert related_values(yaml.safe_load(yaml_text)["related"]) == expected


def test_related_link_targets_split_entries_with_several_links() -> None:
    values = ["[[One]], [[Two|Second]]", "Plain Note", "[[One]]"]
    assert related_link_targets(values) == ["One", "Two", "Plain Note"]


# -- frontmatter fences --------------------------------------------------------------


def test_frontmatter_value_can_contain_three_dashes() -> None:
    header, body = split_frontmatter("---\ntitle: Before --- after\n---\n\n# Body\n")
    assert yaml.safe_load(header) == {"title": "Before --- after"}
    assert body == "# Body\n"


def test_frontmatter_accepts_crlf_and_yaml_document_end() -> None:
    header, body = split_frontmatter("---\r\ntitle: Windows\r\n...\r\nBody\r\n")
    assert yaml.safe_load(header) == {"title": "Windows"}
    assert body == "Body\r\n"


@pytest.mark.parametrize(
    "raw",
    ["----\ntitle: Rule\n---\n", "--- title\n---\n", "---\ntitle: Open\n", "Body"],
)
def test_text_without_a_complete_frontmatter_block_is_body(raw: str) -> None:
    assert split_frontmatter(raw) == (None, raw)


def test_parse_document_reads_unquoted_related_links(tmp_path: Path) -> None:
    note = tmp_path / "Note.md"
    note.write_text(
        "---\nmemory_id: mem-note\ntitle: A --- B\nrelated:\n  - [[Target]]\n---\n\n# A\n",
        encoding="utf-8",
    )
    document = parse_document(note, tmp_path)
    assert document.title == "A --- B"
    assert document.related == ["[[Target]]"]


# -- graph build from parsed notes -------------------------------------------------


def _note(root: Path, relative: str, frontmatter: str, body: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n\n{body}\n", encoding="utf-8")


def test_graph_build_uses_parsed_links_only(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _note(vault, "Target.md", "memory_id: mem-target\ntitle: Target\n", "# Target")
    _note(vault, "Other.md", "memory_id: mem-other\ntitle: Other\n", "# Other")
    _note(vault, "Example.md", "memory_id: mem-example\ntitle: Example\n", "# Example")
    _note(
        vault,
        "Source.md",
        "memory_id: mem-source\ntitle: Source\nrelated:\n  - [[Target]]\n",
        "# Source\n\nSee [[Other]].\n\n```\n[[Example]]\n```\n",
    )
    settings = Settings(
        memory_root=vault,
        state_dir=tmp_path / "state",
        graph_path=tmp_path / "unused.json",
        embedding_provider="hashed",
    )
    build_index(settings, force=True)
    summary = build_memory_graph(settings, tmp_path / "out")
    payload = json.loads((tmp_path / "out" / "graph.json").read_text(encoding="utf-8"))
    edges = {
        (link["source"], link["target"], link["relation"])
        for link in payload["links"]
        if link["relation"] != "belongs-to"
    }
    assert edges == {
        ("core::mem-source", "core::mem-target", "declared-related"),
        ("core::mem-other", "core::mem-source", "body-link"),
    }
    assert summary["unresolved_related"] == 0
    assert summary["unresolved_body_links"] == 0
    assert payload["graph"]["format"] == "ai-memory-graph@1"
    assert payload["graph"]["provider"] == "ai-memory"


def test_intake_ignores_links_in_code_and_checks_unquoted_related(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _note(
        vault,
        "Source.md",
        "memory_id: mem-source\ntitle: Source\nrelated:\n  - [[Missing Related]]\n",
        "# Source\n\n`[[Missing Inline]]`\n",
    )
    report = inspect_vault(vault)
    messages = [issue["message"] for issue in report["issues"]]
    assert any("Missing Related" in message for message in messages)
    assert not any("Missing Inline" in message for message in messages)


# -- graph snapshot parsing ------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "one JSON object"),
        ({"nodes": {}}, "node list"),
        ({"nodes": [], "links": {}}, "edge list"),
        ({"nodes": [{"label": "x"}]}, "no identifier"),
        ({"nodes": [{"id": ""}]}, "no identifier"),
        ({"nodes": [{"id": "a"}, {"id": "a"}]}, "not unique"),
        ({"nodes": ["a"]}, "not a JSON object"),
        ({"nodes": [{"id": "a"}], "links": [{"source": "a"}]}, "no target"),
        ({"nodes": [{"id": "a"}], "links": [{"source": "a", "target": "b"}]}, "missing target"),
        ({"graph": [], "nodes": []}, "metadata"),
    ],
)
def test_malformed_snapshots_are_rejected(payload: object, message: str) -> None:
    with pytest.raises(GraphSnapshotError, match=message):
        parse_graph_snapshot(payload)


def test_legacy_edges_key_and_integer_ids_are_accepted(tmp_path: Path) -> None:
    path = tmp_path / "graph.json"
    path.write_text(
        json.dumps(
            {
                "nodes": [
                    {"id": 1, "label": "One", "source_file": "One.md"},
                    {"id": 2, "label": "Two", "source_file": "Two.md"},
                ],
                "edges": [{"source": 1, "target": 2, "relation": "mentions"}],
            }
        ),
        encoding="utf-8",
    )
    graph = MemoryGraph(path)
    assert graph.health()["edges"] == 1
    assert graph.path("core/One.md", "core/Two.md")[0]["relation"] == "mentions"


def test_status_reports_a_structurally_corrupt_graph(tmp_path: Path) -> None:
    from ai_memory_mcp.artifacts.schema import migrate_artifact_db
    from ai_memory_mcp.service import MemoryService

    vault = tmp_path / "vault"
    _note(vault, "Record.md", "memory_id: mem-record\ntitle: Record\n", "# Record")
    settings = Settings(
        memory_root=vault,
        state_dir=tmp_path / "state",
        graph_path=tmp_path / "legacy.json",
        embedding_provider="hashed",
        artifact_db=tmp_path / "raw" / "artifacts.sqlite3",
        artifact_objects_dir=tmp_path / "raw" / "objects",
        artifact_backup_dir=tmp_path / "backups",
        log_dir=tmp_path / "logs",
    )
    migrate_artifact_db(settings)
    service = MemoryService(settings)
    assert service.sync().ok is True
    generation = load_current_generation(settings)
    assert generation is not None
    graph_path = settings.state_dir / generation["graph_snapshot"]
    payload = json.loads(graph_path.read_text(encoding="utf-8"))
    del payload["nodes"][0]["id"]
    graph_path.write_text(json.dumps(payload), encoding="utf-8")

    status = service.status()
    recall = service.recall("record")

    assert status.ok is False
    assert status.graph.available is False
    assert status.graph.error and "no identifier" in status.graph.error
    assert any("graph component is unavailable" in item for item in recall.warnings)
