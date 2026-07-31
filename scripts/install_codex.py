#!/usr/bin/env python3
"""Register AI Memory MCP and its skills with Codex on any platform."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    ScriptError,
    app_python,
    info,
    repository_root,
    run_main,
)

SKILLS = {
    "ai-memory": Path("skill") / "ai-memory" / "SKILL.md",
    "graphify": Path("graphify-codebase") / "skill" / "graphify" / "SKILL.md",
}

SERVER_PATTERN = re.compile(
    r"(?ms)^\[mcp_servers(?:\.ai-memory|\.\"ai-memory\")\]\r?\n.*?(?=^\[|\Z)"
)
TOOL_PATTERN = re.compile(
    r"(?ms)^\[mcp_servers(?:\.ai-memory|\.\"ai-memory\")\.tools\.[^\]]+\]\r?\n.*?(?=^\[|\Z)"
)

SYNC_APPROVAL_BLOCK = """[mcp_servers.ai-memory.tools.memory_sync]
approval_mode = "approve"
"""


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]


def _backup(path: Path, label: str) -> None:
    if not path.is_file():
        return
    backup = path.with_name(f"{path.name}.backup-{_timestamp()}-ai-memory")
    shutil.copy2(path, backup)
    info(f"Preserved {label} backup: {backup}")


def _write(path: Path, content: str) -> bool:
    existing = path.read_text(encoding="utf-8-sig") if path.is_file() else None
    if existing == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return True


def _server_block(python: Path) -> str:
    # TOML basic strings would require escaping Windows separators, so the
    # POSIX form is used on every platform; Python accepts it as an executable.
    return (
        "[mcp_servers.ai-memory]\n"
        f"command = '{python.as_posix()}'\n"
        'args = ["-m", "ai_memory_mcp.server", "--transport", "stdio"]\n'
        "startup_timeout_sec = 120\n"
        "tool_timeout_sec = 1800\n"
    )


def _skill_metadata(source: Path, expected_name: str) -> str:
    text = source.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if match is None:
        raise ScriptError(f"Canonical skill has no valid YAML header: {source}")
    header = match.group(1)
    name = re.search(r"(?m)^name:\s*(.+?)\s*$", header)
    description = re.search(r"(?m)^description:\s*(.+?)\s*$", header)
    if not name or name.group(1) != expected_name or not description:
        raise ScriptError(f"Canonical skill metadata is invalid: {source}")
    return description.group(1)


def _install_skill(root: Path, codex_home: Path, name: str) -> None:
    source = root / SKILLS[name]
    if not source.is_file():
        raise ScriptError(f"Canonical skill is missing: {source}")
    description = _skill_metadata(source, name)
    skill_path = source.as_posix()
    stub = (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        "Before following any instruction in this stub, first check the "
        f"canonical skill header in '{skill_path}'. If the source skill "
        "metadata has changed and this stub is out of date, update this stub "
        "to match the current source skill metadata before proceeding.\n\n"
        f"Then read the SKILL.md in full from '{skill_path}'\n"
    )
    stub_path = codex_home / "skills" / name / "SKILL.md"
    if stub_path.is_file() and stub_path.read_text(encoding="utf-8-sig") != stub:
        _backup(stub_path, "skill stub")
    if _write(stub_path, stub):
        info(f"Installed the {name} discovery stub at {stub_path}")

    if name == "graphify":
        requirements = (root / "requirements-graphify.txt").read_text(
            encoding="utf-8"
        )
        match = re.search(
            r"^graphifyy(?:\[[^\]]+\])?==([^\s]+)$", requirements, re.MULTILINE
        )
        if match is None:
            raise ScriptError("Pinned Graphify version is missing.")
        _write(stub_path.with_name(".graphify_version"), f"{match.group(1)}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install AI Memory MCP into Codex."
    )
    parser.add_argument("--repository-root", type=Path, default=None)
    parser.add_argument("--codex-home", type=Path, default=None)
    args = parser.parse_args()

    root = (args.repository_root or repository_root()).resolve()
    codex_home = (args.codex_home or (Path.home() / ".codex")).expanduser()
    python = app_python(root)
    if not python.is_file():
        raise ScriptError(f"Application environment is missing: {python}")

    codex_home.mkdir(parents=True, exist_ok=True)
    config_path = codex_home / "config.toml"
    original = (
        config_path.read_text(encoding="utf-8-sig") if config_path.is_file() else ""
    )

    # Stale per-tool blocks are dropped first so the rewritten server block is
    # not followed by approval settings from an earlier layout.
    updated = TOOL_PATTERN.sub("", original)
    server_block = _server_block(python)
    if SERVER_PATTERN.search(updated):
        updated = SERVER_PATTERN.sub(lambda _: server_block + "\n", updated, count=1)
    elif updated.strip():
        updated = f"{updated.rstrip()}\n\n{server_block}"
    else:
        updated = server_block
    updated = f"{updated.rstrip()}\n\n{SYNC_APPROVAL_BLOCK}"

    if updated != original:
        _backup(config_path, "Codex config")
        _write(config_path, updated)
        info(f"Registered the repository-owned MCP in {config_path}")

    for name in SKILLS:
        _install_skill(root, codex_home, name)

    info("Restart Codex to load the updated MCP command and skill sources.")


if __name__ == "__main__":
    run_main(main)
