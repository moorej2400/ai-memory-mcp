from __future__ import annotations

import argparse
import asyncio
import ipaddress
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .artifacts.models import ArtifactReadResponse
from .config import Settings
from .models import (
    RecallCoverage,
    RecallResponse,
    RecallResponseV2,
    RecallToolResponse,
    StatusResponse,
    SyncResponse,
)
from .recall_worker import (
    WorkerDeadlineExceeded,
    WorkerExecutionFailed,
    WorkerQueueFull,
    recall_in_worker_async,
)
from .service import MemoryService
from .freshness import RECONCILIATION_INTERVAL_SECONDS, reconcile_markdown


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _reason_codes(response: RecallResponse) -> list[str]:
    return list(response._execution_state.reason_codes)


def _modern_response(response: RecallResponse, settings: Settings) -> RecallResponseV2:
    reasons = _reason_codes(response)
    execution = response._execution_state.execution
    has_results = bool(
        response.evidence or response.citations or response.relationships
    )
    result_kind: Literal["exact", "ranked", "empty"] = (
        "exact"
        if response.intent == "exact" and has_results
        else "ranked"
        if has_results
        else "empty"
    )
    return RecallResponseV2(
        execution=execution,
        result_kind=result_kind,
        intent=response.intent,
        query=response.query,
        evidence=response.evidence,
        citations=response.citations,
        relationships=response.relationships,
        reason_codes=reasons,
        coverage=response._execution_state.coverage,
        warnings=response.warnings,
    )


def create_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or Settings.from_env()
    # Streamable HTTP has no authentication boundary in this release.
    if not _is_loopback_host(settings.host):
        raise ValueError(
            "AI Memory MCP requires a loopback host until authentication is available."
        )
    service = MemoryService(settings)

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        stopped = threading.Event()

        def reconcile() -> None:
            while not stopped.is_set():
                try:
                    reconcile_markdown(settings)
                except (OSError, ValueError, sqlite3.DatabaseError):
                    # The last marker expires if reconciliation cannot finish.
                    pass
                stopped.wait(RECONCILIATION_INTERVAL_SECONDS)

        reconciler = threading.Thread(target=reconcile, daemon=True, name="markdown-reconciliation")
        reconciler.start()
        try:
            yield {}
        finally:
            stopped.set()
            await asyncio.to_thread(reconciler.join, 2.0)

    mcp = FastMCP(
        "ai-memory",
        instructions=(
            "Use memory_recall for all memory retrieval. The server selects exact, "
            "search, raw-artifact, neighbor, and relationship behavior. Use "
            "memory_artifact_read for ordered raw context. Use memory_sync after "
            "canonical Markdown or artifact data changes. Use memory_status for "
            "diagnostics."
        ),
        host=settings.host,
        port=settings.port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        lifespan=lifespan,
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
    async def memory_recall(
        query: Annotated[
            str,
            Field(
                min_length=1,
                max_length=2000,
                description="Natural-language question or exact memory identity.",
            ),
        ],
        response_version: Annotated[
            Literal["1", "2"],
            Field(description="Recall response contract version."),
        ] = "2",
        source_id: Annotated[
            str | None,
            Field(
                pattern=r"^[a-z][a-z0-9-]{0,62}$",
                description="Optional configured memory source ID.",
            ),
        ] = None,
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
        source_label: Annotated[
            str | None,
            Field(
                pattern=r"^[a-z][a-z0-9-]{0,62}$",
                description="Optional raw artifact connector label.",
            ),
        ] = None,
        source_instance: Annotated[
            str | None,
            Field(
                pattern=r"^[a-z][a-z0-9-]{0,62}$",
                description="Optional raw artifact connector instance.",
            ),
        ] = None,
        artifact_kind: Annotated[
            Literal[
                "conversation",
                "message",
                "meeting",
                "recording",
                "transcript",
                "transcript-cue",
                "attachment",
            ]
            | None,
            Field(description="Optional raw artifact entity kind."),
        ] = None,
        date_from: Annotated[
            datetime | None,
            Field(description="Optional earliest raw artifact time."),
        ] = None,
        date_to: Annotated[
            datetime | None,
            Field(description="Optional latest raw artifact time."),
        ] = None,
        limit: Annotated[
            int,
            Field(
                ge=1,
                le=20,
                description="Maximum evidence records.",
            ),
        ] = 8,
    ) -> RecallToolResponse:
        """Recall cited memory and its applicable relationships."""
        arguments = {
            "query": query,
            "source_id": source_id,
            "root_scope": root_scope,
            "repository": repository,
            "project": project,
            "ticket": ticket,
            "status": status,
            "path_prefix": path_prefix,
            "source_label": source_label,
            "source_instance": source_instance,
            "artifact_kind": artifact_kind,
            "date_from": date_from,
            "date_to": date_to,
            "limit": limit,
        }
        try:
            response = await recall_in_worker_async(settings, arguments)
            if response_version == "1" and response._execution_state.execution != "complete":
                raise WorkerExecutionFailed(
                    "Memory recall did not complete. Use response version 2 for execution details."
                )
            return (
                response
                if response_version == "1"
                else _modern_response(response, settings)
            )
        except WorkerDeadlineExceeded:
            if response_version == "1":
                # Version 1 cannot represent incomplete execution safely.
                # Let FastMCP return a tool error instead of a false empty result.
                raise
            return RecallResponseV2(
                execution="failed",
                result_kind="empty",
                intent="exact" if query.strip().startswith("artifact://") else "search",
                query=query,
                reason_codes=["deadline_exceeded"],
                coverage=RecallCoverage(),
                warnings=[
                    "Memory recall exceeded its time limit. Retry the request."
                ],
            )
        except (WorkerQueueFull, WorkerExecutionFailed) as exc:
            if response_version == "1":
                raise
            queue_full = isinstance(exc, WorkerQueueFull)
            return RecallResponseV2(
                execution="failed",
                result_kind="empty",
                intent="exact" if query.strip().startswith("artifact://") else "search",
                query=query,
                reason_codes=["queue_full" if queue_full else "worker_failed"],
                coverage=RecallCoverage(),
                warnings=[
                    "Memory recall capacity is full. Retry the request."
                    if queue_full
                    else "The memory recall worker failed. Retry the request."
                ],
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
    def memory_artifact_read(
        reference: Annotated[
            str,
            Field(
                min_length=1,
                max_length=200,
                description="Exact artifact URI.",
            ),
        ],
        cursor: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=500,
                description="Optional opaque keyset cursor.",
            ),
        ] = None,
        direction: Annotated[
            Literal["before", "after", "around"],
            Field(description="Context direction."),
        ] = "around",
        limit: Annotated[
            int,
            Field(
                ge=1,
                le=200,
                description="Maximum ordered artifact records.",
            ),
        ] = 50,
        include_payload: Annotated[
            bool,
            Field(description="Return the exact focus payload only."),
        ] = False,
    ) -> ArtifactReadResponse:
        """Read ordered raw context from one stable artifact citation."""
        return service.artifact_read(
            reference,
            cursor=cursor,
            direction=direction,
            limit=limit,
            include_payload=include_payload,
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
        """Update the derived indexes after canonical Markdown or artifact data changes."""
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
