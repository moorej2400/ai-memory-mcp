"""Build an updated global graph entirely inside a staging directory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import graphify.global_graph as global_graph


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--stage-dir", required=True, type=Path)
    args = parser.parse_args()

    args.stage_dir.mkdir(parents=True, exist_ok=True)
    # global_add normally targets fixed files in ~/.graphify. Redirect all of
    # its reads and writes so the live graph stays untouched until validation.
    global_graph._GLOBAL_DIR = args.stage_dir
    global_graph._GLOBAL_GRAPH = args.stage_dir / "global-graph.json"
    global_graph._GLOBAL_MANIFEST = args.stage_dir / "global-manifest.json"

    result = global_graph.global_add(args.source.resolve(), "ai-memory")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
