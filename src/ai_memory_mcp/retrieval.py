from __future__ import annotations

import re
import sqlite3
import time
from collections import defaultdict
from dataclasses import asdict
from typing import Any

from .config import Settings
from .graphify import GraphifyAdapter
from .index import MemoryIndex, scope_sql
from .models import EvidencePacket, ScopeFilter, SearchHit
from .text import cosine_sparse, query_identifiers, semantic_vector, tokenize

TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,12}-\d+\b", re.IGNORECASE)
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "be",
    "do",
    "does",
    "for",
    "how",
    "in",
    "is",
    "of",
    "on",
    "the",
    "to",
    "what",
    "when",
    "which",
    "why",
    "with",
}


def _fts_expression(query: str) -> str:
    terms = list(dict.fromkeys(tokenize(query)))
    if not terms:
        return '""'
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms[:24])


def _intent_expansions(tokens: set[str]) -> set[str]:
    expansions: set[str] = set()
    if tokens & {"start", "starts", "started", "starting", "launch", "launched"}:
        expansions.update({"startup", "launch"})
    if tokens & {"background", "hidden", "terminal", "window", "windowless"} and tokens & {
        "background",
        "hidden",
        "no",
        "not",
        "terminal",
        "without",
        "windowless",
    }:
        expansions.update({"background", "hidden", "windowless"})
    return expansions - tokens


def _row_hit(row: sqlite3.Row, score: float, source: str, rank: int) -> SearchHit:
    return SearchHit(
        memory_id=row["memory_id"],
        path=row["path"],
        title=row["title"],
        heading=row["heading"],
        text=row["text"],
        score=score,
        ranks={source: rank},
        signals={source: score},
    )


class RetrievalEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.index = MemoryIndex(settings)
        self.graph = GraphifyAdapter(settings.graph_path)

    def _plan(self, query: str, supplied: ScopeFilter | None) -> ScopeFilter:
        scope = supplied or ScopeFilter()
        ticket = TICKET_RE.search(query)
        if ticket and not scope.ticket:
            scope.ticket = ticket.group(0).upper()
        return scope

    def _lexical(
        self, query: str, scope: ScopeFilter, limit: int
    ) -> list[SearchHit]:
        where, parameters = scope_sql(scope)
        scope_clause = where.replace("WHERE", "AND", 1) if where else ""
        with self.index.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT c.*, d.identifiers_json, bm25(
                    chunks_fts, 0.0, 4.0, 2.0, 1.0, 7.0
                ) AS lexical_score
                FROM chunks_fts f
                JOIN chunks c ON c.chunk_id = f.chunk_id
                JOIN documents d USING(memory_id)
                WHERE chunks_fts MATCH ?
                {scope_clause}
                ORDER BY lexical_score
                LIMIT ?
                """,
                [_fts_expression(query), *parameters, limit],
            ).fetchall()
        return [
            _row_hit(row, 1.0 / (1.0 + max(0.0, row["lexical_score"])), "lexical", rank)
            for rank, row in enumerate(rows, 1)
        ]

    def _semantic(
        self, query: str, scope: ScopeFilter, limit: int
    ) -> list[SearchHit]:
        vector = semantic_vector(query, self.settings.semantic_dimensions)
        scored = [
            (cosine_sparse(vector, candidate), row)
            for row, candidate in self.index.all_vectors(scope)
        ]
        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            _row_hit(row, score, "semantic", rank)
            for rank, (score, row) in enumerate(scored[:limit], 1)
            if score > 0
        ]

    def _graph(
        self,
        query: str,
        scope: ScopeFilter,
        seeds: list[SearchHit],
        limit: int,
    ) -> list[SearchHit]:
        ranked_paths = self.graph.rank(
            query, [seed.path for seed in seeds[:20]], limit=limit
        )
        if not ranked_paths:
            return []
        where, parameters = scope_sql(scope)
        scope_clause = where.replace("WHERE", "AND", 1) if where else ""
        results: list[SearchHit] = []
        with self.index.connection() as connection:
            for graph_rank, path in enumerate(ranked_paths, 1):
                row = connection.execute(
                    f"""
                    SELECT c.* FROM chunks c
                    JOIN documents d USING(memory_id)
                    WHERE lower(c.path) = lower(?)
                    {scope_clause}
                    ORDER BY c.ordinal LIMIT 1
                    """,
                    [path, *parameters],
                ).fetchone()
                if row:
                    results.append(
                        _row_hit(row, 1.0 / graph_rank, "graph", graph_rank)
                    )
        return results

    def _fuse(
        self,
        query: str,
        rankings: dict[str, list[SearchHit]],
        limit: int,
    ) -> list[SearchHit]:
        by_memory: dict[str, SearchHit] = {}
        query_casefold = query.casefold()
        query_tokens = set(tokenize(query))
        intent_expansions = _intent_expansions(query_tokens)
        identifiers = [value.casefold() for value in query_identifiers(query)]
        for source, hits in rankings.items():
            for rank, hit in enumerate(hits, 1):
                fused = by_memory.get(hit.memory_id)
                if fused is None:
                    fused = SearchHit(
                        memory_id=hit.memory_id,
                        path=hit.path,
                        title=hit.title,
                        heading=hit.heading,
                        text=hit.text,
                        score=0.0,
                    )
                    by_memory[hit.memory_id] = fused
                # A long note can yield many matching sections. Each retriever
                # gets one RRF vote per memory so document length cannot swamp
                # several independent sources that agree on a shorter note.
                if source in fused.ranks:
                    current_text = f"{fused.title} {fused.heading} {fused.text}"
                    candidate_text = f"{hit.title} {hit.heading} {hit.text}"
                    current_overlap = len(query_tokens & set(tokenize(current_text)))
                    candidate_overlap = len(query_tokens & set(tokenize(candidate_text)))
                    if candidate_overlap > current_overlap:
                        fused.heading = hit.heading
                        fused.text = hit.text
                    fused.signals[source] = max(
                        fused.signals.get(source, 0.0), hit.score
                    )
                    continue
                fused.score += 1.0 / (self.settings.rrf_k + rank)
                fused.ranks[source] = rank
                fused.signals[source] = hit.score
        for hit in by_memory.values():
            searchable = f"{hit.memory_id} {hit.path} {hit.title} {hit.heading} {hit.text}".casefold()
            title = hit.title.casefold()
            matched_ids = [identifier for identifier in identifiers if identifier in searchable]
            if matched_ids:
                hit.score += 0.12
                hit.reasons.append(f"exact identifier: {', '.join(matched_ids)}")
                if any(identifier in title or identifier in hit.path.casefold() for identifier in matched_ids):
                    hit.score += 0.08
                    hit.reasons.append("identifier in title or path")
            if len(query_casefold) >= 4 and query_casefold in searchable:
                hit.score += 0.08
                hit.reasons.append("exact phrase")
            title_overlap = len(query_tokens & set(tokenize(title)))
            if title_overlap:
                hit.score += min(0.06, title_overlap * 0.015)
                hit.reasons.append("title match")
            if "lexical" in hit.ranks and "semantic" in hit.ranks:
                hit.score += 0.025
                hit.reasons.append("lexical-semantic agreement")
            if "graph" in hit.ranks:
                hit.reasons.append("Graphify relationship signal")
            content_query = query_tokens - STOPWORDS
            content_overlap = len(content_query & set(tokenize(searchable)))
            hit.signals["query_coverage"] = (
                content_overlap / len(content_query) if content_query else 0.0
            )
            intent_title_overlap = len(
                intent_expansions & set(tokenize(hit.title))
            )
            hit.signals["intent_title_overlap"] = float(intent_title_overlap)
            # RRF remains the cross-retriever backbone; these bounded bonuses
            # let strong semantic agreement and broad query coverage separate
            # the best answer from graph-connected but textually weak notes.
            hit.score += min(0.05, hit.signals.get("semantic", 0.0) * 0.08)
            hit.score += min(0.06, hit.signals["query_coverage"] * 0.09)
            hit.score += min(0.08, intent_title_overlap * 0.04)
        ranked = sorted(
            by_memory.values(),
            key=lambda hit: (hit.score, -min(hit.ranks.values()), hit.title.casefold()),
            reverse=True,
        )
        return ranked[:limit]

    def _expand_context(self, hits: list[SearchHit]) -> None:
        for hit in hits:
            chunks = self.index.chunks_for_memory(hit.memory_id)
            selected = next(
                (chunk for chunk in chunks if chunk["heading"] == hit.heading),
                chunks[0] if chunks else None,
            )
            if not selected:
                continue
            relevant = [
                chunk
                for chunk in chunks
                if abs(int(chunk["ordinal"]) - int(selected["ordinal"])) <= 1
            ]
            hit.text = "\n\n".join(
                f"## {chunk['heading']}\n{chunk['text']}".strip()
                for chunk in relevant
            )[:5000]
            hit.graph_neighbors = [
                str(item.get("path") or item.get("label"))
                for item in self.graph.neighbors(
                    hit.path, depth=self.settings.graph_depth, limit=6
                )
                if item.get("path") or item.get("label")
            ]

    def search(
        self,
        query: str,
        *,
        scope: ScopeFilter | None = None,
        limit: int | None = None,
        explain: bool = True,
    ) -> EvidencePacket:
        started = time.perf_counter()
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        requested_limit = max(1, min(limit or self.settings.result_limit, 25))
        planned_scope = self._plan(query, scope)
        candidate_limit = max(requested_limit * 8, 40)
        lexical = self._lexical(query, planned_scope, candidate_limit)
        semantic = self._semantic(query, planned_scope, candidate_limit)
        graph = self._graph(
            query, planned_scope, lexical[:20] + semantic[:20], candidate_limit
        )
        hits = self._fuse(
            query,
            {"lexical": lexical, "semantic": semantic, "graph": graph},
            requested_limit,
        )
        self._expand_context(hits)
        # RRF-only weak matches cluster near 0.016. Require corroboration or a
        # strong exact-match bonus before claiming the corpus answered.
        top_score = hits[0].score if hits else 0.0
        top = hits[0] if hits else None
        corroborated_text = bool(
            top and "lexical" in top.ranks and "semantic" in top.ranks
        )
        exact_evidence = bool(
            top
            and any(
                reason.startswith(("exact identifier", "exact phrase"))
                for reason in top.reasons
            )
        )
        intent_evidence = bool(
            top
            and top.signals.get("intent_title_overlap", 0.0) >= 2
            and top.signals.get("semantic", 0.0) >= 0.4
        )
        answered = bool(top) and (
            exact_evidence
            or intent_evidence
            or (
                corroborated_text
                and top_score >= 0.045
                and top.signals.get("query_coverage", 0.0) >= 0.34
            )
        )
        return EvidencePacket(
            query=query,
            answer_status="answered" if answered else "no_answer",
            results=hits if answered else [],
            plan={
                "scope": asdict(planned_scope),
                "retrievers": ["lexical", "semantic", "graphify"],
                "fusion": f"RRF(k={self.settings.rrf_k})",
                "rerank": True,
                "context_expansion": True,
            },
            diagnostics={
                "candidate_counts": {
                    "lexical": len(lexical),
                    "semantic": len(semantic),
                    "graph": len(graph),
                },
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "index_snapshot": self.index.path.name,
                "graphify": self.graph.health(),
            },
        )

    def get(self, identity: str) -> dict[str, Any]:
        document = self.index.document(identity)
        if not document:
            return {"found": False, "identity": identity}
        return {
            "found": True,
            "memory": document,
            "citation": {
                "path": document["path"],
                "memory_id": document["memory_id"],
            },
        }

    def neighbors(self, identity: str, depth: int = 1) -> dict[str, Any]:
        document = self.index.document(identity)
        if not document:
            return {"found": False, "identity": identity, "neighbors": []}
        return {
            "found": True,
            "memory": {
                "memory_id": document["memory_id"],
                "path": document["path"],
                "title": document["title"],
            },
            "declared_related": document["related"],
            "graph_neighbors": self.graph.neighbors(
                str(document["path"]), depth=max(1, min(depth, 4))
            ),
        }

    def path(self, source: str, target: str) -> dict[str, Any]:
        left = self.index.document(source)
        right = self.index.document(target)
        if not left or not right:
            return {
                "found": False,
                "missing": [
                    name
                    for name, value in ((source, left), (target, right))
                    if value is None
                ],
            }
        chain = self.graph.path(str(left["path"]), str(right["path"]))
        return {
            "found": bool(chain),
            "source": {"memory_id": left["memory_id"], "path": left["path"]},
            "target": {"memory_id": right["memory_id"], "path": right["path"]},
            "path": chain,
        }
