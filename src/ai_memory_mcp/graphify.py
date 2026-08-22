from __future__ import annotations

import heapq
import json
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from .text import tokenize


class GraphifyAdapter:
    """Expose only provider-neutral graph operations to the retrieval engine."""

    def __init__(
        self,
        graph_path: Path,
        *,
        primary_source_id: str = "core",
        source_ids: tuple[str, ...] = (),
    ):
        self.graph_path = graph_path
        self.primary_source_id = primary_source_id
        self.source_ids = frozenset((primary_source_id, *source_ids))
        self._stamp: tuple[int, int] | None = None
        self._available = False
        self.metadata: dict[str, Any] = {}
        self.nodes: dict[str, dict[str, Any]] = {}
        self.adjacency: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        self.source_nodes: dict[str, set[str]] = defaultdict(set)

    def _source_path(self, value: object) -> str:
        path = str(value or "").replace("\\", "/").lstrip("./")
        if not path:
            return ""
        prefix = path.split("/", 1)[0].casefold()
        if prefix in self.source_ids:
            return path
        return f"{self.primary_source_id}/{path}"

    def _load(self) -> None:
        if not self.graph_path.exists():
            self.nodes = {}
            self.adjacency = defaultdict(list)
            self.source_nodes = defaultdict(set)
            self.metadata = {}
            self._stamp = None
            self._available = False
            return
        stat = self.graph_path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return
        payload = json.loads(self.graph_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("The graph snapshot must contain one JSON object.")
        metadata = payload.get("graph", {})
        self.metadata = metadata if isinstance(metadata, dict) else {}
        self.nodes = {str(node["id"]): node for node in payload.get("nodes", [])}
        self.adjacency = defaultdict(list)
        self.source_nodes = defaultdict(set)
        for node_id, node in self.nodes.items():
            canonical_source = self._source_path(node.get("source_file"))
            source = canonical_source.casefold()
            if source:
                node["source_file"] = canonical_source
                self.source_nodes[source].add(node_id)
        for edge in payload.get("links", payload.get("edges", [])):
            source = str(edge.get("source", ""))
            target = str(edge.get("target", ""))
            if source in self.nodes and target in self.nodes:
                self.adjacency[source].append((target, edge))
                self.adjacency[target].append((source, edge))
        self._stamp = stamp
        self._available = True

    def health(self) -> dict[str, Any]:
        self._load()
        return {
            "available": self._available,
            "path": str(self.graph_path),
            "nodes": len(self.nodes),
            "edges": sum(len(values) for values in self.adjacency.values()) // 2,
            "modified_ns": self._stamp[0] if self._stamp else None,
            "build_mode": self.metadata.get("build_mode"),
            "index_snapshot": self.metadata.get("index_snapshot"),
        }

    @staticmethod
    def _path_key(value: object) -> str:
        return str(value or "").replace("\\", "/").casefold()

    def _node_allowed(
        self,
        node_id: str,
        allowed_paths: frozenset[str] | None,
    ) -> bool:
        if allowed_paths is None:
            return True
        source = self._path_key(self.nodes[node_id].get("source_file"))
        # Scope nodes contain no source document. They can connect allowed
        # documents, but a disallowed document can never become an intermediate.
        return not source or source in allowed_paths

    @staticmethod
    def _edge_weight(edge: dict[str, Any]) -> float:
        confidence = str(edge.get("confidence") or "").casefold()
        return {
            "declared": 1.0,
            "high": 0.9,
            "medium": 0.65,
            "low": 0.35,
        }.get(confidence, 0.5)

    def rank(
        self,
        query: str,
        candidate_paths: list[str],
        limit: int = 30,
        *,
        allowed_paths: set[str] | frozenset[str] | None = None,
        max_depth: int = 2,
    ) -> list[str]:
        self._load()
        allowed = (
            frozenset(self._path_key(path) for path in allowed_paths)
            if allowed_paths is not None
            else None
        )
        query_tokens = set(tokenize(query))
        path_priority = {
            self._path_key(path): len(candidate_paths) - rank
            for rank, path in enumerate(candidate_paths)
        }
        seed_scores: list[tuple[float, str]] = []
        for node_id, node in self.nodes.items():
            if not self._node_allowed(node_id, allowed):
                continue
            label_tokens = set(tokenize(str(node.get("label", ""))))
            overlap = len(query_tokens & label_tokens)
            source = self._path_key(self._source_path(node.get("source_file")))
            path_score = path_priority.get(source, 0)
            if overlap or path_score:
                seed_scores.append(
                    (overlap * 4.0 + path_score * 0.25, node_id)
                )
        seed_scores.sort(reverse=True)
        path_scores: dict[str, tuple[float, str]] = {}
        bounded_depth = max(0, min(max_depth, 4))
        for seed_score, seed_id in seed_scores[: max(limit * 3, limit)]:
            queue = [(-seed_score, 0, seed_id)]
            best_scores = {(seed_id, 0): seed_score}
            while queue:
                negative_score, distance, node_id = heapq.heappop(queue)
                score = -negative_score
                if score < best_scores.get((node_id, distance), 0.0):
                    continue
                source = self._source_path(
                    self.nodes[node_id].get("source_file")
                )
                key = self._path_key(source)
                if source and (
                    key not in path_scores or score > path_scores[key][0]
                ):
                    path_scores[key] = (score, source)
                if distance >= bounded_depth:
                    continue
                for neighbor_id, edge in self.adjacency.get(node_id, []):
                    if not self._node_allowed(neighbor_id, allowed):
                        continue
                    next_score = (
                        score
                        * self._edge_weight(edge)
                        * 0.45
                    )
                    state = (neighbor_id, distance + 1)
                    if next_score <= best_scores.get(state, 0.0):
                        continue
                    # A stronger path must replace a weaker path discovered first.
                    best_scores[state] = next_score
                    heapq.heappush(
                        queue,
                        (-next_score, distance + 1, neighbor_id),
                    )
        ranked = sorted(
            path_scores.values(),
            key=lambda item: (-item[0], item[1].casefold()),
        )
        return [path for _, path in ranked[:limit]]

    def neighbors(
        self,
        path: str,
        depth: int = 1,
        limit: int = 20,
        *,
        allowed_paths: set[str] | frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        self._load()
        allowed = (
            frozenset(self._path_key(item) for item in allowed_paths)
            if allowed_paths is not None
            else None
        )
        starts = self.source_nodes.get(self._path_key(path), set())
        queue = deque((node_id, 0) for node_id in starts)
        visited = set(starts)
        results: list[dict[str, Any]] = []
        while queue and len(results) < limit:
            node_id, distance = queue.popleft()
            if distance >= depth:
                continue
            for neighbor_id, edge in self.adjacency.get(node_id, []):
                if (
                    neighbor_id in visited
                    or not self._node_allowed(neighbor_id, allowed)
                ):
                    continue
                visited.add(neighbor_id)
                node = self.nodes[neighbor_id]
                results.append(
                    {
                        "node_id": neighbor_id,
                        "label": node.get("label"),
                        "path": node.get("source_file"),
                        "distance": distance + 1,
                        "relation": edge.get("relation"),
                        "confidence": edge.get("confidence"),
                    }
                )
                queue.append((neighbor_id, distance + 1))
        return results

    def path(
        self,
        source_path: str,
        target_path: str,
        max_depth: int = 6,
        *,
        allowed_paths: set[str] | frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        self._load()
        allowed = (
            frozenset(self._path_key(item) for item in allowed_paths)
            if allowed_paths is not None
            else None
        )
        starts = self.source_nodes.get(self._path_key(source_path), set())
        targets = self.source_nodes.get(self._path_key(target_path), set())
        if not starts or not targets:
            return []
        queue = deque(starts)
        parents: dict[str, tuple[str, dict[str, Any]] | None] = {
            node_id: None for node_id in starts
        }
        depths = {node_id: 0 for node_id in starts}
        found: str | None = None
        while queue:
            node_id = queue.popleft()
            if node_id in targets:
                found = node_id
                break
            if depths[node_id] >= max_depth:
                continue
            for neighbor_id, edge in self.adjacency.get(node_id, []):
                if (
                    neighbor_id in parents
                    or not self._node_allowed(neighbor_id, allowed)
                ):
                    continue
                parents[neighbor_id] = (node_id, edge)
                depths[neighbor_id] = depths[node_id] + 1
                queue.append(neighbor_id)
        if not found:
            return []
        chain: list[dict[str, Any]] = []
        cursor = found
        while parents[cursor] is not None:
            parent, edge = parents[cursor]
            chain.append(
                {
                    "source": self.nodes[parent].get("label"),
                    "source_path": self.nodes[parent].get("source_file"),
                    "relation": edge.get("relation"),
                    "confidence": edge.get("confidence"),
                    "target": self.nodes[cursor].get("label"),
                    "target_path": self.nodes[cursor].get("source_file"),
                }
            )
            cursor = parent
        return list(reversed(chain))
