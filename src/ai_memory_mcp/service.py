from __future__ import annotations

import importlib.metadata
import hashlib
import json
import platform
import sqlite3
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

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
from .generation import (
    current_graph_path,
    generation_health,
    lease_current_generation,
    load_current_generation,
    manifest_component_path,
)
from .index import MemoryIndex, current_index_path
from .models import (
    ArtifactIndexResult,
    CanonicalMemoryStatus,
    ArtifactDatabaseStatus,
    ArtifactVectorStatus,
    GenerationStatus,
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


@dataclass(frozen=True, slots=True)
class RecallGeneration:
    generation_id: str | None
    engine: RetrievalEngine | None
    artifact_search: ArtifactSearch | None
    artifact_vector_path: Path | None
    artifact_change_counter: int | None
    warnings: tuple[str, ...] = ()


class MemoryService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        self._engine: RetrievalEngine | None = None
        self._pinned_engine: ContextVar[RetrievalEngine | None] = ContextVar(
            "ai_memory_pinned_engine",
            default=None,
        )
        self._artifact_schema_verified: Path | None = None
        self._artifact_schema_lock = threading.Lock()

    @property
    def engine(self) -> RetrievalEngine:
        pinned = self._pinned_engine.get()
        if pinned is not None:
            return pinned
        if self._engine is None:
            self._engine = RetrievalEngine(self.settings)
        return self._engine

    @staticmethod
    def _graph_matches_generation(
        generation: dict[str, Any],
        graph_path: Path,
        graph_health: dict[str, Any],
    ) -> bool:
        layers = generation.get("layers")
        expected = layers.get("graphify") if isinstance(layers, dict) else None
        if not isinstance(expected, dict):
            return False
        digest = hashlib.sha256()
        try:
            with graph_path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            return bool(
                int(expected.get("bytes", -1)) == graph_path.stat().st_size
                and int(expected.get("corpus_size", -1))
                == int(graph_health.get("nodes", -2))
                and int(expected.get("edges", -1))
                == int(graph_health.get("edges", -2))
                and str(expected.get("sha256", "")) == digest.hexdigest()
            )
        except (OSError, TypeError, ValueError):
            return False

    def _engine_for_generation(
        self,
        generation: dict[str, Any] | None,
    ) -> RetrievalEngine | None:
        markdown_path = (
            manifest_component_path(
                self.settings,
                generation,
                "markdown_snapshot",
            )
            if generation is not None
            else current_index_path(self.settings)
        )
        if markdown_path is None:
            return None
        generation_id = (
            str(generation["generation_id"])
            if generation is not None
            else None
        )
        graph_path = (
            manifest_component_path(
                self.settings,
                generation,
                "graph_snapshot",
            )
            if generation is not None
            else current_graph_path(self.settings)
        )
        if generation is not None and graph_path is None:
            # A missing generation component must not fall back to a graph from
            # another generation. The adapter reports this sentinel as absent.
            graph_path = self.settings.state_dir / ".missing-generation-graph"
        if graph_path is not None:
            try:
                graph_health = GraphifyAdapter(
                    graph_path,
                    primary_source_id=self.settings.primary_source_id,
                    source_ids=tuple(
                        source.source_id
                        for source in self.settings.retrieval_sources
                    ),
                ).health()
                if generation is not None and not self._graph_matches_generation(
                    generation,
                    graph_path,
                    graph_health,
                ):
                    graph_path = (
                        self.settings.state_dir / ".missing-generation-graph"
                    )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                graph_path = self.settings.state_dir / ".missing-generation-graph"
        if (
            self._engine is None
            or self._engine.generation_id != generation_id
            or self._engine.index.path != markdown_path
            or self._engine.graph.graph_path != graph_path
        ):
            self._engine = RetrievalEngine(
                self.settings,
                index_path=markdown_path,
                graph_path=graph_path,
                generation_id=generation_id,
            )
        try:
            self._engine.index.metadata()
        except (OSError, TypeError, ValueError, sqlite3.DatabaseError):
            return None
        return self._engine

    @contextmanager
    def _pin_recall_generation(
        self,
        *,
        artifact_only: bool = False,
    ) -> Iterator[RecallGeneration]:
        with lease_current_generation(self.settings) as generation:
            engine = self._engine_for_generation(generation)
            token = self._pinned_engine.set(engine)
            generation_id = (
                str(generation["generation_id"])
                if generation is not None
                else None
            )
            artifact_vector_path = (
                manifest_component_path(
                    self.settings,
                    generation,
                    "artifact_snapshot",
                )
                if generation is not None
                else None
            )
            if generation is None and (artifact_only or engine is None):
                from .artifacts.vector_index import current_artifact_index_path

                artifact_vector_path = current_artifact_index_path(
                    self.settings
                )
            expected_counter = (
                int(generation["artifact_change_counter"])
                if generation is not None
                else None
            )
            component_warnings: list[str] = []
            health = generation_health(self.settings)
            failure_at = str(health.get("last_failure", {}).get("at") or "")
            success_at = str(health.get("last_success", {}).get("at") or "")
            if failure_at and failure_at > success_at:
                component_warnings.append(
                    "The latest coordinated refresh failed. Recall uses the "
                    "previous verified generation."
                )
            if generation is None and engine is not None and not artifact_only:
                component_warnings.append(
                    "A coordinated retrieval generation is not available. "
                    "Run memory_sync."
                )
            elif generation is not None and engine is None:
                component_warnings.append(
                    "The Markdown component is missing from the active generation. "
                    "Run memory_sync."
                )
            elif engine is not None:
                try:
                    markdown_stale = engine.index.canonical_stale()
                except (OSError, TypeError, ValueError, sqlite3.DatabaseError):
                    markdown_stale = True
                if markdown_stale:
                    component_warnings.append(
                        "Canonical Markdown is newer than the active retrieval "
                        "generation. Run memory_sync."
                    )
            if generation is not None and artifact_vector_path is None:
                component_warnings.append(
                    "The artifact semantic component is missing from the active "
                    "generation. Run memory_sync."
                )
            if generation is not None and manifest_component_path(
                self.settings,
                generation,
                "graph_snapshot",
            ) is None:
                component_warnings.append(
                    "The graph component is missing from the active generation. "
                    "Run memory_sync."
                )
            elif (
                engine is not None
                and engine.graph.graph_path.name == ".missing-generation-graph"
            ):
                component_warnings.append(
                    "The graph component is unavailable. Scoped lexical and "
                    "semantic recall remain available."
                )
            try:
                if generation is None and engine is not None and not artifact_only:
                    yield RecallGeneration(
                        generation_id=None,
                        engine=engine,
                        artifact_search=None,
                        artifact_vector_path=None,
                        artifact_change_counter=None,
                        warnings=tuple(component_warnings),
                    )
                    return
                if not self._artifact_schema_available():
                    yield RecallGeneration(
                        generation_id=generation_id,
                        engine=engine,
                        artifact_search=None,
                        artifact_vector_path=artifact_vector_path,
                        artifact_change_counter=expected_counter,
                        warnings=tuple(component_warnings),
                    )
                    return
                with connect_artifact_db(
                    self.settings.artifact_db,
                    read_only=True,
                ) as connection:
                    # The first read starts one SQLite snapshot that remains stable
                    # for every raw artifact query in this recall.
                    connection.execute("BEGIN")
                    row = connection.execute(
                        "SELECT value FROM artifact_metadata "
                        "WHERE key = 'change_counter'"
                    ).fetchone()
                    current_counter = int(row[0]) if row is not None else -1
                    if expected_counter is not None and current_counter != expected_counter:
                        yield RecallGeneration(
                            generation_id=generation_id,
                            engine=engine,
                            artifact_search=None,
                            artifact_vector_path=artifact_vector_path,
                            artifact_change_counter=expected_counter,
                            warnings=(
                                *component_warnings,
                                "Artifact data is newer than the active retrieval "
                                "generation. Run memory_sync.",
                            ),
                        )
                        return
                    yield RecallGeneration(
                        generation_id=generation_id,
                        engine=engine,
                        artifact_search=ArtifactSearch(
                            self.settings,
                            connection=connection,
                        ),
                        artifact_vector_path=artifact_vector_path,
                        artifact_change_counter=current_counter,
                        warnings=tuple(component_warnings),
                    )
            finally:
                self._pinned_engine.reset(token)

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
        # Retrieval telemetry records filter use, not private filter values.
        scope_payload = {
            "source_id": source_id is not None,
            "root_scope": root_scope is not None,
            "repository": repository is not None,
            "project": project is not None,
            "ticket": ticket is not None,
            "status_nondefault": status != "active",
            "path_prefix": path_prefix is not None,
            "source_label": source_label is not None,
            "source_instance": source_instance is not None,
            "artifact_kind": artifact_kind is not None,
            "date_from": date_from is not None,
            "date_to": date_to is not None,
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
        # All retrieval telemetry uses one privacy boundary. Markdown queries
        # and scope values can contain the same private data as raw artifacts.
        artifact_audit_route = True
        try:
            with self._pin_recall_generation(
                artifact_only=(
                    artifact_scope_requested
                    or query.strip().startswith("artifact://")
                ),
            ) as pinned:
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
                    pinned=pinned,
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
                    "error_sha256": hashlib.sha256(
                        str(exc).encode("utf-8")
                    ).hexdigest(),
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
        pinned: RecallGeneration,
    ) -> tuple[RecallResponse, dict[str, Any]]:
        query = query.strip()
        if not query:
            raise ValueError("Query must not be empty.")
        artifact_latency_ms: dict[str, float] = {}
        artifact_semantic_details: dict[str, Any] = {}
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
        artifact_available = pinned.artifact_search is not None
        index_available = pinned.engine is not None

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
                warnings = list(pinned.warnings)
                if not warnings:
                    warnings.append("Artifact database is not available.")
                return (
                    RecallResponse(
                        status="no_answer",
                        intent="exact",
                        query=query,
                        warnings=warnings,
                    ),
                    {
                        "route": "artifact-missing",
                        "generation_id": pinned.generation_id,
                    },
                )
            artifact_scope = ArtifactScope(
                source=source_label,
                source_instance=source_instance,
                entities=(artifact_kind,) if artifact_kind else (),
                date_from=date_from,
                date_to=date_to,
            )
            try:
                provider_started = time.perf_counter()
                exact_hit = pinned.artifact_search.get(query, artifact_scope)
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
            artifact_latency_ms["artifact_fts"] = round(
                (time.perf_counter() - provider_started) * 1000,
                3,
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
            diagnostics["generation_id"] = pinned.generation_id
            diagnostics.setdefault("provider_latency_ms", {}).update(
                artifact_latency_ms
            )
            response.warnings.extend(pinned.warnings)
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
                provider_started = time.perf_counter()
                artifact_hits = pinned.artifact_search.search(
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
                artifact_latency_ms["artifact_fts"] = round(
                    (time.perf_counter() - provider_started) * 1000,
                    3,
                )
                from .artifacts.vector_index import search_artifact_vectors

                if pinned.artifact_vector_path is None:
                    artifact_semantic_warning = (
                        "Artifact semantic index is not available. "
                        "Raw artifact search remains available."
                    )
                else:
                    try:
                        provider_started = time.perf_counter()
                        semantic = search_artifact_vectors(
                            self.settings,
                            query,
                            artifact_scope,
                            limit=max(40, requested_limit * 8),
                            index_path=pinned.artifact_vector_path,
                            expected_change_counter=pinned.artifact_change_counter,
                        )
                    except ARTIFACT_PROVIDER_ERRORS:
                        artifact_semantic_warning = (
                            "Artifact semantic index is not available. "
                            "Raw artifact search remains available."
                        )
                    else:
                        artifact_latency_ms["artifact_vector"] = round(
                            (time.perf_counter() - provider_started) * 1000,
                            3,
                        )
                        artifact_hits.extend(semantic.hits)
                        artifact_semantic_details = {
                            "backend": semantic.backend,
                            "candidates": semantic.candidate_count,
                        }
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
            warnings.extend(pinned.warnings)
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
                    "generation_id": pinned.generation_id,
                    "provider_latency_ms": artifact_latency_ms,
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
                response.warnings.extend(pinned.warnings)
                return (
                    response,
                    {
                        "route": "exact",
                        "graphify": self.engine.graph.health(),
                        "generation_id": pinned.generation_id,
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
                response.warnings.extend(pinned.warnings)
                return (
                    response,
                    {
                        "route": "relationship",
                        "mentioned_documents": len(mentioned),
                        "graphify": self.engine.graph.health(),
                        "generation_id": pinned.generation_id,
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
        response.warnings.extend(pinned.warnings)
        diagnostics["artifact_searched"] = bool(
            use_artifacts and artifact_available
        )
        diagnostics["generation_id"] = pinned.generation_id
        diagnostics.setdefault("provider_latency_ms", {}).update(
            artifact_latency_ms
        )
        if artifact_semantic_details:
            diagnostics["artifact_semantic_search"] = (
                artifact_semantic_details
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
        if self._artifact_schema_verified == self.settings.artifact_db:
            return True
        if not self.settings.artifact_db.is_file():
            return False
        with self._artifact_schema_lock:
            if self._artifact_schema_verified == self.settings.artifact_db:
                return True
            try:
                require_current_artifact_schema(self.settings)
            except ARTIFACT_PROVIDER_ERRORS:
                return False
            # Schema migrations are explicit. Cache only a successful check so a
            # database created later in this process can still become available.
            self._artifact_schema_verified = self.settings.artifact_db
        return True

    @staticmethod
    def _audit_recall_payload(
        query: str,
        response: RecallResponse,
        *,
        protect_query: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        payload: dict[str, Any] = {
            "status": response.status,
            "intent": response.intent,
            "evidence_count": len(response.evidence),
            "citation_count": len(response.citations),
            "warning_count": len(response.warnings),
        }
        sanitized: list[dict[str, Any]] = []
        for evidence in response.evidence:
            sanitized.append(
                {
                    "evidence_class": evidence.evidence_class,
                    "score": evidence.score,
                    "text_sha256": hashlib.sha256(
                        evidence.text.encode("utf-8")
                    ).hexdigest(),
                    "text_characters": len(evidence.text),
                    "identity_sha256": hashlib.sha256(
                        str(
                            evidence.artifact_uri
                            or evidence.memory_id
                            or ""
                        ).encode("utf-8")
                    ).hexdigest(),
                }
            )
        payload["evidence"] = sanitized
        payload["query_sha256"] = hashlib.sha256(query.encode("utf-8")).hexdigest()
        payload["query_characters"] = len(query)
        return MemoryService._audit_query_payload(query, True), payload

    @staticmethod
    def _audit_query_payload(query: str, protect: bool) -> dict[str, Any]:
        return {
            "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            "query_characters": len(query),
        }

    def sync(self) -> SyncResponse:
        from .generation import refresh_generation

        try:
            generation = refresh_generation(self.settings)
        except Exception as exc:
            return SyncResponse(
                ok=False,
                errors=[f"Generation: {type(exc).__name__}: {exc}"],
            )
        index_result = SyncIndexResult.model_validate(
            generation.pop("_index_result")
        )
        artifact_result = ArtifactIndexResult.model_validate(
            generation.pop("_artifact_result")
        )
        self._engine = RetrievalEngine(self.settings)
        retention = generation["retention"]
        return SyncResponse(
            ok=True,
            generation_id=str(generation["generation_id"]),
            graph_snapshot=str(generation["graph_snapshot"]),
            index=index_result,
            artifact_index=artifact_result,
            retention_removed_files=int(retention["removed_files"]),
            retention_removed_bytes=int(retention["removed_bytes"]),
        )

    def status(self) -> StatusResponse:
        from .artifacts.vector_index import current_artifact_index_path
        from .generation import generation_health

        generation = load_current_generation(self.settings)
        health_state = generation_health(self.settings)
        last_success = health_state.get("last_success", {})
        last_failure = health_state.get("last_failure", {})
        generation_id = (
            str(generation["generation_id"])
            if generation is not None
            else None
        )
        last_success_at = (
            str(last_success.get("at")) if last_success.get("at") else None
        )
        last_failure_at = (
            str(last_failure.get("at")) if last_failure.get("at") else None
        )
        layer_health = health_state.get("layers", {})
        retention_health = last_success.get("retention", {})

        def layer_failure_at(name: str) -> str | None:
            failure = layer_health.get(name, {}).get("last_failure", {})
            return str(failure.get("at")) if failure.get("at") else None

        unresolved_failure = bool(
            last_failure_at
            and (last_success_at is None or last_failure_at > last_success_at)
        )
        index_path = (
            manifest_component_path(
                self.settings,
                generation,
                "markdown_snapshot",
            )
            if generation is not None
            else current_index_path(self.settings)
        )
        index_status = IndexStatus(available=False)
        if index_path:
            try:
                index = MemoryIndex(self.settings, path=index_path)
                metadata = index.metadata()
                index_status = IndexStatus(
                    available=True,
                    stale=generation is None or index.canonical_stale(),
                    generation_id=generation_id,
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
                    ann_backend=metadata.get("ann_backend"),
                    documents=metadata.get("documents"),
                    chunks=metadata.get("chunks"),
                    byte_count=index.path.stat().st_size,
                    age_seconds=max(
                        0.0,
                        time.time() - index.path.stat().st_mtime,
                    ),
                    last_success_at=last_success_at,
                    last_failure_at=layer_failure_at("markdown"),
                )
            except (OSError, ValueError, TypeError, sqlite3.DatabaseError):
                index_status = IndexStatus(
                    available=False,
                    stale=True,
                    generation_id=generation_id,
                    path=str(index_path),
                    last_success_at=last_success_at,
                    last_failure_at=layer_failure_at("markdown"),
                )
        graph_path = (
            manifest_component_path(
                self.settings,
                generation,
                "graph_snapshot",
            )
            if generation is not None
            else current_graph_path(self.settings)
        )
        if graph_path is None:
            graph_path = self.settings.state_dir / ".missing-generation-graph"
        try:
            graph_health = GraphifyAdapter(
                graph_path,
                primary_source_id=self.settings.primary_source_id,
                source_ids=tuple(
                    source.source_id
                    for source in self.settings.retrieval_sources
                ),
            ).health()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            graph_health = {
                "available": False,
                "path": str(graph_path),
                "nodes": 0,
                "edges": 0,
                "modified_ns": None,
                "build_mode": None,
                "index_snapshot": None,
            }
        graph_age_seconds = (
            max(0.0, time.time() - graph_path.stat().st_mtime)
            if graph_path.exists()
            else None
        )
        graph_index_snapshot = graph_health.get("index_snapshot")
        graph_content_matches = bool(
            generation is not None
            and self._graph_matches_generation(
                generation,
                graph_path,
                graph_health,
            )
        )
        graph_stale = bool(
            generation is not None
            and not graph_content_matches
            or index_path
            and (
                (
                    graph_index_snapshot
                    and graph_index_snapshot != index_path.name
                )
                or (
                    not graph_index_snapshot
                    and graph_path.exists()
                    and graph_path.stat().st_mtime_ns
                    < index_path.stat().st_mtime_ns
                )
            )
        )
        mcp_version = importlib.metadata.version("mcp")
        artifact_status = self._artifact_status()
        artifact_vector_path = (
            manifest_component_path(
                self.settings,
                generation,
                "artifact_snapshot",
            )
            if generation is not None
            else current_artifact_index_path(self.settings)
        )
        artifact_vector_status = ArtifactVectorStatus(
            available=False,
            stale=True,
            generation_id=generation_id,
            path=str(artifact_vector_path) if artifact_vector_path else None,
            last_success_at=last_success_at,
            last_failure_at=layer_failure_at("artifact-vector"),
        )
        if artifact_vector_path is not None:
            try:
                with sqlite3.connect(
                    f"file:{artifact_vector_path.resolve().as_posix()}?mode=ro",
                    uri=True,
                ) as connection:
                    integrity = str(
                        connection.execute("PRAGMA quick_check").fetchone()[0]
                    )
                    if integrity != "ok":
                        raise sqlite3.DatabaseError(
                            "The artifact vector index failed quick-check."
                        )
                    metadata = {
                        str(key): str(value)
                        for key, value in connection.execute(
                            "SELECT key, value FROM metadata"
                        )
                    }
                vector_counter = int(
                    metadata.get("artifact_change_counter", "-1")
                )
                vector_stale = bool(
                    generation is None
                    or vector_counter != artifact_status.change_counter
                    or vector_counter
                    != int(generation["artifact_change_counter"])
                )
                artifact_vector_status = ArtifactVectorStatus(
                    available=True,
                    stale=vector_stale,
                    generation_id=generation_id,
                    path=str(artifact_vector_path),
                    change_counter=vector_counter,
                    bursts=int(metadata.get("bursts", "0")),
                    embedded_bursts=int(
                        metadata.get("embedded_bursts", "0")
                    ),
                    ann_backend=metadata.get("ann_backend"),
                    byte_count=artifact_vector_path.stat().st_size,
                    age_seconds=max(
                        0.0,
                        time.time() - artifact_vector_path.stat().st_mtime,
                    ),
                    last_success_at=last_success_at,
                    last_failure_at=layer_failure_at("artifact-vector"),
                )
            except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
                pass
        generation_consistent = bool(
            generation is not None
            and index_status.available
            and not index_status.stale
            and artifact_status.available
            and artifact_vector_status.available
            and not artifact_vector_status.stale
            and graph_health["available"]
            and not graph_stale
            and not unresolved_failure
        )
        published_at = (
            str(generation.get("published_at"))
            if generation is not None
            else None
        )
        generation_age = None
        if published_at:
            try:
                generation_age = max(
                    0.0,
                    (
                        datetime.now(timezone.utc)
                        - datetime.fromisoformat(
                            published_at.replace("Z", "+00:00")
                        )
                    ).total_seconds(),
                )
            except ValueError:
                generation_consistent = False
        return StatusResponse(
            ok=(
                generation_consistent
                and self.settings.memory_root.is_dir()
            ),
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
            artifact_vector=artifact_vector_status,
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
                generation_id=generation_id,
                last_success_at=last_success_at,
                last_failure_at=layer_failure_at("graphify"),
                provider_role="internal-graph-signal",
                runtime=self._graphify_runtime(),
            ),
            generation=GenerationStatus(
                available=generation is not None,
                consistent=generation_consistent,
                generation_id=generation_id,
                published_at=published_at,
                manifest_path=(
                    str(generation.get("manifest_path"))
                    if generation is not None
                    else None
                ),
                age_seconds=generation_age,
                last_success_at=last_success_at,
                last_failure_at=last_failure_at,
                last_failure_layer=(
                    str(last_failure.get("layer"))
                    if last_failure.get("layer")
                    else None
                ),
                storage_bytes=int(last_success.get("storage_bytes", 0)),
                storage_growth_bytes=int(
                    last_success.get("storage_growth_bytes", 0)
                ),
                verified_generations=int(
                    retention_health.get("verified_generations", 0)
                ),
                last_good_available=bool(
                    retention_health.get("last_good_available", False)
                ),
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
                last_batch_at = connection.execute(
                    "SELECT MAX(completed_at) FROM artifact_batches "
                    "WHERE status = 'ok'"
                ).fetchone()[0]
        except Exception as exc:
            return ArtifactDatabaseStatus(
                available=False,
                path=str(path),
                integrity=type(exc).__name__,
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
            change_counter=health.change_counter,
            byte_count=health.byte_count,
            last_success_at=last_batch_at,
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
