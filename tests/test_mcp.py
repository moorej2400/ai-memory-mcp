from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from ai_memory_mcp.config import Settings
from ai_memory_mcp.recall_worker import (
    WorkerDeadlineExceeded,
    _archive_worker_generation_leases,
    _run_worker_command,
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
        "memory_sync",
        "memory_status",
    }
    assert tools["memory_recall"].annotations.readOnlyHint is True
    assert tools["memory_artifact_read"].annotations.readOnlyHint is True
    assert tools["memory_artifact_read"].annotations.idempotentHint is True
    assert tools["memory_status"].annotations.readOnlyHint is True
    assert tools["memory_sync"].annotations.readOnlyHint is False
    assert tools["memory_recall"].output_schema["additionalProperties"] is False
    assert tools["memory_recall"].parameters["properties"]["limit"]["maximum"] == 20
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
    assert result.status == "answered"
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
    assert result.status == "no_answer"
    assert any("time limit" in warning for warning in result.warnings)
    assert any(
        "does not mean the memory is absent" in warning
        for warning in result.warnings
    )


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


def test_worker_deadline_terminates_and_reaps_the_process() -> None:
    started = time.perf_counter()

    with pytest.raises(WorkerDeadlineExceeded) as raised:
        _run_worker_command(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            b"",
            0.05,
        )

    elapsed = time.perf_counter() - started
    assert elapsed < 2.0
    assert raised.value.worker_pid is not None
    assert not _process_exists(raised.value.worker_pid)


def test_worker_returns_its_result_through_a_dedicated_pipe() -> None:
    assert _run_worker_command(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'worker-result')",
        ],
        b"",
        10.0,
    ) == b"worker-result"


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


def test_worker_timeout_closes_its_sqlite_snapshot(tmp_path: Path) -> None:
    database = tmp_path / "snapshot.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE records (value INTEGER NOT NULL)")
        connection.execute("INSERT INTO records VALUES (1)")

    child_code = (
        "import sqlite3,sys,time; "
        "from pathlib import Path; "
        "database=sys.argv[1]; "
        "connection=sqlite3.connect(database); "
        "connection.execute('BEGIN'); "
        "connection.execute('SELECT value FROM records').fetchone(); "
        "Path(database+'.ready').write_text('ready\\n', encoding='utf-8'); "
        "time.sleep(60)"
    )
    with pytest.raises(WorkerDeadlineExceeded):
        _run_worker_command(
            [sys.executable, "-c", child_code, str(database)],
            b"",
            5.0,
        )

    assert Path(f"{database}.ready").is_file()
    with sqlite3.connect(database, timeout=0.1) as connection:
        connection.execute("BEGIN EXCLUSIVE")
        connection.execute("UPDATE records SET value = 2")
        connection.commit()
        value = connection.execute("SELECT value FROM records").fetchone()[0]
    assert value == 2


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
    assert recall.status == "no_answer"
    assert any(
        "no_answer result does not mean the memory is absent" in warning
        for warning in recall.warnings
    )
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
