from __future__ import annotations

import asyncio
import concurrent.futures
import os
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ai_memory_mcp.config import Settings
from ai_memory_mcp.recall_worker import (
    WorkerDeadlineExceeded,
    WorkerCancelled,
    WorkerQueueFull,
    _WarmWorker,
    _archive_worker_generation_leases,
    _close_pools,
    _pool_for,
    recall_in_worker,
)
from ai_memory_mcp.server import create_server
from ai_memory_mcp.service import MemoryService


def _process_exists(process_id: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {process_id}", "/FO", "CSV", "/NH"],
            check=False,
            capture_output=True,
            text=True,
        )
        return f'"{process_id}"' in result.stdout
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    return True


def test_public_tool_surface_is_small_and_stable(
    benchmark_settings: Settings,
) -> None:
    server = create_server(benchmark_settings)
    tools = {
        tool.name: tool for tool in server._tool_manager.list_tools()
    }
    assert set(tools) == {
        "memory_recall",
        "memory_artifact_read",
        "memory_upsert",
        "memory_sync",
        "memory_status",
    }
    assert tools["memory_recall"].annotations.readOnlyHint is True
    assert tools["memory_artifact_read"].annotations.readOnlyHint is True
    assert tools["memory_artifact_read"].annotations.idempotentHint is True
    assert tools["memory_upsert"].annotations.readOnlyHint is False
    assert tools["memory_upsert"].annotations.destructiveHint is False
    assert tools["memory_upsert"].annotations.idempotentHint is True
    assert tools["memory_status"].annotations.readOnlyHint is True
    assert tools["memory_sync"].annotations.readOnlyHint is False
    output_schema = tools["memory_recall"].output_schema
    assert len(output_schema["anyOf"]) == 2
    assert "result" not in output_schema.get("properties", {})
    assert tools["memory_recall"].parameters["properties"]["limit"]["maximum"] == 20
    assert tools["memory_recall"].parameters["properties"]["response_version"]["enum"] == [
        "1",
        "2",
    ]
    assert {
        "source_label",
        "source_instance",
        "artifact_kind",
        "date_from",
        "date_to",
    } <= set(tools["memory_recall"].parameters["properties"])
    assert (
        tools["memory_artifact_read"].parameters["properties"]["limit"]["maximum"]
        == 200
    )
    assert tools["memory_sync"].parameters["properties"] == {}
    assert "derived indexes" in tools["memory_sync"].description
    assert "artifact data changes" in tools["memory_sync"].description
    assert "artifact data changes" in server.instructions
    assert "memory_upsert" in server.instructions


def test_warm_worker_pool_reuses_process(
    benchmark_settings: Settings,
) -> None:
    pool = _pool_for(benchmark_settings)
    before = [worker.process.pid for worker in list(pool.available.queue)]
    try:
        for _ in range(2):
            response = recall_in_worker(
                benchmark_settings,
                {"query": "ALPHA-142", "limit": 1},
            )
            assert response.evidence
        after_workers = list(pool.available.queue)
        assert [worker.process.pid for worker in after_workers] == before
        assert sum(worker.request_count for worker in after_workers) >= 2
    finally:
        _close_pools()


def test_worker_pool_reports_cold_and_warm_timing(
    benchmark_settings: Settings,
) -> None:
    settings = replace(benchmark_settings, recall_worker_count=1)
    pool = _pool_for(settings)
    first: dict[str, object] = {}
    second: dict[str, object] = {}
    try:
        recall_in_worker(
            settings,
            {"query": "ALPHA-142", "limit": 1},
            timing=first,
        )
        recall_in_worker(
            settings,
            {"query": "ALPHA-142", "limit": 1},
            timing=second,
        )
    finally:
        _close_pools()

    assert first["outcome"] == second["outcome"] == "complete"
    assert first["cold"] is True
    assert second["cold"] is False
    assert float(first["queue_ms"]) >= 0
    assert float(first["worker_ms"]) > 0
    assert float(first["total_ms"]) >= float(first["worker_ms"])


