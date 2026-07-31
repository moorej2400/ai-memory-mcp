#!/usr/bin/env python3
"""Provision AI Memory MCP on Windows, macOS, or Linux."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    ScriptError,
    app_python,
    graphify_runtime_root,
    info,
    repository_root,
    run_main,
    venv_python,
)

MINIMUM_PYTHON = (3, 11)

ENV_TEMPLATE = """AI_MEMORY_WORK_DIR="{memory_root}"
AI_MEMORY_PRIMARY_SOURCE_ID="core"
AI_MEMORY_RETRIEVAL_SOURCES="{{}}"
AI_MEMORY_MCP_STATE_DIR="{home}/.ai-memory-mcp"
AI_MEMORY_GRAPHIFY_STATE_DIR="{home}/.graphify"
AI_MEMORY_GRAPH_PATH="{home}/.graphify/corpora/ai-memory/graphify-out/graph.json"
GRAPHIFY_GLOBAL_MCP_URL="http://127.0.0.1:4324/mcp"
GRAPHIFY_OPENAI_BASE_URL=""
GRAPHIFY_OPENAI_API_KEY=""
GRAPHIFY_OPENAI_MODEL=""
GRAPHIFY_OPENAI_TOKEN_BUDGET="30000"
GRAPHIFY_OPENAI_MAX_CONCURRENCY="1"
GRAPHIFY_OPENAI_API_TIMEOUT="300"
GRAPHIFY_MAX_RETRIES="1"
"""


def _run(command: list[str], failure: str) -> None:
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        raise ScriptError(failure)


def _bootstrap_interpreter() -> list[str]:
    """Return the interpreter used to create the virtual environments.

    The running interpreter is preferred because it is already known to satisfy
    the version floor and matches the environment the user invoked.
    """
    if sys.version_info >= MINIMUM_PYTHON:
        return [sys.executable]
    raise ScriptError(
        "Python "
        f"{MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]} or newer is required; "
        f"found {sys.version.split()[0]} at {sys.executable}."
    )


def _ensure_venv(venv_root: Path, bootstrap: list[str], label: str) -> Path:
    python = venv_python(venv_root)
    if not python.is_file():
        _run(
            [*bootstrap, "-m", "venv", str(venv_root)],
            f"Failed to create the {label} virtual environment.",
        )
        python = venv_python(venv_root)
    if not python.is_file():
        raise ScriptError(f"The {label} environment has no interpreter: {python}")
    return python


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Provision AI Memory MCP for this machine."
    )
    parser.add_argument(
        "--memory-root",
        required=True,
        type=Path,
        help="Directory holding the canonical memory notes.",
    )
    parser.add_argument("--install-codex", action="store_true")
    parser.add_argument("--install-clients", action="store_true")
    parser.add_argument("--skip-graphify-runtime", action="store_true")
    args = parser.parse_args()

    root = repository_root()
    memory_root = args.memory_root.expanduser()
    if not memory_root.is_dir():
        raise ScriptError(f"Memory root directory is missing: {memory_root}")
    memory_root = memory_root.resolve()

    bootstrap = _bootstrap_interpreter()

    application_python = _ensure_venv(root / ".venv", bootstrap, "application")
    _run(
        [str(application_python), "-m", "pip", "install", "--upgrade", "pip"],
        "Failed to update pip in the application environment.",
    )
    _run(
        [str(application_python), "-m", "pip", "install", "-e", f"{root}[dev]"],
        "Failed to install AI Memory MCP.",
    )

    if not args.skip_graphify_runtime:
        graphify_interpreter = _ensure_venv(
            graphify_runtime_root(root), bootstrap, "Graphify"
        )
        _run(
            [str(graphify_interpreter), "-m", "pip", "install", "--upgrade", "pip"],
            "Failed to update pip in the Graphify environment.",
        )
        _run(
            [
                str(graphify_interpreter),
                "-m",
                "pip",
                "install",
                "-r",
                str(root / "requirements-graphify.txt"),
            ],
            "Failed to install the pinned Graphify runtime.",
        )

    env_path = root / ".env"
    if env_path.is_file():
        info(f"Kept existing local configuration: {env_path}")
    else:
        # Forward slashes stay valid on every platform and avoid escaping rules
        # differing between the shells that read this file.
        env_path.write_text(
            ENV_TEMPLATE.format(
                memory_root=memory_root.as_posix(),
                home=Path.home().as_posix(),
            ),
            encoding="utf-8",
            newline="\n",
        )
        info(f"Created local configuration: {env_path}")

    _run(
        [str(application_python), "-m", "ai_memory_mcp.cli"],
        "The initial AI Memory index build failed.",
    )

    scripts_dir = Path(__file__).resolve().parent
    if args.install_clients:
        _run(
            [str(app_python(root)), str(scripts_dir / "install_clients.py")],
            "Failed to install AI Memory client configurations.",
        )
    elif args.install_codex:
        _run(
            [str(app_python(root)), str(scripts_dir / "install_codex.py")],
            "Failed to install the Codex configuration.",
        )

    info(f"AI Memory MCP is ready at {root}")


if __name__ == "__main__":
    run_main(main)
