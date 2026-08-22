from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ai_memory_mcp.benchmark import (
    _benchmark_settings,
    run_benchmark,
    verify_contract,
)
from ai_memory_mcp.config import Settings
from ai_memory_mcp.index import build_index
from ai_memory_mcp.models import ScopeFilter
from ai_memory_mcp.retrieval import RetrievalEngine
from ai_memory_mcp.service import MemoryService


def test_frozen_contract_is_unchanged(project_root: Path) -> None:
    lock = verify_contract(project_root / "benchmarks")
    assert lock["sha256"] == (
        "e6a13efda90f9a654b702c9c5161f2bbb97aff5e2de145bd060ab537b0753e0c"
    )


def test_frozen_benchmark_uses_isolated_artifact_paths(tmp_path: Path) -> None:
    state_dir = tmp_path / "benchmark-state"
    settings = _benchmark_settings(tmp_path / "benchmark", state_dir)

    assert settings.artifact_db == state_dir / "artifacts.sqlite3"
    assert settings.artifact_objects_dir == state_dir / "artifact-objects"
    assert settings.artifact_backup_dir == state_dir / "artifact-backups"


def test_frozen_benchmark_exercises_the_recall_pipeline(
    tmp_path: Path,
) -> None:
    report = run_benchmark("pytest", output_root=tmp_path)
    metrics = report["metrics"]

    assert metrics["pass_rate"] == 1.0
    assert metrics["recall_at_5"] == 1.0
    assert metrics["mrr"] == 1.0
    assert metrics["no_answer_accuracy"] == 1.0
    assert metrics["scope_leakage_rate"] == 0.0
    assert metrics["citation_failure_rate"] == 0.0
    assert metrics["document_diversity_at_5"] == 1.0
    assert metrics["stale_layer_behavior"] is True
    assert "rerank" in metrics["per_layer_latency_ms"]
    assert metrics["generation_id"]


def test_all_frozen_cases_pass(
    project_root: Path, benchmark_settings: Settings
) -> None:
    service = MemoryService(benchmark_settings)
    cases = json.loads(
        (project_root / "benchmarks" / "cases.json").read_text(encoding="utf-8")
    )["cases"]
    for case in cases:
        packet = service.recall(case["query"], limit=5, **case.get("scope", {}))
        returned = {result.memory_id for result in packet.evidence}
        if case.get("no_answer"):
            assert packet.status == "no_answer", case["id"]
        else:
            assert returned & set(case["expected_any"]), case["id"]
        assert not (returned & set(case.get("forbidden", []))), case["id"]
        if expected_path := case.get("expected_path"):
            assert any(
                citation.path.endswith(expected_path)
                for citation in packet.citations
            ), case["id"]


def test_recall_packet_hides_internal_provider_diagnostics(
    benchmark_settings: Settings,
) -> None:
    packet = MemoryService(benchmark_settings).recall("ALPHA-142", limit=1)
    payload = packet.model_dump()
    assert packet.status == "answered"
    assert packet.citations[0].path.endswith("Retry Decision.md")
    assert set(payload) == {
        "status",
        "intent",
        "query",
        "evidence",
        "citations",
        "relationships",
        "warnings",
    }
    assert "ranks" not in payload["evidence"][0]
    assert "signals" not in payload["evidence"][0]
    assert "diagnostics" not in payload


def test_scope_is_applied_before_ranking(benchmark_settings: Settings) -> None:
    service = MemoryService(benchmark_settings)
    beta = service.recall(
        "generation counter", repository="beta", limit=10
    )
    assert {item.memory_id for item in beta.evidence} == {"mem-beta-cache"}
    partial = service.recall(
        "generation counter", repository="alp", limit=10
    )
    assert partial.status == "no_answer"
    assert partial.evidence == []


def test_recall_routes_exact_neighbors_and_relationship_path(
    benchmark_settings: Settings,
) -> None:
    service = MemoryService(benchmark_settings)
    exact = service.recall("mem-demo-777")
    assert exact.intent == "exact"
    assert any(
        item.target_path == "core/Workflows/Graph Refresh.md"
        for item in exact.relationships
    )
    relationship = service.recall(
        "How is DEMO-777 Memory Refresh Safety related to "
        "Canonical Memory Authority?"
    )
    assert relationship.intent == "relationship"
    assert relationship.status == "answered"
    assert relationship.relationships
    assert relationship.relationships[0].source_path.endswith(
        "Repos/demo/Tickets/DEMO-777/_ticket.md"
    )
    assert relationship.relationships[-1].target_path.endswith(
        "Decisions/Memory Authority.md"
    )


def test_parallel_recall_is_consistent_and_fast(
    benchmark_settings: Settings,
) -> None:
    service = MemoryService(benchmark_settings)
    queries = [
        "ALPHA-142",
        "background without terminal",
        "canonical memory source of truth",
        "current credential rotation",
    ] * 10

    def run(query: str) -> tuple[str, float]:
        started = time.perf_counter()
        result = service.recall(query, limit=3)
        return result.status, (time.perf_counter() - started) * 1000

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(run, queries))
    assert all(status == "answered" for status, _ in outcomes)
    assert sorted(duration for _, duration in outcomes)[37] < 250


def test_no_answer_returns_best_effort_leads(
    benchmark_settings: Settings,
) -> None:
    service = MemoryService(benchmark_settings)
    packet = service.recall("What is the launch date of Project Zephyr?")
    assert packet.status == "no_answer"
    assert packet.evidence, "low-confidence recall must still return leads"
    assert packet.citations
    assert any("best-effort" in warning for warning in packet.warnings)


def test_candidate_limits_count_documents_not_repeated_chunks(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    repeated_sections = "\n\n".join(
        f"## Section {index}\nshared deduplication phrase"
        for index in range(12)
    )
    (vault / "Long.md").write_text(
        "\n".join(
            (
                "---",
                "memory_id: mem-long",
                "title: Long record",
                "status: active",
                "---",
                "",
                "# Long record",
                repeated_sections,
            )
        ),
        encoding="utf-8",
    )
    (vault / "Short.md").write_text(
        "\n".join(
            (
                "---",
                "memory_id: mem-short",
                "title: Short record",
                "status: active",
                "---",
                "",
                "# Short record",
                "",
                "shared deduplication phrase",
            )
        ),
        encoding="utf-8",
    )
    settings = Settings(
        memory_root=vault,
        state_dir=tmp_path / "state",
        graph_path=tmp_path / "graph.json",
        graphify_mcp_url="",
        embedding_provider="hashed",
    )
    build_index(settings, force=True)
    engine = RetrievalEngine(settings)
    scope = ScopeFilter(status="active")

    lexical = engine._lexical("shared deduplication phrase", scope, 2)
    semantic = engine._semantic("shared deduplication phrase", scope, 2)

    assert {hit.memory_id for hit in lexical} == {"mem-long", "mem-short"}
    assert {hit.memory_id for hit in semantic} == {"mem-long", "mem-short"}
