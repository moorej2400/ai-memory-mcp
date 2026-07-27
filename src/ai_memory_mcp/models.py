from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class MemoryDocument:
    memory_id: str
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
    path: str
    title: str
    heading: str
    ordinal: int
    text: str
    vector: dict[int, float]


@dataclass(slots=True)
class ScopeFilter:
    root_scope: str | None = None
    repository: str | None = None
    project: str | None = None
    ticket: str | None = None
    status: str = "active"
    path_prefix: str | None = None


@dataclass(slots=True)
class SearchHit:
    memory_id: str
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
