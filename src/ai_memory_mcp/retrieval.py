from __future__ import annotations

import copy
import re
import sqlite3
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifacts.models import ArtifactSearchHit

from .config import Settings
from .embedding import EmbeddingProvider, EmbeddingUnavailable, resolve_provider
from .graphify import GraphifyAdapter
from .generation import current_graph_path
from .index import MemoryIndex, scope_sql
from .models import EvidencePacket, ScopeFilter, SearchHit
from .query_log import query_stage
from .text import (
    cosine_sparse,
    fts_expressions,
    query_identifiers,
    query_windows,
    tokenize,
)

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


SEMANTIC_COVERAGE_MIN = 0.20
SEMANTIC_MARGIN_MIN = 0.35
SCOPED_DISTRIBUTED_COVERAGE_MIN = 0.50
FRESHNESS_CAP = 0.03
FRESHNESS_HALF_LIFE_DAYS = 180.0
REVIEW_OVERDUE_PENALTY = 0.03
RAW_FRESHNESS_CAP = 0.03
RAW_CHAT_HALF_LIFE_DAYS = 30.0
RAW_MEETING_HALF_LIFE_DAYS = 90.0
RAW_ARTIFACT_WARNING = (
    "Raw artifact evidence is a lead. Verify the source context or distill it "
    "before use as durable memory."
)


def _quoted_phrases(query: str) -> list[str]:
    return [
        match.group(1).strip()
        for match in re.finditer(r'"([^"\r\n]{1,2000})"', query)
        if match.group(1).strip()
    ]


def _raw_exact_reason(query: str, hit: ArtifactSearchHit) -> str | None:
    candidate = query.strip().strip('"').casefold()
    identifiers = {
        hit.external_id.casefold(),
        hit.artifact_id.casefold(),
        hit.artifact_uri.casefold(),
    }
    if candidate and (
        candidate in identifiers
        or candidate == hit.matched_identity.casefold()
    ):
        return "exact identifier"
    searchable = f"{hit.title}\n{hit.text}".casefold()
    if any(phrase.casefold() in searchable for phrase in _quoted_phrases(query)):
        return "exact phrase"
    return None


