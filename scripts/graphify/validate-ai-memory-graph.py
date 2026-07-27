"""Fail closed when an AI-Memory corpus, global graph, or MCP is unhealthy."""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def graph_counts(path: Path) -> tuple[int, int, set[str], list[dict[str, Any]]]:
    data = load_json(path)
    nodes = data.get("nodes", [])
    edges = data.get("links", data.get("edges", []))
    ids = {str(node.get("id")) for node in nodes}
    if len(ids) != len(nodes):
        raise ValueError(f"{path}: duplicate or missing node IDs")
    missing = [
        edge for edge in edges
        if str(edge.get("source")) not in ids or str(edge.get("target")) not in ids
    ]
    if missing:
        raise ValueError(f"{path}: {len(missing)} dangling edge(s)")
    self_loops = [edge for edge in edges if edge.get("source") == edge.get("target")]
    if self_loops:
        raise ValueError(f"{path}: {len(self_loops)} self-loop edge(s)")
    return len(nodes), len(edges), ids, nodes


def parse_sse(body: str) -> dict[str, Any]:
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return json.loads(body) if body.strip() else {}


def mcp_post(url: str, payload: dict[str, Any], session_id: str | None = None) -> tuple[dict[str, Any], str | None]:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8")
        return parse_sse(body), response.headers.get("Mcp-Session-Id") or session_id


def validate_mcp(url: str) -> dict[str, Any]:
    init, session_id = mcp_post(url, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "graphify-health-gate", "version": "1.0"},
        },
    })
    if "result" not in init:
        raise ValueError(f"MCP initialize failed: {init}")
    mcp_post(url, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, session_id)
    tools, session_id = mcp_post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, session_id)
    tool_names = {tool["name"] for tool in tools.get("result", {}).get("tools", [])}
    required = {"query_graph", "graph_stats"}
    if not required.issubset(tool_names):
        raise ValueError(f"MCP tools missing: {sorted(required - tool_names)}")
    # query_ai_memory was a local 0.9.5 extension. The facade now owns corpus
    # scoping, while stock Graphify 0.9.26 remains a provider with query_graph.
    query_tool = "query_ai_memory" if "query_ai_memory" in tool_names else "query_graph"
    query, _ = mcp_post(url, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": query_tool, "arguments": {"question": "Memory Map", "depth": 1}},
    }, session_id)
    content = query.get("result", {}).get("content", [])
    text = "\n".join(item.get("text", "") for item in content if item.get("type") == "text")
    if "Indexes/Memory Map.md" not in text:
        raise ValueError(f"MCP AI-Memory retrieval failed: {text[:500]}")
    return {"toolCount": len(tool_names), "queryPreview": text[:500]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--global-graph", type=Path)
    parser.add_argument("--prior-corpus-nodes", type=int, default=0)
    parser.add_argument("--mcp-url")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    corpus_nodes, corpus_edges, _, _ = graph_counts(args.corpus)
    manifest = load_json(args.manifest)
    if not manifest:
        raise ValueError("AI-Memory manifest is empty; incremental refresh would reprocess every file")
    if args.prior_corpus_nodes and corpus_nodes < max(1, args.prior_corpus_nodes // 2):
        raise ValueError(
            f"Corpus shrank from {args.prior_corpus_nodes} to {corpus_nodes} nodes; refusing publication"
        )

    result: dict[str, Any] = {
        "corpusNodes": corpus_nodes,
        "corpusEdges": corpus_edges,
        "manifestFiles": len(manifest),
    }
    if args.global_graph:
        global_nodes, global_edges, _, nodes = graph_counts(args.global_graph)
        memory_nodes = sum(1 for node in nodes if node.get("repo") == "ai-memory")
        if memory_nodes < max(1, int(corpus_nodes * 0.9)):
            raise ValueError(
                f"Global graph contains only {memory_nodes}/{corpus_nodes} ai-memory nodes"
            )
        result.update(globalNodes=global_nodes, globalEdges=global_edges, aiMemoryNodes=memory_nodes)
    if args.mcp_url:
        result["mcp"] = validate_mcp(args.mcp_url)

    serialized = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