def test_worker_pool_rejects_work_beyond_queue_capacity(
    benchmark_settings: Settings,
) -> None:
    settings = replace(
        benchmark_settings,
        recall_worker_count=1,
        recall_queue_capacity=0,
    )
    pool = _pool_for(settings)
    timing: dict[str, object] = {}
    assert pool.capacity.acquire(blocking=False)
    try:
        with pytest.raises(WorkerQueueFull):
            pool.request(b"{}", 1.0, timing=timing)
    finally:
        pool.capacity.release()
        _close_pools()
    assert timing["outcome"] == "queue_full"


def test_cancellation_replaces_the_assigned_worker(
    benchmark_settings: Settings,
) -> None:
    settings = replace(
        benchmark_settings,
        recall_worker_count=1,
        recall_queue_capacity=1,
    )
    pool = _pool_for(settings)
    original = pool.available.get_nowait()
    original.process.terminate()
    original.process.wait(timeout=2)
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    pool.available.put(_WarmWorker(process=sleeper))
    cancelled = threading.Event()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(pool.request, b"{}", 10.0, cancelled)
            time.sleep(0.05)
            cancelled.set()
            with pytest.raises(WorkerCancelled):
                future.result(timeout=2)
        assert not _process_exists(sleeper.pid)
        replacement = pool.available.get_nowait()
        assert replacement.process.poll() is None
        pool.available.put(replacement)
    finally:
        _close_pools()


def test_serialized_stdio_request_uses_real_server_process(
    benchmark_settings: Settings,
) -> None:
    environment = dict(os.environ)
    environment.update(
        {
            "AI_MEMORY_WORK_DIR": str(benchmark_settings.memory_root),
            "AI_MEMORY_MCP_STATE_DIR": str(benchmark_settings.state_dir),
            "AI_MEMORY_GRAPH_PATH": str(benchmark_settings.graph_path),
            "AI_MEMORY_MCP_EMBEDDING_PROVIDER": "hashed",
            "AI_MEMORY_ARTIFACT_DB": str(benchmark_settings.artifact_db),
            "AI_MEMORY_ARTIFACT_OBJECTS_DIR": str(
                benchmark_settings.artifact_objects_dir
            ),
            "AI_MEMORY_ARTIFACT_BACKUP_DIR": str(
                benchmark_settings.artifact_backup_dir
            ),
            "AI_MEMORY_AUDIT_LOGGING": "false",
        }
    )

    async def call() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "ai_memory_mcp.server", "--transport", "stdio"],
            env=environment,
        )
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool(
                    "memory_recall",
                    {"query": "ALPHA-142", "limit": 1, "response_version": "2"},
                )
        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["response_version"] == "2"

    asyncio.run(call())


def test_tool_call_runs_full_retrieval_internally(
    benchmark_settings: Settings,
) -> None:
    server = create_server(benchmark_settings)

    async def call() -> dict:
        return await server._tool_manager.call_tool(
            "memory_recall",
            {
                "query": "What is the transient authentication retry policy "
                "for ALPHA-142?",
                "limit": 1,
            },
        )

    result = asyncio.run(call())
    assert result.response_version == "2"
    assert result.execution in {"complete", "partial"}
    assert result.result_kind == "ranked"
    assert result.evidence[0].memory_id == "mem-alpha-retry"
    assert result.citations[0].path.endswith("Retry Decision.md")
    assert result.intent == "search"


def test_memory_recall_returns_a_bounded_result_when_retrieval_stalls(
    benchmark_settings: Settings,
) -> None:
    server = create_server(
        replace(benchmark_settings, recall_timeout_seconds=0.01)
    )

    async def call() -> tuple[object, float]:
        started = time.perf_counter()
        result = await server._tool_manager.call_tool(
            "memory_recall",
            {"query": "bounded recall"},
        )
        elapsed = time.perf_counter() - started
        return result, elapsed

    result, elapsed = asyncio.run(call())

    assert elapsed < 2.0
    assert result.execution == "failed"
    assert result.result_kind == "empty"
    assert result.reason_codes == ["deadline_exceeded"]
    assert any("time limit" in warning for warning in result.warnings)