def merge_artifact_evidence(
    query: str,
    markdown_packet: EvidencePacket | None,
    artifact_hits: list[ArtifactSearchHit],
    *,
    settings: Settings,
    now: datetime,
    limit: int,
) -> EvidencePacket:
    """Fuse distilled and raw rankings while preserving each answer gate."""
    if not artifact_hits and markdown_packet is not None:
        return markdown_packet

    fusion_started = time.perf_counter()
    combined: dict[str, SearchHit] = {}
    raw_by_uri: dict[str, ArtifactSearchHit] = {}
    if markdown_packet is not None:
        for rank, original in enumerate(markdown_packet.results, start=1):
            hit = copy.deepcopy(original)
            # Markdown has already been reranked and answer-checked. Preserve
            # its final scores and ordering. Replacing scores with a single RRF
            # vote lets two weak raw producers bury an answer-checked passage.
            hit.ranks = {"distilled": rank}
            combined[hit.memory_id] = hit

    producer_ranks = {"artifact-fts": 0, "artifact-vector": 0}
    for raw in artifact_hits:
        ranking = (
            "artifact-fts"
            if raw.evidence_class == "raw"
            else "artifact-vector"
        )
        # Each producer owns its RRF rank sequence. A shared sequence makes
        # later producer lists weaker only because the caller concatenated them.
        producer_ranks[ranking] += 1
        rank = producer_ranks[ranking]
        score = 1.0 / (settings.rrf_k + rank)
        hit = SearchHit(
            memory_id=raw.artifact_id,
            source_id=f"artifact-{raw.source}",
            path=raw.artifact_uri,
            title=raw.title,
            heading=raw.entity,
            text=raw.text,
            score=score,
            ranks={ranking: rank},
            signals={ranking: raw.score},
            reasons=[],
            evidence_class=raw.evidence_class,
            artifact_uri=raw.artifact_uri,
            source_label=raw.source,
            source_instance=raw.source_instance,
            occurred_at=(
                raw.occurred_at.isoformat() if raw.occurred_at is not None else None
            ),
            artifact_kind=raw.entity,
            external_id=raw.external_id,
            segment_id=raw.segment_id,
            segment_start=raw.segment_start,
            segment_end=raw.segment_end,
            meeting_artifact_uri=raw.meeting_artifact_uri,
            continuation=raw.continuation,
        )
        existing = combined.get(raw.artifact_uri)
        prior_raw = raw_by_uri.get(raw.artifact_uri)
        if prior_raw is None or raw.score > prior_raw.score or (
            prior_raw.evidence_class != "raw" and raw.evidence_class == "raw"
        ):
            raw_by_uri[raw.artifact_uri] = raw
        if existing is None:
            combined[raw.artifact_uri] = hit
            continue
        # A burst can start at the same artifact as a raw FTS hit. Preserve
        # the raw record because only it carries the external ID answer gate.
        winner, secondary = (
            (existing, hit)
            if existing.evidence_class == "raw" or hit.evidence_class != "raw"
            else (hit, existing)
        )
        producer = next(iter(secondary.ranks))
        if producer not in winner.ranks:
            winner.ranks.update(secondary.ranks)
            winner.signals.update(secondary.signals)
            # One source gets at most one vote from each producer.
            winner.score += secondary.score
        elif secondary.signals.get(producer, 0.0) > winner.signals.get(
            producer, 0.0
        ):
            winner.signals[producer] = secondary.signals[producer]
        if raw.score >= (prior_raw.score if prior_raw is not None else -1.0):
            winner.text = hit.text
            winner.segment_id = hit.segment_id
            winner.segment_start = hit.segment_start
            winner.segment_end = hit.segment_end
            winner.meeting_artifact_uri = hit.meeting_artifact_uri
            winner.continuation = hit.continuation
        combined[raw.artifact_uri] = winner

    artifact_fusion_ms = round(
        (time.perf_counter() - fusion_started) * 1000,
        3,
    )
    rerank_started = time.perf_counter()
    raw_candidates = list(combined[uri] for uri in raw_by_uri)
    for hit in raw_candidates:
        # Feed raw text the same bounded relevance signals as Markdown. Keep
        # producer ranks for diagnostics; aliases are only reranker inputs.
        if "artifact-fts" in hit.ranks:
            hit.ranks["lexical"] = hit.ranks["artifact-fts"]
        if "artifact-vector" in hit.ranks:
            hit.ranks["semantic"] = hit.ranks["artifact-vector"]
            hit.signals["semantic"] = hit.signals.get("artifact-vector", 0.0)
    RetrievalEngine._rerank_hits(query, raw_candidates, len(raw_candidates), now)
    for hit in raw_candidates:
        hit.ranks.pop("lexical", None)
        hit.ranks.pop("semantic", None)
    for uri, raw in raw_by_uri.items():
        hit = combined[uri]
        reason = _raw_exact_reason(query, raw)
        if reason is not None:
            hit.score += 0.20
            hit.reasons.append(reason)
    ranked = sorted(
        combined.values(),
        key=lambda hit: (
            hit.score,
            hit.evidence_class == "distilled",
            hit.title.casefold(),
        ),
        reverse=True,
    )[:limit]
    top = ranked[0] if ranked else None
    if top is None:
        answer_status = "no_answer"
    elif top.evidence_class == "distilled":
        answer_status = (
            markdown_packet.answer_status
            if markdown_packet is not None
            else "no_answer"
        )
    else:
        answer_status = (
            "answered"
            if any(
                reason in {"exact identifier", "exact phrase"}
                for reason in top.reasons
            )
            else "no_answer"
        )

    plan = (
        copy.deepcopy(markdown_packet.plan)
        if markdown_packet is not None
        else {
            "scope": {},
            "retrievers": [],
            "fusion": f"RRF(k={settings.rrf_k})",
            "rerank": True,
            "context_expansion": False,
        }
    )
    retrievers = list(plan.get("retrievers", []))
    if "artifact-fts" not in retrievers:
        retrievers.append("artifact-fts")
    if (
        any(hit.evidence_class == "burst" for hit in artifact_hits)
        and "artifact-vector" not in retrievers
    ):
        retrievers.append("artifact-vector")
    plan["retrievers"] = retrievers
    diagnostics = (
        copy.deepcopy(markdown_packet.diagnostics)
        if markdown_packet is not None
        else {
            "candidate_counts": {},
            "provider_latency_ms": {},
            "route": "artifact-only",
        }
    )
    diagnostics.setdefault("candidate_counts", {})["artifact"] = len(
        artifact_hits
    )
    diagnostics.setdefault("provider_latency_ms", {}).update(
        {
            "artifact_fusion": artifact_fusion_ms,
            "artifact_rerank": round(
                (time.perf_counter() - rerank_started) * 1000,
                3,
            ),
        }
    )
    return EvidencePacket(
        query=query,
        answer_status=answer_status,
        results=ranked,
        plan=plan,
        diagnostics=diagnostics,
    )


