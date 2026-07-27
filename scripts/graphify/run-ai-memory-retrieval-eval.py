"""Small, deterministic retrieval gate for the live AI-Memory corpus."""

from __future__ import annotations

import json
import os
from pathlib import Path

from networkx.readwrite import json_graph

from graphify.serve import _query_graph_text


DEFAULT_CASES = (
    ("Memory Map", "Indexes/Memory Map.md"),
    ("durable memory", "Memory Map"),
)


def retrieval_cases() -> tuple[tuple[str, str], ...]:
    raw = os.getenv("GRAPHIFY_MEMORY_RETRIEVAL_EVAL_CASES", "").strip()
    if not raw:
        return DEFAULT_CASES
    configured = json.loads(raw)
    if not isinstance(configured, list):
        raise ValueError("Retrieval evaluation cases must be a JSON list.")
    cases: list[tuple[str, str]] = []
    for item in configured:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(value, str) and value for value in item)
        ):
            raise ValueError(
                "Each retrieval evaluation case must contain two strings."
            )
        cases.append((item[0], item[1]))
    return tuple(cases)


def main() -> int:
    state_root = Path(
        os.getenv(
            "AI_MEMORY_GRAPHIFY_STATE_DIR",
            Path.home() / ".graphify",
        )
    ).expanduser()
    path = state_root / "global-graph.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    try:
        graph = json_graph.node_link_graph(data, edges="links")
    except TypeError:
        graph = json_graph.node_link_graph(data)
    # Graphify 0.9.26 removed the local `repo=` query parameter. Scope the
    # provider graph before traversal; the MCP facade applies the same boundary
    # before hybrid ranking.
    memory_nodes = [
        node_id
        for node_id, attributes in graph.nodes(data=True)
        if attributes.get("repo") == "ai-memory"
    ]
    memory_graph = graph.subgraph(memory_nodes).copy()

    cases = retrieval_cases()
    failures: list[str] = []
    for question, expected in cases:
        result = _query_graph_text(
            memory_graph, question, depth=1, token_budget=600
        )
        if expected not in result:
            failures.append(f"{question!r} did not retrieve {expected!r}")

    summary = {
        "cases": len(cases),
        "passed": len(cases) - len(failures),
        "scopedNodes": memory_graph.number_of_nodes(),
        "failures": failures,
    }
    print(json.dumps(summary, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
