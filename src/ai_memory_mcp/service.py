from __future__ import annotations

import importlib.metadata
import json
import platform
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit import append_event, logging_status
from .config import Settings
from .graphify import GraphifyAdapter
from .index import MemoryIndex, build_index, current_index_path
from .models import (
    CanonicalMemoryStatus,
    GraphifyRuntimeStatus,
    GraphifyStatus,
    IndexStatus,
    LoggingStatus,
    RecallCitation,
    RecallEvidence,
    RecallRelationship,
    RecallResponse,
    RuntimeStatus,
    ScopeFilter,
    SearchHit,
    StatusResponse,
    SyncIndexResult,
    SyncResponse,
)
from .platform_paths import (
    venv_bin_dir,
    venv_executable,
    venv_python,
    venv_site_packages,
)
from .retrieval import RetrievalEngine
from .text import tokenize

RELATIONSHIP_TERMS = {
    "connect",
    "connected",
    "connection",
    "path",
    "relate",
    "related",
    "relationship",
}


class MemoryService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        self._engine: RetrievalEngine | None = None

    @property
    def engine(self) -> RetrievalEngine:
        if self._engine is None:
            self._engine = RetrievalEngine(self.settings)
        return self._engine

    def recall(
        self,
        query: str,
        *,
        source_id: str | None = None,
        root_scope: str | None = None,
        repository: str | None = None,
        project: str | None = None,
        ticket: str | None = None,
        status: str = "active",
        path_prefix: str | None = None,
        limit: int | None = None,
    ) -> RecallResponse:
        started = time.perf_counter()
        scope_payload = {
            "source_id": source_id,
            "root_scope": root_scope,
            "repository": repository,
            "project": project,
            "ticket": ticket,
            "status": status,
            "path_prefix": path_prefix,
            "limit": limit,
        }
        try:
            response, diagnostics = self._recall(
                query,
                source_id=source_id,
                root_scope=root_scope,
                repository=repository,
                project=project,
                ticket=ticket,
                status=status,
                path_prefix=path_prefix,
                limit=limit,
            )
        except Exception as exc:
            append_event(
                self.settings,
                "retrieval",
                "retrieval_failed",
                {
                    "query": query,
                    "scope": scope_payload,
                    "elapsed_ms": round(
                        (time.perf_counter() - started) * 1000,
                        3,
                    ),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            raise
        append_event(
            self.settings,
            "retrieval",
            "retrieval_completed",
            {
                "query": query,
                "scope": scope_payload,
                "elapsed_ms": round(
                    (time.perf_counter() - started) * 1000,
                    3,
                ),
                "diagnostics": diagnostics,
                "response": response.model_dump(mode="json"),
            },
        )
        return response

    def _recall(
        self,
        query: str,
        *,
        source_id: str | None = None,
        root_scope: str | None = None,
        repository: str | None = None,
        project: str | None = None,
        ticket: str | None = None,
        status: str = "active",
        path_prefix: str | None = None,
        limit: int | None = None,
    ) -> tuple[RecallResponse, dict[str, Any]]:
        query = query.strip()
        if not query:
            raise ValueError("Query must not be empty.")
        scope = ScopeFilter(
            source_id=source_id,
            root_scope=root_scope,
            repository=repository,
            project=project,
            ticket=ticket,
            status=status,
            path_prefix=path_prefix,
        )
        if current_index_path(self.settings) is None:
            return (
                RecallResponse(
                    status="no_answer",
                    intent="search",
                    query=query,
                    warnings=["Memory index is not available. Call memory_sync."],
                ),
                {"route": "no-index"},
            )

        exact = self.engine.get(self._identity_candidate(query), scope)
        if exact["found"]:
            return (
                self._exact_response(query, exact["memory"], scope),
                {
                    "route": "exact",
                    "graphify": self.engine.graph.health(),
                },
            )

        mentioned = self.engine.mentioned_documents(query, scope, limit=3)
        if self._is_relationship_query(query) and len(mentioned) >= 2:
            return (
                self._relationship_response(
                    query,
                    mentioned[0],
                    mentioned[1],
                    scope,
                ),
                {
                    "route": "relationship",
                    "mentioned_documents": len(mentioned),
                    "graphify": self.engine.graph.health(),
                },
            )

        packet = self.engine.search(query, scope=scope, limit=limit)
        evidence = [
            RecallEvidence(
                memory_id=hit.memory_id,
                source_id=hit.source_id,
                heading=hit.heading,
                text=hit.text,
                score=max(0.0, hit.score),
                reasons=hit.reasons,
            )
            for hit in packet.results
        ]
        citations = [
            RecallCitation(
                memory_id=hit.memory_id,
                source_id=hit.source_id,
                path=hit.path,
                title=hit.title,
            )
            for hit in packet.results
        ]
        relationships = self._result_relationships(packet.results, scope)
        warnings = self._graph_warnings()
        if self.engine.provider_warning:
            warnings.append(self.engine.provider_warning)
        if packet.answer_status == "no_answer" and evidence:
            warnings.append(
                "No result met the answer threshold. Evidence contains "
                "best-effort leads only. Verify a lead in its canonical "
                "Markdown source before you use it."
            )
        return (
            RecallResponse(
                status=packet.answer_status,
                intent="search",
                query=query,
                evidence=evidence,
                citations=citations,
                relationships=relationships,
                warnings=warnings,
            ),
            {
                "route": "search",
                **packet.diagnostics,
            },
        )

    def sync(self) -> SyncResponse:
        indexed = build_index(self.settings, force=False)
        self._engine = RetrievalEngine(self.settings)
        return SyncResponse(
            ok=True,
            index=SyncIndexResult.model_validate(indexed),
        )

    def status(self) -> StatusResponse:
        index_path = current_index_path(self.settings)
        index_status = IndexStatus(available=False)
        if index_path:
            index = MemoryIndex(self.settings)
            metadata = index.metadata()
            index_status = IndexStatus(
                available=True,
                path=str(index.path),
                schema_version=metadata.get("schema_version"),
                built_at=metadata.get("built_at"),
                memory_root=metadata.get("memory_root"),
                memory_sources=json.loads(
                    metadata.get("memory_sources", "[]")
                ),
                semantic_dimensions=metadata.get("semantic_dimensions"),
                documents=metadata.get("documents"),
                chunks=metadata.get("chunks"),
            )
        graph_health = GraphifyAdapter(
            self.settings.graph_path,
            primary_source_id=self.settings.primary_source_id,
            source_ids=tuple(
                source.source_id
                for source in self.settings.retrieval_sources
            ),
        ).health()
        graph_age_seconds = (
            max(0.0, time.time() - self.settings.graph_path.stat().st_mtime)
            if self.settings.graph_path.exists()
            else None
        )
        graph_index_snapshot = graph_health.get("index_snapshot")
        graph_stale = bool(
            index_path
            and (
                (
                    graph_index_snapshot
                    and graph_index_snapshot != index_path.name
                )
                or (
                    not graph_index_snapshot
                    and self.settings.graph_path.exists()
                    and self.settings.graph_path.stat().st_mtime_ns
                    < index_path.stat().st_mtime_ns
                )
            )
        )
        mcp_version = importlib.metadata.version("mcp")
        return StatusResponse(
            ok=index_status.available,
            canonical_memory_root=CanonicalMemoryStatus(
                source_id=self.settings.primary_source_id,
                path=str(self.settings.memory_root),
                available=self.settings.memory_root.is_dir(),
                writable=True,
                authority="canonical-markdown",
            ),
            retrieval_sources=[
                CanonicalMemoryStatus(
                    source_id=source.source_id,
                    path=str(source.root),
                    available=source.root.is_dir(),
                    writable=False,
                    authority="canonical-markdown",
                )
                for source in self.settings.retrieval_sources
            ],
            index=index_status,
            graphify=GraphifyStatus(
                available=bool(graph_health["available"]),
                stale=graph_stale,
                path=str(graph_health["path"]),
                nodes=int(graph_health["nodes"]),
                edges=int(graph_health["edges"]),
                modified_ns=graph_health["modified_ns"],
                age_seconds=graph_age_seconds,
                build_mode=graph_health.get("build_mode"),
                index_snapshot=graph_index_snapshot,
                provider_role="internal-graph-signal",
                runtime=self._graphify_runtime(),
            ),
            logging=LoggingStatus.model_validate(
                logging_status(self.settings)
            ),
            runtime=RuntimeStatus(
                python=platform.python_version(),
                mcp=mcp_version,
                mcp_supported=mcp_version.startswith("1."),
            ),
            checked_at=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def _identity_candidate(query: str) -> str:
        words = query.split()
        if not words:
            return query
        prefixes = (
            ("get",),
            ("open",),
            ("show",),
            ("recall",),
            ("find", "memory"),
            ("get", "memory"),
            ("open", "memory"),
            ("show", "memory"),
        )
        lowered = tuple(word.casefold().strip(":") for word in words)
        for prefix in sorted(prefixes, key=len, reverse=True):
            if lowered[: len(prefix)] == prefix and len(words) > len(prefix):
                return " ".join(words[len(prefix) :]).strip(" :-")
        return query

    @staticmethod
    def _is_relationship_query(query: str) -> bool:
        return bool(set(tokenize(query)) & RELATIONSHIP_TERMS)

    def _exact_response(
        self,
        query: str,
        document: dict[str, object],
        scope: ScopeFilter,
    ) -> RecallResponse:
        memory_id = str(document["memory_id"])
        path = str(document["path"])
        title = str(document["title"])
        neighbor_packet = self.engine.neighbors(
            memory_id,
            self.settings.graph_depth,
            scope,
        )
        return RecallResponse(
            status="answered",
            intent="exact",
            query=query,
            evidence=[
                RecallEvidence(
                    memory_id=memory_id,
                    source_id=str(document["source_id"]),
                    heading="",
                    text=str(document["body"]),
                    score=1.0,
                    reasons=["exact identity"],
                )
            ],
            citations=[
                RecallCitation(
                    memory_id=memory_id,
                    source_id=str(document["source_id"]),
                    path=path,
                    title=title,
                )
            ],
            relationships=self._neighbor_relationships(
                document,
                neighbor_packet,
            )[:6],
            warnings=self._graph_warnings(),
        )

    def _relationship_response(
        self,
        query: str,
        source: dict[str, object],
        target: dict[str, object],
        scope: ScopeFilter,
    ) -> RecallResponse:
        result = self.engine.path(
            str(source["memory_id"]),
            str(target["memory_id"]),
            scope,
        )
        relationships = [
            RecallRelationship(
                source_path=str(edge.get("source_path") or ""),
                source_label=str(edge.get("source") or ""),
                relation=str(edge.get("relation") or ""),
                target_path=str(edge.get("target_path") or ""),
                target_label=str(edge.get("target") or ""),
                confidence=(
                    str(edge["confidence"])
                    if edge.get("confidence") is not None
                    else None
                ),
            )
            for edge in result.get("path", [])
        ]
        warnings = self._graph_warnings()
        if not relationships:
            warnings.append("No relationship path was found.")
        documents = (source, target)
        return RecallResponse(
            status="answered" if relationships else "no_answer",
            intent="relationship",
            query=query,
            evidence=[
                RecallEvidence(
                    memory_id=str(document["memory_id"]),
                    source_id=str(document["source_id"]),
                    heading="",
                    text=str(document["body"])[:2000],
                    score=1.0,
                    reasons=["named relationship endpoint"],
                )
                for document in documents
            ],
            citations=[
                RecallCitation(
                    memory_id=str(document["memory_id"]),
                    source_id=str(document["source_id"]),
                    path=str(document["path"]),
                    title=str(document["title"]),
                )
                for document in documents
            ],
            relationships=relationships,
            warnings=warnings,
        )

    def _result_relationships(
        self,
        hits: list[SearchHit],
        scope: ScopeFilter,
    ) -> list[RecallRelationship]:
        relationships: list[RecallRelationship] = []
        seen: set[tuple[str | None, str | None, str | None]] = set()
        # The highest-ranked record gives enough graph context for normal recall.
        # Deeper relationship questions use the dedicated internal path route.
        for hit in hits[:1]:
            packet = self.engine.neighbors(
                hit.memory_id,
                self.settings.graph_depth,
                scope,
            )
            for relationship in self._neighbor_relationships(
                {
                    "memory_id": hit.memory_id,
                    "path": hit.path,
                    "title": hit.title,
                },
                packet,
            ):
                key = (
                    relationship.source_path,
                    relationship.relation,
                    relationship.target_path or relationship.target_label,
                )
                if key not in seen:
                    seen.add(key)
                    relationships.append(relationship)
        return relationships[:1]

    @staticmethod
    def _neighbor_relationships(
        document: dict[str, object],
        packet: dict[str, Any],
    ) -> list[RecallRelationship]:
        source_memory_id = str(document["memory_id"])
        source_path = str(document["path"])
        relationships = [
            RecallRelationship(
                source_memory_id=source_memory_id,
                source_path=source_path,
                relation="declared-related",
                target_label=str(target),
            )
            for target in packet.get("declared_related", [])
        ]
        relationships.extend(
            RecallRelationship(
                source_memory_id=source_memory_id,
                source_path=source_path,
                relation=(
                    str(neighbor["relation"])
                    if neighbor.get("relation") is not None
                    else None
                ),
                target_path=(
                    str(neighbor["path"])
                    if neighbor.get("path") is not None
                    else None
                ),
                target_label=(
                    str(neighbor["label"])
                    if neighbor.get("label") is not None
                    else None
                ),
                confidence=(
                    str(neighbor["confidence"])
                    if neighbor.get("confidence") is not None
                    else None
                ),
                distance=int(neighbor["distance"]),
            )
            for neighbor in packet.get("graph_neighbors", [])
        )
        return relationships

    def _graph_warnings(self) -> list[str]:
        return (
            []
            if self.engine.graph.health()["available"]
            else ["Graph relationships are not available."]
        )

    def _graphify_runtime(self) -> GraphifyRuntimeStatus:
        project_root = Path(__file__).resolve().parents[2]
        runtime_root = project_root / ".graphify-runtime"
        scripts = venv_bin_dir(runtime_root)
        python = venv_python(runtime_root)
        executable = venv_executable(runtime_root, "graphify")
        mcp_executable = venv_executable(runtime_root, "graphify-mcp")
        expected = "0.9.26"
        package_version: str | None = None
        cli_version: str | None = None
        errors: list[str] = []
        if python.exists():
            site_packages = venv_site_packages(runtime_root)
            package_version = next(
                (
                    distribution.version
                    for distribution in importlib.metadata.distributions(
                        path=[str(path) for path in site_packages]
                    )
                    if distribution.metadata.get("Name", "").casefold()
                    == "graphifyy"
                ),
                None,
            )
            if package_version is None:
                errors.append("pinned Python cannot resolve graphifyy metadata")
        else:
            errors.append("pinned Python is missing")
        if executable.exists():
            # The console shim and distribution share this isolated venv, so
            # metadata gives the CLI version without spawning another process.
            cli_version = package_version
        else:
            errors.append("pinned Graphify CLI is missing")
        if not mcp_executable.exists():
            errors.append("pinned Graphify MCP executable is missing")
        consistent = (
            package_version == expected
            and cli_version == expected
            and mcp_executable.exists()
            and not errors
        )
        return GraphifyRuntimeStatus(
            consistent=consistent,
            expected=expected,
            package=package_version,
            cli=cli_version,
            python=str(python),
            mcp_executable=str(mcp_executable),
            scripts_dir=str(scripts),
            errors=errors,
        )
