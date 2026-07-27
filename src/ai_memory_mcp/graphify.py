from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from .text import tokenize


class GraphifyAdapter:
    """Expose only provider-neutral graph operations to the retrieval engine."""

    def __init__(self, graph_path: Path):
        self.graph_path = graph_path
        self._stamp: tuple[int, int] | None = None
        self.nodes: dict[str, dict[str, Any]] = {}
        self.adjacency: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        self.source_nodes: dict[str, set[str]] = defaultdict(set)

    def _load(self) -> None:
        if not self.graph_path.exists():
            self.nodes = {}
            self.adjacency = defaultdict(list)
            self.source_nodes = defaultdict(set)
            self._stamp = None
            return
        stat = self.graph_path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return
        payload = json.loads(self.graph_path.read_text(encoding="utf-8"))
        self.nodes = {str(node["id"]): node for node in payload.get("nodes", [])}
        self.adjacency = defaultdict(list)
        self.source_nodes = defaultdict(set)
        for node_id, node in self.nodes.items():
            source = str(node.get("source_file") or "").replace("\\", "/").casefold()
            if source:
                self.source_nodes[source].add(node_id)
        for edge in payload.get("links", payload.get("edges", [])):
            source = str(edge.get("source", ""))
            target = str(edge.get("target", ""))
            if source in self.nodes and target in self.nodes:
                self.adjacency[source].append((target, edge))
                self.adjacency[target].append((source, edge))
        self._stamp = stamp

    def health(self) -> dict[str, Any]:
        self._load()
        return {
            "available": bool(self.nodes),
            "path": str(self.graph_path),
            "nodes": len(self.nodes),
            "edges": sum(len(values) for values in self.adjacency.values()) // 2,
            "modified_ns": self._stamp[0] if self._stamp else None,
        }

    def rank(self, query: str, candidate_paths: list[str], limit: int = 30) -> list[str]:
        self._load()
        query_tokens = set(tokenize(query))
        path_priority = {
            path.replace("\\", "/").casefold(): len(candidate_paths) - rank
            for rank, path in enumerate(candidate_paths)
        }
        scores: list[tuple[float, str]] = []
        for node_id, node in self.nodes.items():
            label_tokens = set(tokenize(str(node.get("label", ""))))
            overlap = len(query_tokens & label_tokens)
            source = str(node.get("source_file") or "").replace("\\", "/").casefold()
            path_score = path_priority.get(source, 0)
            if overlap or path_score:
                scores.append((overlap * 4.0 + path_score * 0.25, node_id))
        scores.sort(reverse=True)
        ranked_paths: list[str] = []
        seen: set[str] = set()
        for _, node_id in scores[: max(limit * 3, limit)]:
            node = self.nodes[node_id]
            source = str(node.get("source_file") or "").replace("\\", "/")
            if source and source.casefold() not in seen:
                ranked_paths.append(source)
                seen.add(source.casefold())
            for neighbor_id, _ in self.adjacency.get(node_id, []):
                neighbor_source = str(
                    self.nodes[neighbor_id].get("source_file") or ""
                ).replace("\\", "/")
                if neighbor_source and neighbor_source.casefold() not in seen:
                    ranked_paths.append(neighbor_source)
                    seen.add(neighbor_source.casefold())
        return ranked_paths[:limit]

    def neighbors(self, path: str, depth: int = 1, limit: int = 20) -> list[dict[str, Any]]:
        self._load()
        starts = self.source_nodes.get(path.replace("\\", "/").casefold(), set())
        queue = deque((node_id, 0) for node_id in starts)
        visited = set(starts)
        results: list[dict[str, Any]] = []
        while queue and len(results) < limit:
            node_id, distance = queue.popleft()
            if distance >= depth:
                continue
            for neighbor_id, edge in self.adjacency.get(node_id, []):
                if neighbor_id in visited:
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

    def path(self, source_path: str, target_path: str, max_depth: int = 6) -> list[dict[str, Any]]:
        self._load()
        starts = self.source_nodes.get(source_path.replace("\\", "/").casefold(), set())
        targets = self.source_nodes.get(target_path.replace("\\", "/").casefold(), set())
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
                if neighbor_id in parents:
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