def _parse_utc(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _lexical_score(bm25: float) -> float:
    """Convert an FTS5 bm25() value into a bounded, higher-is-better score.

    bm25() returns negative values where a more negative value is a better
    match. Clamping the raw value at zero first (the previous behaviour)
    collapsed every hit to exactly 1.0 and destroyed the lexical signal.
    """
    relevance = max(0.0, -bm25)
    return relevance / (1.0 + relevance)


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


def _distributed_query_coverage(
    query: str,
    hits: list[SearchHit],
) -> float:
    content_query = set(tokenize(query)) - STOPWORDS
    if not content_query:
        return 0.0
    matched: set[str] = set()
    for hit in hits:
        searchable = (
            f"{hit.memory_id} {hit.path} {hit.title} {hit.heading} {hit.text}"
        )
        matched.update(content_query & set(tokenize(searchable)))
    return len(matched) / len(content_query)


def _row_hit(row: sqlite3.Row, score: float, source: str, rank: int) -> SearchHit:
    return SearchHit(
        memory_id=row["memory_id"],
        source_id=row["source_id"],
        path=row["path"],
        title=row["title"],
        heading=row["heading"],
        text=row["text"],
        score=score,
        updated=row["updated"],
        review_after=row["review_after"],
        ranks={source: rank},
        signals={source: score},
        segment_id=str(row["chunk_id"]),
        segment_start=0,
        segment_end=len(str(row["text"])),
    )


class RetrievalEngine:
    def __init__(
        self,
        settings: Settings,
        *,
        index_path: Path | None = None,
        graph_path: Path | None = None,
        generation_id: str | None = None,
    ):
        self.settings = settings
        self.generation_id = generation_id
        self.now = lambda: datetime.now(timezone.utc)
        self.index = MemoryIndex(settings, path=index_path)
        metadata = self.index.metadata()
        self.provider: EmbeddingProvider | None
        try:
            self.provider = resolve_provider(
                metadata.get("embedding_provider", "hashed"),
                model=metadata.get("embedding_model", ""),
                dimensions=int(
                    metadata.get(
                        "semantic_dimensions",
                        settings.semantic_dimensions,
                    )
                ),
            )
            self.provider_warning = ""
        except EmbeddingUnavailable as exc:
            # Lexical and graph retrieval still work; semantic is disabled
            # until the recorded provider is installed or the index rebuilt.
            self.provider = None
            self.provider_warning = (
                f"Semantic retrieval disabled: {exc}. "
                "Install the provider or run memory_sync to rebuild."
            )
        self.graph = GraphifyAdapter(
            graph_path or current_graph_path(settings),
            primary_source_id=settings.primary_source_id,
            source_ids=tuple(
                source.source_id for source in settings.retrieval_sources
            ),
        )

    def _plan(self, query: str, supplied: ScopeFilter | None) -> ScopeFilter:
        # Prose identifiers remain retrieval signals. Only explicit arguments
        # can narrow scope because many older notes have incomplete metadata.
        return copy.deepcopy(supplied) if supplied is not None else ScopeFilter()

    @query_stage("markdown_lexical")
    def _lexical(
        self, query: str, scope: ScopeFilter, limit: int
    ) -> list[SearchHit]:
        where, parameters = scope_sql(scope)
        scope_clause = where.replace("WHERE", "AND", 1) if where else ""
        by_chunk: dict[str, sqlite3.Row] = {}
        with self.index.connection() as connection:
            for expression in fts_expressions(query):
                rows = connection.execute(
                    f"""
                WITH candidates AS (
                    SELECT c.*, d.identifiers_json, d.updated, d.review_after,
                           bm25(
                               chunks_fts, 0.0, 4.0, 2.0, 1.0, 7.0
                           ) AS lexical_score
                    FROM chunks_fts f
                    JOIN chunks c ON c.chunk_id = f.chunk_id
                    JOIN documents d USING(memory_id)
                    WHERE chunks_fts MATCH ?
                    {scope_clause}
                ), ranked AS (
                    SELECT *, row_number() OVER (
                        PARTITION BY memory_id
                        ORDER BY lexical_score, ordinal, chunk_id
                    ) AS document_rank
                    FROM candidates
                )
                SELECT * FROM ranked
                WHERE document_rank = 1
                ORDER BY lexical_score
                LIMIT ?
                """,
                    [expression, *parameters, limit],
                ).fetchall()
                for row in rows:
                    key = str(row["chunk_id"])
                    prior = by_chunk.get(key)
                    if prior is None or float(row["lexical_score"]) < float(
                        prior["lexical_score"]
                    ):
                        by_chunk[key] = row
        rows = sorted(
            by_chunk.values(),
            key=lambda row: (float(row["lexical_score"]), str(row["chunk_id"])),
        )[:limit]
        return [
            _row_hit(row, _lexical_score(row["lexical_score"]), "lexical", rank)
            for rank, row in enumerate(rows, 1)
        ]

    @query_stage("markdown_semantic")
    def _semantic(
        self,
        query: str,
        scope: ScopeFilter,
        limit: int,
        *,
        details: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        if self.provider is None:
            return []
        windows = query_windows(query)
        all_candidates: dict[str, tuple[Any, dict[int, float], float]] = {}
        backends: list[str] = []
        for window in windows:
            vector = self.provider.embed(window)
            candidates, backend = self.index.vector_candidates(scope, vector, limit)
            backends.append(backend)
            for row, candidate in candidates:
                score = cosine_sparse(vector, candidate)
                key = str(row["chunk_id"])
                prior = all_candidates.get(key)
                if prior is None or score > prior[2]:
                    all_candidates[key] = (row, candidate, score)
        if details is not None:
            details["backend"] = "+".join(dict.fromkeys(backends))
            details["candidates"] = len(all_candidates)
            details["query_windows"] = len(windows)
            details["budget_exhausted"] = any(
                "budget-exhausted" in backend for backend in backends
            )
        by_memory: dict[str, tuple[float, Any]] = {}
        for row, _candidate, score in all_candidates.values():
            memory_id = str(row["memory_id"])
            current = by_memory.get(memory_id)
            if current is None or score > current[0]:
                by_memory[memory_id] = (score, row)
        scored = list(by_memory.values())
        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            _row_hit(row, score, "semantic", rank)
            for rank, (score, row) in enumerate(scored[:limit], 1)
            if score > 0
        ]

    @query_stage("graph")
    def _graph(
        self,
        query: str,
        scope: ScopeFilter,
        seeds: list[SearchHit],
        limit: int,
    ) -> list[SearchHit]:
        allowed_paths = self._scoped_paths(scope)
        seed_scores: dict[str, float] = defaultdict(float)
        canonical_paths: dict[str, str] = {}
        for seed in seeds:
            key = seed.path.casefold()
            canonical_paths.setdefault(key, seed.path)
            for rank in seed.ranks.values():
                seed_scores[key] += 1.0 / (self.settings.rrf_k + rank)
        ranked_seeds = [
            canonical_paths[key]
            for key in sorted(
                seed_scores,
                key=lambda item: (-seed_scores[item], item),
            )[:20]
        ]
        ranked_paths = self.graph.rank(
            query,
            ranked_seeds,
            limit=limit,
            allowed_paths=allowed_paths,
            max_depth=self.settings.graph_depth,
        )
        if not ranked_paths:
            return []
        where, parameters = scope_sql(scope)
        scope_clause = where.replace("WHERE", "AND", 1) if where else ""
        placeholders = ", ".join("?" for _ in ranked_paths)
        with self.index.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT c.*, d.updated, d.review_after FROM documents d
                JOIN chunks c USING(memory_id)
                WHERE d.path IN ({placeholders})
                AND c.ordinal = 0
                {scope_clause}
                """,
                [*ranked_paths, *parameters],
            ).fetchall()
        rows_by_path = {
            str(row["path"]).casefold(): row
            for row in rows
        }
        results: list[SearchHit] = []
        for graph_rank, path in enumerate(ranked_paths, 1):
            row = rows_by_path.get(path.casefold())
            if row:
                results.append(
                    _row_hit(row, 1.0 / graph_rank, "graph", graph_rank)
                )
        return results

    def _scoped_paths(self, scope: ScopeFilter) -> set[str]:
        where, parameters = scope_sql(scope)
        with self.index.connection() as connection:
            return {
                str(row[0])
                for row in connection.execute(
                    f"SELECT d.path FROM documents d {where}",
                    parameters,
                )
            }

    @staticmethod
    def _declared_target_keys(value: object) -> set[str]:
        target = str(value or "").strip()
        if target.startswith("[[") and target.endswith("]]"):
            target = target[2:-2]
        destination, separator, label = target.partition("|")
        destination = destination.strip().replace("\\", "/")
        keys = {
            destination.casefold(),
            destination.rsplit("/", 1)[-1].removesuffix(".md").casefold(),
        }
        if separator and label.strip():
            keys.add(label.strip().casefold())
        return {key for key in keys if key}

    def _fuse(
        self,
        rankings: dict[str, list[SearchHit]],
    ) -> list[SearchHit]:
        by_memory: dict[str, SearchHit] = {}
        passages: dict[str, dict[str, tuple[float, SearchHit, set[str]]]] = {}
        for source, hits in rankings.items():
            for rank, hit in enumerate(hits, 1):
                if source != "graph":
                    key = hit.segment_id or f"{hit.heading}\n{hit.text}"
                    choices = passages.setdefault(hit.memory_id, {})
                    score, passage, producers = choices.get(key, (0.0, hit, set()))
                    if source not in producers:
                        producers.add(source)
                        choices[key] = (
                            score + 1.0 / (self.settings.rrf_k + rank),
                            passage,
                            producers,
                        )
                fused = by_memory.get(hit.memory_id)
                if fused is None:
                    fused = SearchHit(
                        memory_id=hit.memory_id,
                        source_id=hit.source_id,
                        path=hit.path,
                        title=hit.title,
                        heading=hit.heading,
                        text=hit.text,
                        score=0.0,
                        updated=hit.updated,
                        review_after=hit.review_after,
                        segment_id=hit.segment_id,
                        segment_start=hit.segment_start,
                        segment_end=hit.segment_end,
                    )
                    by_memory[hit.memory_id] = fused
                # A long note can yield many matching sections. Each retriever
                # gets one RRF vote per memory so document length cannot swamp
                # several independent sources that agree on a shorter note.
                if source in fused.ranks:
                    fused.signals[source] = max(
                        fused.signals.get(source, 0.0), hit.score
                    )
                    continue
                fused.score += 1.0 / (self.settings.rrf_k + rank)
                fused.ranks[source] = rank
                fused.signals[source] = hit.score
        for memory_id, choices in passages.items():
            # Passage votes use ranks, like document fusion. Graph-only prefix
            # scores must never compete with textual similarity scores.
            _score, passage, _producers = max(choices.values(), key=lambda item: item[0])
            fused = by_memory[memory_id]
            fused.heading = passage.heading
            fused.text = passage.text
            fused.segment_id = passage.segment_id
            fused.segment_start = passage.segment_start
            fused.segment_end = passage.segment_end
        return list(by_memory.values())

    def _rerank(
        self,
        query: str,
        hits: list[SearchHit],
        limit: int,
    ) -> list[SearchHit]:
        return self._rerank_hits(query, hits, limit, self.now())

    @staticmethod
    def _rerank_hits(query: str, hits: list[SearchHit], limit: int, now: datetime) -> list[SearchHit]:
        query_casefold = query.casefold()
        query_tokens = set(tokenize(query))
        intent_expansions = _intent_expansions(query_tokens)
        identifiers = [value.casefold() for value in query_identifiers(query)]
        for hit in hits:
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
            # Freshness is a tiebreaker, not a relevance signal: the cap sits
            # below every exact-match bonus so it only reorders near-equals.
            updated_at = _parse_utc(hit.updated)
            if updated_at is not None:
                age_days = max(
                    0.0, (now - updated_at).total_seconds() / 86400.0
                )
                freshness = FRESHNESS_CAP * 0.5 ** (
                    age_days / FRESHNESS_HALF_LIFE_DAYS
                )
                hit.score += freshness
                hit.signals["freshness"] = freshness
                if freshness > 0.02:
                    hit.reasons.append("recently updated")
            review_at = _parse_utc(hit.review_after)
            if review_at is not None and review_at < now:
                hit.score -= REVIEW_OVERDUE_PENALTY
                hit.reasons.append("review overdue")
        ranked = sorted(
            hits,
            key=lambda hit: (hit.score, -min(hit.ranks.values()), hit.title.casefold()),
            reverse=True,
        )
        return ranked[:limit]

    def _expand_context(
        self,
        hits: list[SearchHit],
        scope: ScopeFilter,
    ) -> None:
        # One query avoids a database round trip for each fused result.
        chunks_by_memory = self.index.chunks_for_memories(
            [hit.memory_id for hit in hits]
        )
        allowed_paths = self._scoped_paths(scope)
        for hit in hits:
            chunks = chunks_by_memory.get(hit.memory_id, [])
            selected = next(
                (
                    chunk
                    for chunk in chunks
                    if str(chunk["chunk_id"]) == hit.segment_id
                ),
                chunks[0] if chunks else None,
            )
            if not selected:
                continue
            relevant = [
                chunk
                for chunk in chunks
                if abs(int(chunk["ordinal"]) - int(selected["ordinal"])) <= 1
            ]
            winning = f"## {selected['heading']}\n{selected['text']}".strip()
            neighbors = [
                f"## {chunk['heading']}\n{chunk['text']}".strip()
                for chunk in relevant
                if str(chunk["chunk_id"]) != str(selected["chunk_id"])
            ]
            expanded = winning
            for neighbor in neighbors:
                remaining = self.settings.context_max_characters - len(expanded) - 2
                if remaining <= 0:
                    break
                expanded += "\n\n" + neighbor[:remaining]
            hit.text = expanded[: self.settings.context_max_characters]
            hit.continuation = any(
                len(neighbor) > max(0, self.settings.context_max_characters - len(winning))
                for neighbor in neighbors
            )
            hit.graph_neighbors = [
                str(item.get("path") or item.get("label"))
                for item in self.graph.neighbors(
                    hit.path,
                    depth=self.settings.graph_depth,
                    limit=6,
                    allowed_paths=allowed_paths,
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
        provider_latency_ms: dict[str, float] = {}
        provider_started = time.perf_counter()
        lexical = self._lexical(query, planned_scope, candidate_limit)
        provider_latency_ms["lexical"] = round(
            (time.perf_counter() - provider_started) * 1000,
            3,
        )
        provider_started = time.perf_counter()
        semantic_details: dict[str, Any] = {}
        semantic = self._semantic(
            query,
            planned_scope,
            candidate_limit,
            details=semantic_details,
        )
        provider_latency_ms["semantic"] = round(
            (time.perf_counter() - provider_started) * 1000,
            3,
        )
        provider_started = time.perf_counter()
        graph = self._graph(
            query, planned_scope, lexical[:20] + semantic[:20], candidate_limit
        )
        provider_latency_ms["graphify"] = round(
            (time.perf_counter() - provider_started) * 1000,
            3,
        )
        provider_started = time.perf_counter()
        fused = self._fuse(
            {"lexical": lexical, "semantic": semantic, "graph": graph},
        )
        provider_latency_ms["fusion"] = round(
            (time.perf_counter() - provider_started) * 1000,
            3,
        )
        provider_started = time.perf_counter()
        ranked_pool = self._rerank(query, fused, max(requested_limit, 8))
        hits = ranked_pool[:requested_limit]
        provider_latency_ms["rerank"] = round(
            (time.perf_counter() - provider_started) * 1000,
            3,
        )
        provider_started = time.perf_counter()
        self._expand_context(hits, planned_scope)
        provider_latency_ms["context"] = round(
            (time.perf_counter() - provider_started) * 1000,
            3,
        )
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
        # A paraphrase answer shares few query tokens, so query_coverage alone
        # rejects it. A semantic margin alone is also unsafe: on a small corpus
        # one note leads the ranking even when nothing answers the question.
        # Requiring a lexical anchor AND a clear semantic lead separates a real
        # paraphrase match from a confident-looking miss.
        top_semantic = top.signals.get("semantic", 0.0) if top else 0.0
        runner_up_semantic = max(
            (hit.signals.get("semantic", 0.0) for hit in ranked_pool[1:]),
            default=0.0,
        )
        semantic_margin = (
            (top_semantic - runner_up_semantic) / top_semantic
            if top_semantic > 0
            else 0.0
        )
        semantic_evidence = bool(
            top
            and semantic_margin >= SEMANTIC_MARGIN_MIN
            and top.signals.get("query_coverage", 0.0) >= SEMANTIC_COVERAGE_MIN
        )
        corroborating_hits = [
            hit
            for hit in hits[:3]
            if "lexical" in hit.ranks and "semantic" in hit.ranks
        ]
        narrow_scope = bool(
            planned_scope.repository
            or planned_scope.project
            or planned_scope.ticket
            or planned_scope.path_prefix
        )
        # A narrow query can require facts from multiple notes. Aggregate only
        # independently corroborated hits so broad corpus noise cannot answer it.
        distributed_evidence = bool(
            narrow_scope
            and len(corroborating_hits) >= 2
            and top_score >= 0.08
            and _distributed_query_coverage(query, corroborating_hits)
            >= SCOPED_DISTRIBUTED_COVERAGE_MIN
        )
        answered = bool(top) and (
            exact_evidence
            or intent_evidence
            or semantic_evidence
            or distributed_evidence
            or (
                corroborated_text
                and top_score >= 0.045
                and top.signals.get("query_coverage", 0.0) >= 0.34
            )
        )
        return EvidencePacket(
            query=query,
            answer_status="answered" if answered else "no_answer",
            results=hits,
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
                "provider_latency_ms": provider_latency_ms,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "index_snapshot": self.index.path.name,
                "generation_id": self.generation_id,
                "semantic_search": semantic_details,
                "graphify": self.graph.health(),
            },
        )

    def get(
        self,
        identity: str,
        scope: ScopeFilter | None = None,
    ) -> dict[str, Any]:
        document = self.index.document(identity, scope)
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

    def neighbors(
        self,
        identity: str,
        depth: int = 1,
        scope: ScopeFilter | None = None,
    ) -> dict[str, Any]:
        document = self.index.document(identity, scope)
        if not document:
            return {"found": False, "identity": identity, "neighbors": []}
        selected_scope = scope or ScopeFilter(status="")
        scoped_identities = self.index.scoped_identities(selected_scope)
        return {
            "found": True,
            "memory": {
                "memory_id": document["memory_id"],
                "path": document["path"],
                "title": document["title"],
            },
            "declared_related": [
                value
                for value in document["related"]
                if self._declared_target_keys(value) & scoped_identities
            ],
            "graph_neighbors": self.graph.neighbors(
                str(document["path"]),
                depth=max(1, min(depth, 4)),
                allowed_paths=self._scoped_paths(selected_scope),
            ),
        }

    def path(
        self,
        source: str,
        target: str,
        scope: ScopeFilter | None = None,
    ) -> dict[str, Any]:
        left = self.index.document(source, scope)
        right = self.index.document(target, scope)
        if not left or not right:
            return {
                "found": False,
                "missing": [
                    name
                    for name, value in ((source, left), (target, right))
                    if value is None
                ],
            }
        selected_scope = scope or ScopeFilter(status="")
        chain = self.graph.path(
            str(left["path"]),
            str(right["path"]),
            allowed_paths=self._scoped_paths(selected_scope),
        )
        return {
            "found": bool(chain),
            "source": {"memory_id": left["memory_id"], "path": left["path"]},
            "target": {"memory_id": right["memory_id"], "path": right["path"]},
            "path": chain,
        }

    def mentioned_documents(
        self,
        query: str,
        scope: ScopeFilter,
        *,
        limit: int = 3,
    ) -> list[dict[str, object]]:
        return self.index.mentioned_documents(query, scope, limit=limit)
