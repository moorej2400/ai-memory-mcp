from __future__ import annotations

import argparse
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import Settings
from .service import MemoryService


def create_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or Settings.from_env()
    service = MemoryService(settings)
    mcp = FastMCP(
        "ai-memory",
        instructions=(
            "Use memory_search for normal recall. The server plans and fuses "
            "lexical, semantic, and Graphify retrieval internally; do not call "
            "separate provider tools."
        ),
        host=settings.host,
        port=settings.port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
    )

    @mcp.tool()
    def memory_search(
        query: str,
        root_scope: str | None = None,
        repository: str | None = None,
        project: str | None = None,
        ticket: str | None = None,
        status: str = "active",
        path_prefix: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        """Retrieve cited memory evidence; filters are applied before ranking."""
        return service.search(
            query,
            root_scope=root_scope,
            repository=repository,
            project=project,
            ticket=ticket,
            status=status,
            path_prefix=path_prefix,
            limit=limit,
        )

    @mcp.tool()
    def memory_get(identity: str) -> dict[str, Any]:
        """Get one memory by memory_id, canonical relative path, or title fragment."""
        return service.get(identity)

    @mcp.tool()
    def memory_neighbors(identity: str, depth: int = 1) -> dict[str, Any]:
        """Return declared and Graphify-derived neighbors for one memory."""
        return service.neighbors(identity, depth)

    @mcp.tool()
    def memory_path(source: str, target: str) -> dict[str, Any]:
        """Return a Graphify relationship path between two memories."""
        return service.path(source, target)

    @mcp.tool()
    def memory_explain(query: str, identity: str | None = None) -> dict[str, Any]:
        """Explain why cited memories answer a query."""
        return service.explain(query, identity)

    @mcp.tool()
    def memory_refresh(mode: str = "index", force: bool = False) -> dict[str, Any]:
        """Refresh the local index; mode=full runs guarded Graphify refresh first."""
        return service.refresh(mode, force)

    @mcp.tool()
    def memory_health() -> dict[str, Any]:
        """Report canonical-source, index, Graphify, freshness, and runtime health."""
        return service.health()

    @mcp.tool()
    def memory_feedback(
        query: str,
        memory_id: str,
        relevance: str,
        note: str = "",
    ) -> dict[str, Any]:
        """Append relevance feedback for later evaluation and ranking analysis."""
        return service.feedback(query, memory_id, relevance, note)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the AI Memory MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http", "sse"),
        default="stdio",
    )
    args = parser.parse_args()
    create_server().run(transport=args.transport)


if __name__ == "__main__":
    main()

