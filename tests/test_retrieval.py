from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ai_memory_mcp.benchmark import verify_contract
from ai_memory_mcp.config import Settings
from ai_memory_mcp.service import MemoryService


def test_frozen_contract_is_unchanged(project_root: Path) -> None:
    lock = verify_contract(project_root / "benchmarks")
    assert lock["sha256"] == (
        "e6a13efda90f9a654b702c9c5161f2bbb97aff5e2de145bd060ab537b0753e0c"
    )


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
    assert len(relationship.relationships) == 2


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
