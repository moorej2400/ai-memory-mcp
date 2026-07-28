from __future__ import annotations

import argparse
import ipaddress
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .config import Settings
from .models import RecallResponse, StatusResponse, SyncResponse
from .service import MemoryService


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def create_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or Settings.from_env()
    # Streamable HTTP has no authentication boundary in this release.
    if not _is_loopback_host(settings.host):
        raise ValueError(
            "AI Memory MCP requires a loopback host until authentication is available."
        )
    service = MemoryService(settings)
    mcp = FastMCP(
        "ai-memory",
        instructions=(
            "Use memory_recall for all memory retrieval. The server selects exact, "
            "search, neighbor, and relationship behavior. Use memory_sync after "
            "canonical Markdown changes. Use memory_status for diagnostics."
        ),
        host=settings.host,
        port=settings.port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
    )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    def memory_recall(
        query: Annotated[
            str,
            Field(
                min_length=1,
                max_length=2000,
                description="Natural-language question or exact memory identity.",
            ),
        ],
        root_scope: Annotated[
            Literal["work", "personal"] | None,
            Field(description="Optional memory domain."),
        ] = None,
        repository: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=200,
                description="Optional repository identifier.",
            ),
        ] = None,
        project: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=200,
                description="Optional project identifier.",
            ),
        ] = None,
        ticket: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=100,
                description="Optional ticket identifier.",
            ),
        ] = None,
        status: Annotated[
            Literal["active", "needs-review", "superseded", "archived"],
            Field(description="Memory lifecycle status."),
        ] = "active",
        path_prefix: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=500,
                description="Optional canonical path prefix.",
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(
                ge=1,
                le=20,
                description="Maximum evidence records.",
            ),
        ] = 8,
    ) -> RecallResponse:
        """Recall cited memory and its applicable relationships."""
        return service.recall(
            query,
            root_scope=root_scope,
            repository=repository,
            project=project,
            ticket=ticket,
            status=status,
            path_prefix=path_prefix,
            limit=limit,
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    def memory_sync() -> SyncResponse:
        """Update the derived index from canonical Markdown."""
        return service.sync()

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    def memory_status() -> StatusResponse:
        """Report source, index, Graphify, and runtime status."""
        return service.status()

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
