from __future__ import annotations

import asyncio

from ai_memory_mcp.config import Settings
from ai_memory_mcp.server import create_server


def test_public_tool_surface_is_small_and_stable(
    benchmark_settings: Settings,
) -> None:
    server = create_server(benchmark_settings)
    assert {tool.name for tool in server._tool_manager.list_tools()} == {
        "memory_search",
        "memory_get",
        "memory_neighbors",
        "memory_path",
        "memory_explain",
        "memory_refresh",
        "memory_health",
        "memory_feedback",
    }


def test_tool_call_runs_full_retrieval_internally(
    benchmark_settings: Settings,
) -> None:
    server = create_server(benchmark_settings)

    async def call() -> dict:
        return await server._tool_manager.call_tool(
            "memory_search", {"query": "ALPHA-142", "limit": 1}
        )

    result = asyncio.run(call())
    assert result["answer_status"] == "answered"
    assert result["results"][0]["memory_id"] == "mem-alpha-retry"
    assert result["plan"]["retrievers"] == ["lexical", "semantic", "graphify"]