def test_legacy_recall_keeps_the_version_one_contract(
    benchmark_settings: Settings,
) -> None:
    assert MemoryService(benchmark_settings).sync().ok
    server = create_server(benchmark_settings)

    async def call() -> object:
        return await server._tool_manager.call_tool(
            "memory_recall",
            {"query": "ALPHA-142", "response_version": "1"},
        )

    result = asyncio.run(call())
    assert result.status == "answered"
    assert not hasattr(result, "response_version")


def test_date_arguments_cross_the_worker_json_boundary(
    benchmark_settings: Settings,
) -> None:
    server = create_server(benchmark_settings)

    async def call() -> object:
        return await server._tool_manager.call_tool(
            "memory_recall",
            {
                "query": "meeting notes",
                "date_from": "2026-01-01T12:00:00-05:00",
                "date_to": "2026-01-02T17:00:00Z",
            },
        )

    result = asyncio.run(call())
    assert result.response_version == "2"
    assert "deadline_exceeded" not in result.reason_codes


def test_artifact_only_recall_does_not_load_the_markdown_engine(
    benchmark_settings: Settings,
    monkeypatch,
) -> None:
    service = MemoryService(benchmark_settings)

    def fail_if_loaded(*args, **kwargs):
        raise AssertionError("Artifact-only recall must skip the Markdown engine.")

    monkeypatch.setattr(service, "_engine_for_generation", fail_if_loaded)

    with service._pin_recall_generation(artifact_only=True) as pinned:
        assert pinned.engine is None






def test_timeout_archives_the_dead_worker_generation_lease(tmp_path: Path) -> None:
    settings = Settings(
        memory_root=tmp_path / "vault",
        state_dir=tmp_path / "state",
        graph_path=tmp_path / "graph.json",
        graphify_mcp_url="",
    )
    settings.state_dir.mkdir(parents=True)
    lease = settings.state_dir / ".generation-lease-example-321-deadbeef.json"
    lease.write_text("{}\n", encoding="utf-8")

    moved = _archive_worker_generation_leases(settings, 321)

    assert moved == 1
    assert not lease.exists()
    assert (
        settings.state_dir / "retired-generation-leases" / lease.name
    ).is_file()




def test_sync_updates_only_the_derived_index(
    benchmark_settings: Settings,
) -> None:
    server = create_server(benchmark_settings)

    async def call() -> object:
        return await server._tool_manager.call_tool("memory_sync", {})

    result = asyncio.run(call())
    assert result.ok is True
    assert result.index.documents == 13
    assert result.index.removed == 0


def test_read_tools_do_not_build_a_missing_index(
    benchmark_settings: Settings,
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "missing-index"
    server = create_server(
        replace(benchmark_settings, state_dir=state_dir)
    )

    async def call(name: str, arguments: dict) -> object:
        return await server._tool_manager.call_tool(name, arguments)

    status = asyncio.run(call("memory_status", {}))
    recall = asyncio.run(call("memory_recall", {"query": "ALPHA-142"}))

    assert status.index.available is False
    assert status.index.stale is False
    assert recall.execution == "partial"
    assert recall.result_kind == "empty"
    assert any(
        "Artifact semantic index is not available" in warning
        for warning in recall.warnings
    )
    assert not list(state_dir.glob("index-*.sqlite"))
    assert not (state_dir / "current-index.json").exists()
    assert (state_dir / "logs" / "retrieval.jsonl").is_file()


def test_server_rejects_non_loopback_host(
    benchmark_settings: Settings,
) -> None:
    with pytest.raises(ValueError, match="requires a loopback host"):
        create_server(replace(benchmark_settings, host="0.0.0.0"))
