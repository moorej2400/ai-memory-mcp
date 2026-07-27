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
        packet = service.search(case["query"], limit=5, **case.get("scope", {}))
        returned = {result["memory_id"] for result in packet["results"]}
        if case.get("no_answer"):
            assert packet["answer_status"] == "no_answer", case["id"]
        else:
            assert returned & set(case["expected_any"]), case["id"]
        assert not (returned & set(case.get("forbidden", []))), case["id"]
        if expected_path := case.get("expected_path"):
            assert any(
                result["path"] == expected_path for result in packet["results"]
            ), case["id"]


def test_evidence_packet_exposes_internal_plan(
    benchmark_settings: Settings,
) -> None:
    packet = MemoryService(benchmark_settings).search("ALPHA-142", limit=1)
    assert packet["answer_status"] == "answered"
    assert packet["plan"]["retrievers"] == ["lexical", "semantic", "graphify"]
    assert packet["results"][0]["path"].endswith("Retry Decision.md")
    assert packet["results"][0]["ranks"]
    assert packet["diagnostics"]["graphify"]["available"] is True


def test_scope_is_applied_before_ranking(benchmark_settings: Settings) -> None:
    service = MemoryService(benchmark_settings)
    beta = service.search(
        "generation counter", repository="beta", limit=10
    )
    assert {item["memory_id"] for item in beta["results"]} == {"mem-beta-cache"}


def test_graph_neighbors_and_path(benchmark_settings: Settings) -> None:
    service = MemoryService(benchmark_settings)
    neighbors = service.neighbors("mem-demo-777", depth=2)
    assert neighbors["found"]
    assert any(
        item["path"] == "Workflows/Graph Refresh.md"
        for item in neighbors["graph_neighbors"]
    )
    path = service.path("mem-demo-777", "mem-memory-authority")
    assert path["found"]
    assert len(path["path"]) == 2


def test_parallel_search_is_consistent_and_fast(
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
        result = service.search(query, limit=3)
        return result["answer_status"], (time.perf_counter() - started) * 1000

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(run, queries))
    assert all(status == "answered" for status, _ in outcomes)
    assert sorted(duration for _, duration in outcomes)[37] < 250
