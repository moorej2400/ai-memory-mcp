from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .index import current_index_path
from .text import wikilink_targets


def _values(raw: str) -> list[str]:
    value = json.loads(raw)
    return [str(item) for item in value] if isinstance(value, list) else []


def _identity_keys(row: sqlite3.Row) -> set[str]:
    path = str(row["path"]).replace("\\", "/")
    relative = path.split("/", 1)[-1]
    keys = {
        str(row["memory_id"]).casefold(),
        str(row["title"]).casefold(),
        path.casefold(),
        relative.casefold(),
        Path(relative).stem.casefold(),
    }
    keys.update(value.casefold() for value in _values(row["identifiers_json"]))
    return {key for key in keys if key}


def _related_keys(value: str) -> list[str]:
    normalized = value.strip()
    if normalized.startswith("[[") and normalized.endswith("]]"):
        normalized = normalized[2:-2]
    target, separator, label = normalized.partition("|")
    target = target.strip().replace("\\", "/")
    keys = [
        target.casefold(),
        Path(target).stem.casefold(),
    ]
    if separator and label.strip():
        keys.append(label.strip().casefold())
    return list(dict.fromkeys(key for key in keys if key))


def _resolve_link(
    value: str,
    identity_candidates: dict[str, set[str]],
) -> tuple[str | None, str]:
    """Resolve one link target to a memory_id.

    Separating "no such note" from "several notes match" matters: the first is
    a broken link the author can fix, the second needs a disambiguating title.
    """
    candidates: set[str] = set()
    for key in _related_keys(value):
        matches = identity_candidates.get(key, set())
        if len(matches) == 1:
            return next(iter(matches)), "resolved"
        candidates.update(matches)
    if not candidates:
        return None, "unresolved"
    if len(candidates) > 1:
        return None, "ambiguous"
    return next(iter(candidates)), "resolved"


