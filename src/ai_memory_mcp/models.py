from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


@dataclass(slots=True)
class MemoryDocument:
    memory_id: str
    source_id: str
    path: str
    title: str
    body: str
    status: str = "active"
    root_scope: str = "work"
    scope_kind: str = "reference"
    scope_id: str = ""
    updated: str = ""
    review_after: str = ""
    related: list[str] = field(default_factory=list)
    identifiers: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    repos: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    content_hash: str = ""
    mtime_ns: int = 0


@dataclass(slots=True)
class MemoryChunk:
    chunk_id: str
    memory_id: str
    source_id: str
    path: str
    title: str
    heading: str
    ordinal: int
    text: str
    vector: dict[int, float]


@dataclass(slots=True)
class ScopeFilter:
    source_id: str | None = None
    root_scope: str | None = None
    repository: str | None = None
    project: str | None = None
    ticket: str | None = None
    status: str = "active"
    path_prefix: str | None = None


@dataclass(slots=True)
class SearchHit:
    memory_id: str
    source_id: str
    path: str
    title: str
    heading: str
    text: str
    score: float
    ranks: dict[str, int] = field(default_factory=dict)
    signals: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    graph_neighbors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class EvidencePacket:
    query: str
    answer_status: str
    results: list[SearchHit]
    plan: dict[str, Any]
    diagnostics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "answer_status": self.answer_status,
            "results": [result.to_dict() for result in self.results],
            "plan": self.plan,
            "diagnostics": self.diagnostics,
        }


class StrictOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecallEvidence(StrictOutput):
    memory_id: str
    source_id: str
    heading: str
    text: str
    score: float = Field(ge=0.0)
    reasons: list[str] = Field(default_factory=list)


class RecallCitation(StrictOutput):
    memory_id: str
    source_id: str
    path: str
    title: str


class RecallRelationship(StrictOutput):
    source_memory_id: str | None = None
    source_path: str | None = None
    source_label: str | None = None
    relation: str | None = None
    target_memory_id: str | None = None
    target_path: str | None = None
    target_label: str | None = None
    confidence: str | None = None
    distance: int | None = Field(default=None, ge=1, le=6)


class RecallResponse(StrictOutput):
    status: Literal["answered", "no_answer"]
    intent: Literal["exact", "relationship", "search"]
    query: str
    evidence: list[RecallEvidence] = Field(default_factory=list)
    citations: list[RecallCitation] = Field(default_factory=list)
    relationships: list[RecallRelationship] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class IndexParseError(StrictOutput):
    source_id: str
    path: str
    error: str


class SyncIndexResult(StrictOutput):
    snapshot: str
    documents: int = Field(ge=0)
    chunks: int = Field(ge=0)
    added: int = Field(ge=0)
    changed: int = Field(ge=0)
    unchanged: int = Field(ge=0)
    removed: int = Field(ge=0)
    parse_errors: list[IndexParseError] = Field(default_factory=list)
    elapsed_ms: float = Field(ge=0.0)


class SyncResponse(StrictOutput):
    ok: bool
    index: SyncIndexResult


class CanonicalMemoryStatus(StrictOutput):
    source_id: str
    path: str
    available: bool
    writable: bool
    authority: Literal["canonical-markdown"]


class IndexStatus(StrictOutput):
    available: bool
    path: str | None = None
    schema_version: int | None = None
    built_at: str | None = None
    memory_root: str | None = None
    memory_sources: list[str] = Field(default_factory=list)
    semantic_dimensions: int | None = None
    documents: int | None = Field(default=None, ge=0)
    chunks: int | None = Field(default=None, ge=0)


class GraphifyRuntimeStatus(StrictOutput):
    consistent: bool
    expected: str
    package: str | None = None
    cli: str | None = None
    python: str
    mcp_executable: str
    scripts_dir: str
    errors: list[str] = Field(default_factory=list)


class GraphifyStatus(StrictOutput):
    available: bool
    path: str
    nodes: int = Field(ge=0)
    edges: int = Field(ge=0)
    modified_ns: int | None = None
    age_seconds: float | None = Field(default=None, ge=0.0)
    provider_role: Literal["internal-graph-signal"]
    runtime: GraphifyRuntimeStatus


class RuntimeStatus(StrictOutput):
    python: str
    mcp: str
    mcp_supported: bool


class StatusResponse(StrictOutput):
    ok: bool
    canonical_memory_root: CanonicalMemoryStatus
    retrieval_sources: list[CanonicalMemoryStatus] = Field(default_factory=list)
    index: IndexStatus
    graphify: GraphifyStatus
    runtime: RuntimeStatus
    checked_at: str
