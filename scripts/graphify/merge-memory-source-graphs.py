"""Merge named memory-source graphs into one AI Memory provider graph."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

SOURCE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _source_argument(value: str) -> tuple[str, Path]:
    source_id, separator, raw_path = value.partition("=")
    if not separator or not SOURCE_ID_PATTERN.fullmatch(source_id):
        raise argparse.ArgumentTypeError(
            "Use SOURCE_ID=GRAPH_PATH for each --source value."
        )
    path = Path(raw_path)
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"Graph file does not exist: {path}")
    return source_id, path


def _source_path(source_id: str, value: object) -> str:
    path = str(value or "").replace("\\", "/").lstrip("./")
    return f"{source_id}/{path}" if path else ""


def merge_sources(
    sources: list[tuple[str, Path]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    manifest: dict[str, Any] = {}
    seen_ids: set[str] = set()

    for source_id, graph_path in sources:
        graph = _load(graph_path)
        node_ids = {
            str(node["id"]): f"{source_id}::{node['id']}"
            for node in graph.get("nodes", [])
        }
        for node in graph.get("nodes", []):
            merged = dict(node)
            merged["id"] = node_ids[str(node["id"])]
            merged["source_id"] = source_id
            merged["source_file"] = _source_path(
                source_id,
                node.get("source_file"),
            )
            if merged["id"] in seen_ids:
                raise ValueError(f"Duplicate merged node ID: {merged['id']}")
            seen_ids.add(merged["id"])
            nodes.append(merged)

        for edge in graph.get("links", graph.get("edges", [])):
            source = node_ids.get(str(edge.get("source")))
            target = node_ids.get(str(edge.get("target")))
            if source is None or target is None:
                raise ValueError(
                    f"Graph '{source_id}' contains a dangling edge."
                )
            merged_edge = dict(edge)
            merged_edge["source"] = source
            merged_edge["target"] = target
            merged_edge["source_id"] = source_id
            links.append(merged_edge)

        source_manifest = _load(graph_path.with_name("manifest.json"))
        for path, value in source_manifest.items():
            manifest[_source_path(source_id, path)] = value

    graph = {
        "directed": False,
        "multigraph": False,
        "graph": {"memory_sources": [source_id for source_id, _ in sources]},
        "nodes": nodes,
        "links": links,
    }
    return graph, manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge Graphify graphs from configured memory sources."
    )
    parser.add_argument(
        "--source",
        action="append",
        required=True,
        type=_source_argument,
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    graph, manifest = merge_sources(args.source)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "graph.json").write_text(
        json.dumps(graph, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(
        f"Merged {len(args.source)} memory sources: "
        f"{len(graph['nodes'])} nodes and {len(graph['links'])} edges."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
