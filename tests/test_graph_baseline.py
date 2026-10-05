"""Pin graph retrieval behavior with golden values from the benchmark vault.

These values were recorded before the Graphify removal. The native graph must
keep the same label matches, relationships, neighbors, paths, ranking, and
scope isolation for the same input.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ai_memory_mcp.artifacts.schema import migrate_artifact_db
from ai_memory_mcp.config import Settings
from ai_memory_mcp.graphify import GraphifyAdapter as MemoryGraph
from ai_memory_mcp.service import MemoryService

DEMO = "core/Repos/demo/Tickets/DEMO-777/_ticket.md"
REFRESH = "core/Workflows/Graph Refresh.md"
AUTHORITY = "core/Decisions/Memory Authority.md"
RETRY = "core/Repos/alpha/Tickets/ALPHA-142/Retry Decision.md"
RECOVERY = "core/Decisions/Index Recovery.md"


@pytest.fixture(scope="module")
def synced(tmp_path_factory, project_root: Path) -> tuple[MemoryService, Path]:
    root = tmp_path_factory.mktemp("graph-baseline")
    shutil.copytree(project_root / "benchmarks" / "fixtures" / "vault", root / "vault")
    settings = Settings(
        memory_root=root / "vault",
        state_dir=root / "state",
        graph_path=root / "legacy" / "graph.json",
        graphify_mcp_url="",
        embedding_provider="hashed",
        audit_logging_enabled=False,
        log_dir=root / "logs",
        artifact_db=root / "artifacts.sqlite3",
        artifact_objects_dir=root / "objects",
        artifact_backup_dir=root / "backups",
    )
    migrate_artifact_db(settings)
    service = MemoryService(settings)
    result = service.sync()
    assert result.ok, result.errors
    return service, settings.state_dir / str(result.graph_snapshot)


@pytest.fixture(scope="module")
def graph(synced) -> MemoryGraph:
    return MemoryGraph(synced[1])


def test_native_graph_relationships(synced) -> None:
    payload = json.loads(synced[1].read_text(encoding="utf-8"))
    declared = sorted(
        (link["source"], link["target"])
        for link in payload["links"]
        if link["relation"] == "declared-related"
    )
    assert declared == [
        ("core::mem-alpha-retry", "core::mem-graph-refresh"),
        ("core::mem-demo-777", "core::mem-graph-refresh"),
        ("core::mem-demo-777", "core::mem-memory-authority"),
        ("core::mem-graph-refresh", "core::mem-memory-authority"),
    ]
    relations = sorted({link["relation"] for link in payload["links"]})
    assert relations == ["belongs-to", "declared-related"]
    assert len(payload["nodes"]) == 26
    assert len(payload["links"]) == 17


def test_label_match_ranking(graph: MemoryGraph) -> None:
    assert graph.rank("guarded memory refresh", [], limit=10) == [
        REFRESH,
        DEMO,
        AUTHORITY,
        RETRY,
    ]


def test_seed_ranking_without_label_match(graph: MemoryGraph) -> None:
    assert graph.rank("rotation", ["core/Policies/Active Rotation.md"], limit=10) == [
        "core/Policies/Active Rotation.md",
        "core/Policies/Old Rotation.md",
    ]


def test_neighbors(graph: MemoryGraph) -> None:
    neighbors = graph.neighbors(DEMO, depth=2)
    assert [(item["path"], item["distance"], item["relation"]) for item in neighbors] == [
        ("", 1, "belongs-to"),
        (REFRESH, 1, "declared-related"),
        (AUTHORITY, 1, "declared-related"),
        (RETRY, 2, "declared-related"),
        ("", 2, "belongs-to"),
        ("", 2, "belongs-to"),
    ]
    assert neighbors[1]["label"] == "Guarded Graphify memory refresh"
    assert neighbors[1]["confidence"] == "DECLARED"


def test_path(graph: MemoryGraph) -> None:
    assert graph.path(REFRESH, AUTHORITY) == [
        {
            "source": "Guarded Graphify memory refresh",
            "source_path": REFRESH,
            "relation": "declared-related",
            "confidence": "DECLARED",
            "target": "Canonical memory authority",
            "target_path": AUTHORITY,
        }
    ]


def test_scope_isolation(graph: MemoryGraph) -> None:
    allowed = {DEMO, AUTHORITY}
    assert graph.rank("guarded memory refresh", [], limit=10, allowed_paths=allowed) == [
        DEMO,
        AUTHORITY,
    ]
    scoped = graph.neighbors(DEMO, depth=2, allowed_paths=allowed)
    assert {item["path"] for item in scoped} == {"", AUTHORITY}
    assert graph.path(REFRESH, AUTHORITY, allowed_paths={REFRESH}) == []


def test_legacy_snapshot_compatibility(project_root: Path) -> None:
    legacy = MemoryGraph(project_root / "benchmarks" / "fixtures" / "graph.json")
    health = legacy.health()
    assert (health["available"], health["nodes"], health["edges"]) == (True, 5, 4)
    assert legacy.rank("snapshot recovery", [], limit=10) == [
        RECOVERY,
        REFRESH,
        AUTHORITY,
        RETRY,
        DEMO,
    ]
    chain = legacy.path(RETRY, RECOVERY)
    assert [(step["relation"], step["confidence"]) for step in chain] == [
        ("uses_bounded_retry", "INFERRED"),
        ("preserves", "EXTRACTED"),
    ]
    assert legacy.path(RETRY, RECOVERY, allowed_paths={RETRY, RECOVERY}) == []


def test_graph_assisted_recall(synced) -> None:
    service = synced[0]
    response = service.recall(
        "What does DEMO-777 say about memory refresh safety?",
        limit=5,
    )
    assert response.status == "answered"
    assert [citation.path for citation in response.citations] == [
        DEMO,
        REFRESH,
        AUTHORITY,
        RECOVERY,
        RETRY,
    ]
    packet = service.engine.search("guarded refresh rollback", limit=5)
    top = packet.results[0]
    assert top.path == REFRESH
    assert top.ranks["graph"] == 1
    assert AUTHORITY in top.graph_neighbors
