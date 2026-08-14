from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from ai_memory_mcp.config import Settings
from ai_memory_mcp.server import create_server


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
    assert recall.status == "no_answer"
    assert recall.warnings == [
        "Memory index is not available. Call memory_sync."
    ]
    assert not list(state_dir.glob("index-*.sqlite"))
    assert not (state_dir / "current-index.json").exists()
    assert (state_dir / "logs" / "retrieval.jsonl").is_file()


def test_server_rejects_non_loopback_host(
    benchmark_settings: Settings,
) -> None:
    with pytest.raises(ValueError, match="requires a loopback host"):
        create_server(replace(benchmark_settings, host="0.0.0.0"))
