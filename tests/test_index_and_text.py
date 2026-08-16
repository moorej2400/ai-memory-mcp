from __future__ import annotations

import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ai_memory_mcp.config import Settings
from ai_memory_mcp.index import MemoryIndex, build_index
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
