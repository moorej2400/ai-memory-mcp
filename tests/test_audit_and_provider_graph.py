from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from ai_memory_mcp.audit import file_lock
from ai_memory_mcp.config import Settings
from ai_memory_mcp.index import build_index
from ai_memory_mcp.provider_graph import build_provider_graph
from ai_memory_mcp.service import MemoryService


def test_retrieval_audit_records_query_result_and_performance(
    benchmark_settings: Settings,
    tmp_path: Path,
) -> None:
    settings = replace(
        benchmark_settings,
        log_dir=tmp_path / "logs",
        artifact_db=tmp_path / "missing-artifacts.sqlite3",
        artifact_objects_dir=tmp_path / "missing-objects",
        artifact_backup_dir=tmp_path / "missing-backups",
    )
    query = "What is the transient authentication retry policy for ALPHA-142?"
    result = MemoryService(settings).recall(query, limit=1)

    records = [
        json.loads(line)
        for line in (settings.resolved_log_dir / "retrieval.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    record = records[-1]
    assert record["event"] == "retrieval_completed"
    assert query not in json.dumps(record)
    assert record["query_sha256"]
    assert record["query_characters"] == len(query)
    assert record["elapsed_ms"] >= 0
    assert record["response"]["status"] == result.status
    assert record["response"]["evidence_count"] == 1
    assert record["response"]["evidence"][0]["evidence_class"] == "distilled"
    assert record["diagnostics"]["route"] == "search"
    assert set(record["diagnostics"]["provider_latency_ms"]) == {
        "lexical",
        "semantic",
        "graphify",
        "fusion",
        "rerank",
        "context",
    }


def test_retrieval_audit_does_not_store_scope_values(
    benchmark_settings: Settings,
    tmp_path: Path,
) -> None:
    settings = replace(benchmark_settings, log_dir=tmp_path / "logs")
    query = "private retrieval question marker"
    repository = "private repository marker"
    ticket = "PRIVATE-999"

    MemoryService(settings).recall(
        query,
        repository=repository,
        ticket=ticket,
        limit=1,
    )
    record = json.loads(
        (settings.resolved_log_dir / "retrieval.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )
    serialized = json.dumps(record)

    assert query not in serialized
    assert repository not in serialized
    assert ticket not in serialized
    assert record["scope"]["repository"] is True
    assert record["scope"]["ticket"] is True


def test_index_waits_for_concurrent_publisher(
    benchmark_settings: Settings,
    tmp_path: Path,
) -> None:
    settings = replace(
        benchmark_settings,
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "logs",
        index_lock_timeout_seconds=3,
    )
    lock_path = settings.state_dir / "index.lock"
    with ThreadPoolExecutor(max_workers=1) as pool:
        with file_lock(lock_path, 1):
            started = time.perf_counter()
            future = pool.submit(build_index, settings, force=True)
            time.sleep(0.3)
        result = future.result(timeout=10)

    assert result["documents"] == 13
    assert (time.perf_counter() - started) >= 0.25
    records = [
        json.loads(line)
        for line in (settings.resolved_log_dir / "index.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert records[-1]["event"] == "index_completed"
    assert records[-1]["lock_wait_ms"] >= 250


def test_provider_graph_covers_the_current_index(
    benchmark_settings: Settings,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "graphify-out"
    summary = build_provider_graph(benchmark_settings, output_dir)
    graph = json.loads(
        (output_dir / "graph.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (output_dir / "manifest.json").read_text(encoding="utf-8")
    )

    document_nodes = [
        node for node in graph["nodes"] if node.get("memory_id")
    ]
    assert summary["documents"] == 13
    assert len(document_nodes) == 13
    assert len(manifest) == 13
    assert graph["graph"]["build_mode"] == "deterministic-memory-index"
    assert any(
        link["relation"] == "declared-related"
        for link in graph["links"]
    )
