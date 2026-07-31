#!/usr/bin/env python3
"""Register AI Memory MCP with every supported client on any platform."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    ScriptError,
    app_python,
    info,
    repository_root,
    run_main,
)

# Codex is configured by a dedicated script because it uses TOML rather than the
# JSON surface the packaged installer writes.
CODEX = "codex"
PACKAGE_CLIENTS = (
    "claude-code",
    "claude-desktop",
    "copilot",
    "opencode",
    "vscode",
    "agent-skills",
)
ALL_CLIENTS = (CODEX, *PACKAGE_CLIENTS)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install AI Memory MCP into supported clients."
    )
    parser.add_argument("--repository-root", type=Path, default=None)
    parser.add_argument(
        "--client",
        action="append",
        choices=ALL_CLIENTS,
        dest="clients",
        help="Restrict installation to specific clients (repeatable).",
    )
    args = parser.parse_args()

    root = (args.repository_root or repository_root()).resolve()
    python = app_python(root)
    if not python.is_file():
        raise ScriptError(
            f"Application environment is missing: {python}. Run scripts/setup.py."
        )

    clients = args.clients or list(ALL_CLIENTS)
    scripts_dir = Path(__file__).resolve().parent

    if CODEX in clients:
        result = subprocess.run(
            [
                str(python),
                str(scripts_dir / "install_codex.py"),
                "--repository-root",
                str(root),
            ],
            check=False,
        )
        if result.returncode != 0:
            raise ScriptError("Failed to install the Codex configuration.")

    selected = [client for client in clients if client in PACKAGE_CLIENTS]
    if selected:
        command = [
            str(python),
            "-m",
            "ai_memory_mcp.client_install",
            "--repository-root",
            str(root),
        ]
        for client in selected:
            command.extend(["--client", client])
        result = subprocess.run(command, check=False)
        if result.returncode != 0:
            raise ScriptError(
                "Failed to install one or more AI Memory client configurations."
            )

    info("Restart each configured client to load AI Memory MCP and its skill.")


if __name__ == "__main__":
    run_main(main)
