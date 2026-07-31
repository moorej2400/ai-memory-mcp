#!/usr/bin/env python3
"""Start the pinned Graphify global MCP listener on any platform."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common import (  # noqa: E402
    ScriptError,
    graphify_mcp_executable,
    graphify_state_root,
    info,
    load_environment,
    mcp_url,
    repository_root,
    run_main,
)
from _processes import (  # noqa: E402
    detached_popen,
    find_processes,
    port_is_serving,
    terminate_tree,
    wait_for_port,
)

STARTUP_DEADLINE_SECONDS = 45
# A just-stopped listener can hold the port briefly while the kernel tears the
# socket down, so a single probe would report a false conflict.
PORT_RELEASE_SECONDS = 5.0


def _wait_for_port_release(host: str, port: int, seconds: float) -> bool:
    """Return whether the port is still occupied after waiting for release."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not port_is_serving(host, port):
            return False
        time.sleep(0.25)
    return port_is_serving(host, port)


def main() -> None:
    root = repository_root()
    load_environment(root)

    state_root = graphify_state_root()
    graph_path = state_root / "global-graph.json"
    log_dir = state_root / "logs"

    executable = graphify_mcp_executable(root)
    if not executable.is_file():
        raise ScriptError(
            f"Pinned Graphify MCP executable was not found at {executable}. "
            "Run scripts/setup.py."
        )
    if not graph_path.is_file():
        raise ScriptError(
            f"Global graph not found at {graph_path}. Run the extract step first."
        )

    endpoint = urlparse(mcp_url())
    host = endpoint.hostname or "127.0.0.1"
    port = endpoint.port or 4324
    mount_path = endpoint.path or "/mcp"

    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / "graphify-global-mcp.out.log"
    stderr_path = log_dir / "graphify-global-mcp.err.log"

    # Stop only previous instances bound to this exact port so a restart cannot
    # leave a stale listener behind and make the readiness check lie.
    for process in find_processes("graphify-mcp", f"--port {port}"):
        terminate_tree(process.pid)

    # Once our own instances are stopped the port must be free. Anything still
    # answering belongs to another program, and starting anyway would let the
    # readiness check below succeed against that foreign listener while our own
    # process quietly fails to bind. The PowerShell implementation avoided this
    # by matching the listener's owning process; refusing to start is the
    # portable equivalent of that guarantee.
    if _wait_for_port_release(host, port, PORT_RELEASE_SECONDS):
        raise ScriptError(
            f"Port {port} is already in use by another program, so the "
            "Graphify MCP cannot bind to it. Stop that program, or set "
            "GRAPHIFY_GLOBAL_MCP_URL to a free port."
        )

    with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
        child = detached_popen(
            [
                str(executable),
                "--graph",
                str(graph_path),
                "--transport",
                "http",
                "--host",
                host,
                "--port",
                str(port),
                "--path",
                mount_path,
            ],
            stdout,
            stderr,
        )

    if not wait_for_port(host, port, STARTUP_DEADLINE_SECONDS, child):
        tail = ""
        if stderr_path.is_file():
            lines = stderr_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            tail = "\n".join(lines[-40:])
        raise ScriptError(
            f"Graphify MCP did not start listening on port {port} within "
            f"{STARTUP_DEADLINE_SECONDS} seconds. Check {stderr_path}\n{tail}"
        )

    info(f"graphify-global-mcp started (PID {child.pid}) on {endpoint.geturl()}")


if __name__ == "__main__":
    run_main(main)
