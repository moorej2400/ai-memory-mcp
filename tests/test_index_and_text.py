from __future__ import annotations

import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ai_memory_mcp.config import Settings
from ai_memory_mcp.index import MemoryIndex, _eligible_markdown, build_index
from ai_memory_mcp.models import ScopeFilter
from ai_memory_mcp.text import parse_document, semantic_vector


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.md")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_parser_preserves_scope_and_identifiers(
    benchmark_settings: Settings,
) -> None:
    path = (
        benchmark_settings.memory_root
        / "Repos"
        / "alpha"
        / "Tickets"
        / "ALPHA-142"
        / "Retry Decision.md"
    )
    document = parse_document(path, benchmark_settings.memory_root)
    assert document.memory_id == "mem-alpha-retry"
    assert document.scope_id == "ALPHA-142"
    assert "NX-401" in document.identifiers
    assert document.repos == ["alpha"]


def test_index_refresh_is_incremental_and_read_only(
    benchmark_settings: Settings,
) -> None:
    before = _tree_digest(benchmark_settings.memory_root)
    result = build_index(benchmark_settings)
    after = _tree_digest(benchmark_settings.memory_root)
    assert result["added"] == 0
    assert result["changed"] == 0
    assert result["unchanged"] == 13
    assert result["parse_errors"] == []
    assert before == after


def test_changed_note_rebuilds_only_one_document(
    project_root: Path,
) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    source = project_root / "benchmarks" / "fixtures" / "vault"
    run_root = project_root / "benchmarks" / "runs" / f"delta-{stamp}"
    vault = run_root / "vault"
    shutil.copytree(source, vault)
    settings = Settings(
        memory_root=vault,
        state_dir=run_root / "state",
        graph_path=project_root / "benchmarks" / "fixtures" / "graph.json",
        graphify_mcp_url="",
    )
    first = build_index(settings, force=True)
    target = vault / "Projects" / "Orion.md"
    target.write_text(
        target.read_text(encoding="utf-8") + "\nA canary marker is optional.\n",
        encoding="utf-8",
    )
    second = build_index(settings)
    assert first["documents"] == 13
    assert second["changed"] == 1
    assert second["unchanged"] == 12
    assert second["added"] == 0


def test_moved_note_keeps_identity_without_a_parse_error(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    source = vault / "People/Example.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "---\nmemory_id: mem-example\ntitle: Example\ntype: memory\n"
        "status: active\ncreated: 2026-01-01\nupdated: 2026-01-01\n"
        "---\n# Example\n\nA durable topic.\n",
        encoding="utf-8",
    )
    settings = Settings(
        memory_root=vault,
        state_dir=vault / ".ai-memory/indexes",
        graph_path=vault / ".ai-memory/provider-state/graph.json",
        graphify_mcp_url="",
        embedding_provider="hashed",
    )
    build_index(settings, force=True)
    destination = vault / "Collections/People/Records/Example.md"
    destination.parent.mkdir(parents=True)
    source.replace(destination)

    result = build_index(settings)

    assert result["parse_errors"] == []
    assert result["changed"] == 1
    assert result["added"] == 0
    assert result["removed"] == 0
    assert MemoryIndex(settings).document("mem-example")["path"].endswith(
        "Collections/People/Records/Example.md"
    )


def test_index_excludes_internal_data_markdown(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    internal = vault / ".ai-memory" / "migration"
    internal.mkdir(parents=True)
    (internal / "legacy.md").write_text(
        "# This is not a memory record.\n",
        encoding="utf-8",
    )
    settings = Settings(
        memory_root=vault,
        state_dir=vault / ".ai-memory" / "indexes",
        graph_path=vault / ".ai-memory" / "provider-state" / "graph.json",
        graphify_mcp_url="",
        embedding_provider="hashed",
    )

    result = build_index(settings, force=True)

    assert result["documents"] == 0
    assert result["parse_errors"] == []


def test_markdown_discovery_prunes_excluded_directories(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    allowed = vault / "Projects" / "Allowed.md"
    excluded = [
        vault / ".hidden" / "Hidden.md",
        vault / "Restricted" / "Restricted.md",
        vault / ".trash" / "Trash.md",
        vault / "Projects" / ".Hidden.md",
    ]
    allowed.parent.mkdir(parents=True)
    allowed.write_text("# Allowed\n", encoding="utf-8")
    for path in excluded:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Excluded\n", encoding="utf-8")

    assert _eligible_markdown(vault) == [allowed]


def test_semantic_vector_is_deterministic() -> None:
    first = semantic_vector("hidden background process", 1024)
    second = semantic_vector("hidden background process", 1024)
    assert first == second
    assert abs(sum(value * value for value in first.values()) - 1.0) < 1e-9


def test_index_snapshot_integrity(benchmark_settings: Settings) -> None:
    index = MemoryIndex(benchmark_settings)
    metadata = index.metadata()
    assert metadata["documents"] == "13"
    assert int(metadata["chunks"]) >= 13


def test_generic_collection_and_scope_filters(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    records = vault / "Collections/Links/Records"
    records.mkdir(parents=True)
    template = """---
schema_version: 2
memory_id: {memory_id}
title: {title}
type: memory
record_type: link
collection: Links
domain: personal
scope_kind: topic
scope_id: research
status: active
created: 2026-09-18
updated: 2026-09-18
provenance:
  - manual:test
---
# {title}

An independently maintained link record.
"""
    (records / "First.md").write_text(
        template.format(memory_id="mem_first", title="First"), encoding="utf-8"
    )
    notes = vault / "Notes"
    notes.mkdir()
    (notes / "Other.md").write_text(
        template.format(memory_id="mem_other", title="Other")
        .replace("collection: Links", "collection: Notes")
        .replace("record_type: link", "record_type: note"),
        encoding="utf-8",
    )
    settings = Settings(
        memory_root=vault,
        state_dir=vault / ".ai-memory/indexes",
        graph_path=vault / ".ai-memory/provider-state/graph.json",
        graphify_mcp_url="",
        embedding_provider="hashed",
    )
    build_index(settings, force=True)
    index = MemoryIndex(settings)

    identities = index.scoped_identities(
        ScopeFilter(
            root_scope="personal",
            collection="Links",
            record_type="link",
            scope_kind="topic",
            scope_id="research",
        )
    )
    assert "mem_first" in identities
    assert "mem_other" not in identities
