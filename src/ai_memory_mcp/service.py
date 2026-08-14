from __future__ import annotations

import importlib.metadata
import hashlib
import json
import platform
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifacts.identity import parse_artifact_uri
from .artifacts.models import ArtifactReadResponse, ArtifactScope
from .artifacts.schema import (
    artifact_database_status,
    connect_artifact_db,
    require_current_artifact_schema,
)
from .artifacts.search import ArtifactSearch
from .audit import append_event, logging_status
from .config import Settings
from .graphify import GraphifyAdapter
from .index import MemoryIndex, build_index, current_index_path
from .models import (
    CanonicalMemoryStatus,
    ArtifactDatabaseStatus,
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
from .retrieval import (
    RAW_ARTIFACT_WARNING,
    RetrievalEngine,
    merge_artifact_evidence,
)
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
ARTIFACT_PROVIDER_ERRORS = (
    FileNotFoundError,
    OSError,
    RuntimeError,
    ValueError,
    sqlite3.DatabaseError,
)


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
        source_label: str | None = None,
        source_instance: str | None = None,
        artifact_kind: str | None = None,
        date_from: str | datetime | None = None,
        date_to: str | datetime | None = None,
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
            "source_label": source_label,
            "source_instance": source_instance,
            "artifact_kind": artifact_kind,
            "date_from": date_from,
            "date_to": date_to,
            "limit": limit,
        }
        artifact_scope_requested = any(
            value is not None
            for value in (
                source_label,
                source_instance,
                artifact_kind,
                date_from,
                date_to,
            )
        )
        markdown_scope_requested = any(
            value is not None
            for value in (
                source_id,
                root_scope,
                repository,
                project,
                ticket,
                path_prefix,
            )
        ) or status != "active"
        artifact_schema_available = self._artifact_schema_available()
        # Select privacy from the requested route. A failed or empty artifact
        # search has no returned raw evidence from which to infer this policy.
        artifact_audit_route = bool(
            query.strip().startswith("artifact://")
            or artifact_scope_requested
            or (
                not markdown_scope_requested
                and artifact_schema_available
            )
        )
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
                source_label=source_label,
                source_instance=source_instance,
                artifact_kind=artifact_kind,
                date_from=date_from,
                date_to=date_to,
                limit=limit,
            )
        except Exception as exc:
            append_event(
                self.settings,
                "retrieval",
                "retrieval_failed",
                {
                    **self._audit_query_payload(query, artifact_audit_route),
                    "scope": scope_payload,
                    "elapsed_ms": round(
                        (time.perf_counter() - started) * 1000,
                        3,
                    ),
                    "error_type": type(exc).__name__,
                    **(
                        {
                            "error_sha256": hashlib.sha256(
                                str(exc).encode("utf-8")
                            ).hexdigest()
                        }
                        if artifact_audit_route
                        else {"error": str(exc)}
                    ),
                },
            )
            raise
        audit_query, audit_response = self._audit_recall_payload(
            query,
            response,
            protect_query=artifact_audit_route,
        )
        append_event(
            self.settings,
            "retrieval",
            "retrieval_completed",
            {
                **audit_query,
                "scope": scope_payload,
                "elapsed_ms": round(
                    (time.perf_counter() - started) * 1000,
                    3,
                ),
                "diagnostics": diagnostics,
                "response": audit_response,
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
        source_label: str | None = None,
        source_instance: str | None = None,
        artifact_kind: str | None = None,
        date_from: str | datetime | None = None,
        date_to: str | datetime | None = None,
        limit: int | None = None,
    ) -> tuple[RecallResponse, dict[str, Any]]:
        query = query.strip()
        if not query:
            raise ValueError("Query must not be empty.")
        requested_limit = max(1, min(limit or self.settings.result_limit, 20))
        scope = ScopeFilter(
            source_id=source_id,
            root_scope=root_scope,
            repository=repository,
            project=project,
            ticket=ticket,
            status=status,
            path_prefix=path_prefix,
        )
        artifact_filters = any(
            value is not None
            for value in (
                source_label,
                source_instance,
                artifact_kind,
                date_from,
                date_to,
            )
        )
        markdown_filters = any(
            value is not None
            for value in (
                source_id,
                root_scope,
                repository,
                project,
                ticket,
                path_prefix,
            )
        ) or status != "active"
        artifact_available = self._artifact_schema_available()
        index_available = current_index_path(self.settings) is not None

        if artifact_filters and markdown_filters:
            raise ValueError(
                "Markdown filters and artifact filters cannot be combined."
            )

        if query.startswith("artifact://"):
            parse_artifact_uri(query)
            if markdown_filters:
                raise ValueError(
                    "Markdown filters cannot be used with an artifact reference."
                )
            if not artifact_available:
                return (
                    RecallResponse(
                        status="no_answer",
                        intent="exact",
                        query=query,
                        warnings=["Artifact database is not available."],
                    ),
                    {"route": "artifact-missing"},
                )
            artifact_scope = ArtifactScope(
                source=source_label,
                source_instance=source_instance,
                entities=(artifact_kind,) if artifact_kind else (),
                date_from=date_from,
                date_to=date_to,
            )
            try:
                exact_hit = ArtifactSearch(self.settings).get(query, artifact_scope)
            except ARTIFACT_PROVIDER_ERRORS:
                return (
                    RecallResponse(
                        status="no_answer",
                        intent="exact",
                        query=query,
                        warnings=["Artifact database is not available."],
                    ),
                    {"route": "artifact-unavailable"},
                )
            packet = merge_artifact_evidence(
                query,
                None,
                [exact_hit],
                settings=self.settings,
                now=datetime.now(timezone.utc),
                limit=1,
            )
            response, diagnostics = self._packet_response(
                query,
                packet,
                scope,
                intent="exact",
                markdown_used=False,
            )
            diagnostics["artifact_searched"] = True
            return response, diagnostics

        artifact_hits = []
        artifact_warning: str | None = None
        artifact_semantic_warning: str | None = None
        use_artifacts = not markdown_filters or artifact_filters
        if use_artifacts and artifact_available:
            artifact_scope = ArtifactScope(
                source=source_label,
                source_instance=source_instance,
                entities=(artifact_kind,) if artifact_kind else (),
                date_from=date_from,
                date_to=date_to,
            )
            try:
                artifact_hits = ArtifactSearch(self.settings).search(
                    query,
                    artifact_scope,
                    limit=max(40, requested_limit * 8),
                )
            except ARTIFACT_PROVIDER_ERRORS:
                artifact_warning = (
                    "Artifact database is not available for this filter."
                    if artifact_filters
                    else "Artifact database is not available."
                )
            else:
                from .artifacts.vector_index import search_artifact_vectors

                try:
                    semantic = search_artifact_vectors(
                        self.settings,
                        query,
                        artifact_scope,
                        limit=max(40, requested_limit * 8),
                    )
                except ARTIFACT_PROVIDER_ERRORS:
                    artifact_semantic_warning = (
                        "Artifact semantic index is not available. "
                        "Raw artifact search remains available."
                    )
                else:
                    artifact_hits.extend(semantic.hits)
                    if semantic.stale:
                        artifact_semantic_warning = (
                            "Artifact semantic index is stale. Raw artifact search "
                            "remains available."
                        )
        elif artifact_filters and not artifact_available:
            artifact_warning = "Artifact database is not available for this filter."

        use_markdown = index_available and not artifact_filters
        if not use_markdown and not artifact_hits:
            warnings = []
            if not index_available and not artifact_filters:
                warnings.append(
                    "Memory index is not available. Call memory_sync."
                )
            if artifact_warning:
                warnings.append(artifact_warning)
            if artifact_semantic_warning:
                warnings.append(artifact_semantic_warning)
            return (
                RecallResponse(
                    status="no_answer",
                    intent="search",
                    query=query,
                    warnings=warnings,
                ),
                {
                    "route": "no-provider",
                    "artifact_searched": bool(use_artifacts and artifact_available),
                },
            )

        if use_markdown:
            exact = self.engine.get(self._identity_candidate(query), scope)
            if exact["found"] and not artifact_hits:
                response = self._exact_response(query, exact["memory"], scope)
                if artifact_warning:
                    response.warnings.append(artifact_warning)
                if artifact_semantic_warning:
                    response.warnings.append(artifact_semantic_warning)
                return (
                    response,
                    {
                        "route": "exact",
                        "graphify": self.engine.graph.health(),
                    },
                )

            mentioned = self.engine.mentioned_documents(query, scope, limit=3)
            if (
                not artifact_hits
                and self._is_relationship_query(query)
                and len(mentioned) >= 2
            ):
                response = self._relationship_response(
                    query,
                    mentioned[0],
                    mentioned[1],
                    scope,
                )
                if artifact_warning:
                    response.warnings.append(artifact_warning)
                if artifact_semantic_warning:
                    response.warnings.append(artifact_semantic_warning)
                return (
                    response,
                    {
                        "route": "relationship",
                        "mentioned_documents": len(mentioned),
                        "graphify": self.engine.graph.health(),
                    },
                )
            markdown_packet = self.engine.search(
                query,
                scope=scope,
                limit=requested_limit,
            )
        else:
            markdown_packet = None
        packet = merge_artifact_evidence(
            query,
            markdown_packet,
            artifact_hits,
            settings=self.settings,
            now=datetime.now(timezone.utc),
            limit=requested_limit,
        )
        response, diagnostics = self._packet_response(
            query,
            packet,
            scope,
            intent="search",
            markdown_used=markdown_packet is not None,
        )
        if artifact_warning:
            response.warnings.append(artifact_warning)
        if artifact_semantic_warning:
            response.warnings.append(artifact_semantic_warning)
        diagnostics["artifact_searched"] = bool(
            use_artifacts and artifact_available
        )
        return response, diagnostics

    def _packet_response(
        self,
        query: str,
        packet: Any,
        scope: ScopeFilter,
        *,
        intent: str,
        markdown_used: bool,
    ) -> tuple[RecallResponse, dict[str, Any]]:
        evidence = [
            RecallEvidence(
                memory_id=hit.memory_id,
                source_id=hit.source_id,
                heading=hit.heading,
                text=hit.text,
                score=max(0.0, hit.score),
                reasons=hit.reasons,
                evidence_class=hit.evidence_class,
                artifact_uri=hit.artifact_uri,
                source_label=hit.source_label,
                source_instance=hit.source_instance,
                occurred_at=hit.occurred_at,
            )
            for hit in packet.results
        ]
        citations = [
            RecallCitation(
                memory_id=hit.memory_id,
                source_id=hit.source_id,
                path=hit.path,
                title=hit.title,
                evidence_class=hit.evidence_class,
                artifact_uri=hit.artifact_uri,
                source_label=hit.source_label,
                source_instance=hit.source_instance,
                occurred_at=hit.occurred_at,
            )
            for hit in packet.results
        ]
        distilled_hits = [
            hit for hit in packet.results if hit.evidence_class == "distilled"
        ]
        relationships = (
            self._result_relationships(distilled_hits, scope)
            if markdown_used and distilled_hits
            else []
        )
        warnings = self._graph_warnings() if markdown_used else []
        if markdown_used and self.engine.provider_warning:
            warnings.append(self.engine.provider_warning)
        if evidence and evidence[0].evidence_class in {"raw", "burst"}:
            warnings.append(RAW_ARTIFACT_WARNING)
        elif packet.answer_status == "no_answer" and evidence:
            warnings.append(
                "No result met the answer threshold. Evidence contains "
                "best-effort leads only. Verify a lead in its canonical "
                "Markdown source before you use it."
            )
        return (
            RecallResponse(
                status=packet.answer_status,
                intent=intent,
                query=query,
                evidence=evidence,
                citations=citations,
                relationships=relationships,
                warnings=warnings,
            ),
            {
                "route": "artifact" if not markdown_used else "search",
                **packet.diagnostics,
            },
        )

    def artifact_read(
        self,
        reference: str,
        *,
        cursor: str | None = None,
        direction: str = "around",
        limit: int = 50,
        include_payload: bool = False,
    ) -> ArtifactReadResponse:
        parse_artifact_uri(reference)
        if not self.settings.artifact_db.is_file():
            raise FileNotFoundError("Artifact database is not available.")
        return ArtifactSearch(self.settings).read(
            reference,
            cursor=cursor,
            direction=direction,
            limit=limit,
            include_payload=include_payload,
        )

    def _artifact_schema_available(self) -> bool:
        if not self.settings.artifact_db.is_file():
            return False
        try:
            require_current_artifact_schema(self.settings)
        except ARTIFACT_PROVIDER_ERRORS:
            return False
        return True

    @staticmethod
    def _audit_recall_payload(
        query: str,
        response: RecallResponse,
        *,
        protect_query: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        payload = response.model_dump(mode="json")
        raw_paths: set[str] = set()
        sanitized: list[dict[str, Any]] = []
        contains_raw = False
        for evidence in response.evidence:
            if evidence.evidence_class == "distilled":
                sanitized.append(evidence.model_dump(mode="json"))
                continue
            contains_raw = True
            if evidence.artifact_uri:
                raw_paths.add(evidence.artifact_uri)
            sanitized.append(
                {
                    "artifact_uri": evidence.artifact_uri,
                    "evidence_class": evidence.evidence_class,
                    "source_label": evidence.source_label,
                    "score": evidence.score,
                    "text_sha256": hashlib.sha256(
                        evidence.text.encode("utf-8")
                    ).hexdigest(),
                    "text_characters": len(evidence.text),
                }
            )
        payload["evidence"] = sanitized
        if not contains_raw and not protect_query:
            return {"query": query}, payload
        payload.pop("query", None)
        payload["query_sha256"] = hashlib.sha256(query.encode("utf-8")).hexdigest()
        payload["query_characters"] = len(query)
        payload["citations"] = [
            ({"artifact_uri": citation.path}
             if citation.path in raw_paths
             else citation.model_dump(mode="json"))
            for citation in response.citations
        ]
        return MemoryService._audit_query_payload(query, True), payload

    @staticmethod
    def _audit_query_payload(query: str, protect: bool) -> dict[str, Any]:
        if not protect:
            return {"query": query}
        return {
            "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            "query_characters": len(query),
        }

    def sync(self) -> SyncResponse:
        from .artifacts.vector_index import build_artifact_vector_index

        index_result = None
        artifact_result = None
        errors: list[str] = []
        try:
            indexed = build_index(self.settings, force=False)
            index_result = SyncIndexResult.model_validate(indexed)
            self._engine = RetrievalEngine(self.settings)
        except Exception as exc:
            errors.append(f"Markdown index: {type(exc).__name__}: {exc}")
        if self.settings.artifact_db.is_file():
            try:
                artifact_result = build_artifact_vector_index(self.settings)
            except Exception as exc:
                errors.append(
                    f"Artifact index: {type(exc).__name__}: {exc}"
                )
        return SyncResponse(
            ok=index_result is not None or artifact_result is not None,
            index=index_result,
            artifact_index=artifact_result,
            errors=errors,
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
                embedding_provider=metadata.get("embedding_provider"),
                embedding_model=metadata.get("embedding_model"),
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
        artifact_status = self._artifact_status()
        return StatusResponse(
            ok=index_status.available or artifact_status.available,
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
            artifact_database=artifact_status,
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

    def _artifact_status(self) -> ArtifactDatabaseStatus:
        path = self.settings.artifact_db
        if not path.is_file():
            return ArtifactDatabaseStatus(
                available=False,
                path=str(path),
                integrity="missing",
            )
        try:
            health = artifact_database_status(self.settings)
            with connect_artifact_db(path, read_only=True) as connection:
                artifacts = int(
                    connection.execute(
                        "SELECT count(*) FROM artifacts"
                    ).fetchone()[0]
                )
                active = int(
                    connection.execute(
                        "SELECT count(*) FROM artifacts "
                        "WHERE deleted_at IS NULL AND redacted_at IS NULL"
                    ).fetchone()[0]
                )
                batches = int(
                    connection.execute(
                        "SELECT count(*) FROM artifact_batches"
                    ).fetchone()[0]
                )
                pending = int(
                    connection.execute(
                        "SELECT count(*) FROM distillation_state "
                        "WHERE status = 'pending'"
                    ).fetchone()[0]
                )
                fts_available = connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'artifacts_fts'"
                ).fetchone() is not None
        except Exception as exc:
            return ArtifactDatabaseStatus(
                available=False,
                path=str(path),
                integrity=f"{type(exc).__name__}: {exc}",
            )
        return ArtifactDatabaseStatus(
            available=health.integrity == "ok",
            path=str(path),
            schema_version=health.schema_version,
            integrity=health.integrity,
            artifacts=artifacts,
            active_artifacts=active,
            batches=batches,
            fts_available=fts_available,
            pending_distillations=pending,
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
