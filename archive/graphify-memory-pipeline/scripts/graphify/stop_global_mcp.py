#!/usr/bin/env python3
"""Stop the Graphify global MCP listener on any platform."""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common import (  # noqa: E402
    info,
    load_environment,
    mcp_url,
    repository_root,
    run_main,
)
from _processes import find_processes, terminate_tree  # noqa: E402


def main() -> None:
    load_environment(repository_root())
    port = urlparse(mcp_url()).port or 4324

    stopped = False
    for process in find_processes("graphify-mcp", f"--port {port}"):
        terminate_tree(process.pid)
        info(f"Stopped PID {process.pid}")
        stopped = True

    if not stopped:
        info(f"No Graphify MCP process was listening on port {port}.")


if __name__ == "__main__":
    run_main(main)
