#!/usr/bin/env python3
"""Extract a Graphify corpus from one AI Memory source on any platform."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common import (  # noqa: E402
    ScriptError,
    graphify_executable,
    graphify_state_root,
    info,
    load_environment,
    memory_sources,
    repository_root,
    run_main,
)

REQUIRED_SETTINGS = (
    "GRAPHIFY_OPENAI_BASE_URL",
    "GRAPHIFY_OPENAI_API_KEY",
    "GRAPHIFY_OPENAI_MODEL",
    "GRAPHIFY_OPENAI_TOKEN_BUDGET",
)

EXCLUDES = (
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.svg",
    "*.html",
    "*.json",
    ".obsidian/**",
    # Match the MCP index exclusions for every configured source.
    "**/Restricted/**",
    "**/.trash/**",
    "References/Restricted/**",
    "*.zip",
)


def _positive_int(raw: str, default: int, name: str) -> int:
    if not raw.strip():
        return default
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ScriptError(f"{name} must be a positive integer.") from exc
    if parsed <= 0:
        raise ScriptError(f"{name} must be a positive integer.")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract a Graphify corpus from an AI Memory source."
    )
    parser.add_argument("--out-root", type=Path, default=None)
    parser.add_argument("--memory-root", type=Path, default=None)
    parser.add_argument("--source-id", default="")
    parser.add_argument("--skip-global", action="store_true")
    args = parser.parse_args()

    root = repository_root()
    load_environment(root)

    corpora_root = graphify_state_root() / "corpora"
    corpora_root.mkdir(parents=True, exist_ok=True)

    executable = graphify_executable(root)
    if not executable.is_file():
        raise ScriptError(
            f"Pinned Graphify executable was not found at {executable}. "
            "Run scripts/setup.py."
        )

    sources = memory_sources()
    source_id = args.source_id.strip().casefold()
    if not source_id:
        writable = next((item for item in sources if item.writable), None)
        if writable is None:
            raise ScriptError("No writable memory source is configured.")
        source_id = writable.source_id

    memory_root = args.memory_root
    if memory_root is None:
        selected = next(
            (item for item in sources if item.source_id == source_id), None
        )
        if selected is None:
            raise ScriptError(f"Unknown memory source ID: {source_id}")
        memory_root = selected.root
    memory_root = memory_root.expanduser().resolve()

    for setting in REQUIRED_SETTINGS:
        if not os.environ.get(setting, "").strip():
            raise ScriptError(f"{setting} is required for AI-Memory extraction.")

    environment = dict(os.environ)
    environment["OPENAI_BASE_URL"] = environment["GRAPHIFY_OPENAI_BASE_URL"]
    environment["OPENAI_API_KEY"] = environment["GRAPHIFY_OPENAI_API_KEY"]
    environment["OPENAI_MODEL"] = environment["GRAPHIFY_OPENAI_MODEL"]
    if not environment.get("GRAPHIFY_MAX_RETRIES", "").strip():
        environment["GRAPHIFY_MAX_RETRIES"] = "1"

    token_budget = _positive_int(
        os.environ.get("GRAPHIFY_OPENAI_TOKEN_BUDGET", ""),
        16000,
        "GRAPHIFY_OPENAI_TOKEN_BUDGET",
    )
    max_concurrency = _positive_int(
        os.environ.get("GRAPHIFY_OPENAI_MAX_CONCURRENCY", ""),
        2,
        "GRAPHIFY_OPENAI_MAX_CONCURRENCY",
    )
    api_timeout = _positive_int(
        os.environ.get("GRAPHIFY_OPENAI_API_TIMEOUT", ""),
        300,
        "GRAPHIFY_OPENAI_API_TIMEOUT",
    )

    tag = f"ai-memory-{source_id}"
    out_root = args.out_root or (corpora_root / tag)

    info(f"=== {tag} ===")
    command = [
        str(executable),
        "extract",
        str(memory_root),
        "--backend",
        "openai",
        "--model",
        environment["OPENAI_MODEL"],
        "--out",
        str(out_root),
        "--max-concurrency",
        str(max_concurrency),
        "--token-budget",
        str(token_budget),
        "--api-timeout",
        str(api_timeout),
    ]
    for pattern in EXCLUDES:
        command.extend(["--exclude", pattern])
    if not args.skip_global:
        command.extend(["--global", "--as", tag])

    if subprocess.run(command, check=False, env=environment).returncode != 0:
        raise ScriptError("graphify extract failed for ai-memory")

    if not args.skip_global:
        info("")
        subprocess.run(
            [str(executable), "global", "list"], check=False, env=environment
        )


if __name__ == "__main__":
    run_main(main)