def build_provider_graph(
    settings: Settings,
    output_dir: Path,
) -> dict[str, Any]:
    index_path = current_index_path(settings)
    if index_path is None:
        raise FileNotFoundError(
            "Memory index is not available. Run memory_sync before Graphify refresh."
        )
    with sqlite3.connect(
        f"file:{index_path.as_posix()}?mode=ro",
        uri=True,
    ) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT memory_id, source_id, path, title, body, status, root_scope,
                   scope_kind, scope_id, related_json, identifiers_json,
                   projects_json, repos_json, tools_json, content_hash, mtime_ns
            FROM documents
            ORDER BY source_id, path
            """
        ).fetchall()

    node_id_by_memory = {
        str(row["memory_id"]): f"{row['source_id']}::{row['memory_id']}"
        for row in rows
    }
    identity_candidates: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        for key in _identity_keys(row):
            identity_candidates[key].add(str(row["memory_id"]))

    nodes: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    edge_keys: set[tuple[str, str, str]] = set()
    scope_nodes: dict[tuple[str, str], str] = {}
    unresolved_related = 0

    for row in rows:
        memory_id = str(row["memory_id"])
        node_id = node_id_by_memory[memory_id]
        nodes.append(
            {
                "id": node_id,
                "label": str(row["title"]),
                "file_type": "markdown",
                "source_file": str(row["path"]).replace("\\", "/"),
                "memory_id": memory_id,
                "source_id": str(row["source_id"]),
                "status": str(row["status"]),
                "root_scope": str(row["root_scope"]),
            }
        )

        scope_kind = str(row["scope_kind"])
        scope_id = str(row["scope_id"])
        if scope_kind and scope_id:
            scope_key = (scope_kind.casefold(), scope_id.casefold())
            scope_node_id = scope_nodes.setdefault(
                scope_key,
                f"scope::{scope_key[0]}::{scope_key[1]}",
            )
            edge_key = (node_id, scope_node_id, "belongs-to")
            if edge_key not in edge_keys:
                edge_keys.add(edge_key)
                links.append(
                    {
                        "source": node_id,
                        "target": scope_node_id,
                        "relation": "belongs-to",
                        "confidence": "DECLARED",
                        "source_file": str(row["path"]).replace("\\", "/"),
                    }
                )

        for related in _values(row["related_json"]):
            candidates: set[str] = set()
            for key in _related_keys(related):
                matches = identity_candidates.get(key, set())
                if len(matches) == 1:
                    candidates = set(matches)
                    break
                candidates.update(matches)
            if len(candidates) != 1:
                unresolved_related += 1
                continue
            target_memory_id = next(iter(candidates))
            target_id = node_id_by_memory[target_memory_id]
            if target_id == node_id:
                continue
            edge_source_id, edge_target_id = sorted((node_id, target_id))
            edge_key = (
                edge_source_id,
                edge_target_id,
                "declared-related",
            )
            if edge_key in edge_keys:
                continue
            edge_keys.add(edge_key)
            links.append(
                {
                    "source": edge_source_id,
                    "target": edge_target_id,
                    "relation": "declared-related",
                    "confidence": "DECLARED",
                    "source_file": str(row["path"]).replace("\\", "/"),
                }
            )

    # Second pass: body wikilinks. It runs after every frontmatter edge exists
    # so a body link never duplicates a pair already joined by `related`.
    body_links = 0
    unresolved_body_links = 0
    ambiguous_body_links = 0
    for row in rows:
        node_id = node_id_by_memory[str(row["memory_id"])]
        source_file = str(row["path"]).replace("\\", "/")
        for target in dict.fromkeys(wikilink_targets([str(row["body"])])):
            target_memory_id, state = _resolve_link(target, identity_candidates)
            if state == "unresolved":
                unresolved_body_links += 1
                continue
            if state == "ambiguous":
                ambiguous_body_links += 1
                continue
            target_id = node_id_by_memory[str(target_memory_id)]
            if target_id == node_id:
                continue
            edge_source_id, edge_target_id = sorted((node_id, target_id))
            if (edge_source_id, edge_target_id, "declared-related") in edge_keys:
                continue
            edge_key = (edge_source_id, edge_target_id, "body-link")
            if edge_key in edge_keys:
                continue
            edge_keys.add(edge_key)
            body_links += 1
            links.append(
                {
                    "source": edge_source_id,
                    "target": edge_target_id,
                    "relation": "body-link",
                    "confidence": "DECLARED",
                    "source_file": source_file,
                }
            )

    for (scope_kind, scope_id), node_id in sorted(scope_nodes.items()):
        nodes.append(
            {
                "id": node_id,
                "label": f"{scope_kind}: {scope_id}",
                "file_type": "memory-scope",
                "source_file": "",
                "scope_kind": scope_kind,
                "scope_id": scope_id,
            }
        )

    built_at = datetime.now(timezone.utc).isoformat()
    graph = {
        "directed": False,
        "multigraph": False,
        "graph": {
            "provider": "graphify-compatible",
            "build_mode": "deterministic-memory-index",
            "index_snapshot": index_path.name,
            "built_at": built_at,
            "memory_sources": [
                source.source_id for source in settings.memory_sources
            ],
        },
        "nodes": nodes,
        "links": links,
    }
    manifest = {
        str(row["path"]).replace("\\", "/"): {
            "mtime_ns": int(row["mtime_ns"]),
            "content_hash": str(row["content_hash"]),
        }
        for row in rows
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "graph.json").write_text(
        json.dumps(graph, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return {
        "index_snapshot": index_path.name,
        "documents": len(rows),
        "nodes": len(nodes),
        "edges": len(links),
        "scopes": len(scope_nodes),
        "unresolved_related": unresolved_related,
        "body_links": body_links,
        "unresolved_body_links": unresolved_body_links,
        "ambiguous_body_links": ambiguous_body_links,
        "output_dir": str(output_dir),
        "built_at": built_at,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the Graphify provider graph from the AI Memory index."
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    summary = build_provider_graph(Settings.from_env(), args.output_dir)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
