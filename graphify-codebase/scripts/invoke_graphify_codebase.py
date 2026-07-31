#!/usr/bin/env python3
"""Drive Graphify against a code repository on Windows, macOS, or Linux."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

MODES = ("build", "update", "query", "path", "explain")


class ScriptError(RuntimeError):
    """A failure that should be reported without a traceback."""


def _graphify_executable() -> str:
    """Locate the Graphify CLI.

    PATH is consulted first because that is what the PowerShell implementation
    on `main` does, and this skill is deliberately independent of the AI Memory
    runtime. The repository-local pinned runtime is only a fallback, so a
    machine without a global install still works.
    """
    found = shutil.which("graphify")
    if found:
        return found

    repository_root = Path(__file__).resolve().parents[2]
    bin_dir = repository_root / ".graphify-runtime" / (
        "Scripts" if sys.platform == "win32" else "bin"
    )
    suffix = ".exe" if sys.platform == "win32" else ""
    pinned = bin_dir / f"graphify{suffix}"
    if pinned.is_file():
        return str(pinned)

    raise ScriptError("Graphify is not installed or is not available on PATH.")


def _require(value: str, name: str, mode: str) -> str:
    if not value.strip():
        raise ScriptError(f"--{name} is required for {mode} mode.")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Graphify against a code repository."
    )
    parser.add_argument("--mode", choices=MODES, default="build")
    parser.add_argument("--path", type=Path, default=Path("."))
    parser.add_argument("--question", default="")
    parser.add_argument("--target", default="")
    args = parser.parse_args()

    repository = args.path.expanduser().resolve()
    if not repository.is_dir():
        raise ScriptError(f"The target directory does not exist: {repository}")

    inside = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--is-inside-work-tree"],
        check=False,
        capture_output=True,
        text=True,
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise ScriptError(f"The target is not a Git repository: {repository}")

    graphify = _graphify_executable()
    mode = args.mode
    if mode == "build":
        command = [graphify, "extract", str(repository)]
    elif mode == "update":
        command = [graphify, "update", str(repository)]
    elif mode == "query":
        command = [graphify, "query", _require(args.question, "question", mode)]
    elif mode == "explain":
        command = [graphify, "explain", _require(args.question, "question", mode)]
    else:
        command = [
            graphify,
            "path",
            _require(args.question, "question", mode),
            _require(args.target, "target", mode),
        ]

    # Graphify resolves its corpus relative to the working directory.
    result = subprocess.run(command, check=False, cwd=repository)
    if result.returncode != 0:
        raise ScriptError(f"Graphify failed in {mode} mode.")


if __name__ == "__main__":
    try:
        main()
    except ScriptError as error:
        print(f"error: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from error
