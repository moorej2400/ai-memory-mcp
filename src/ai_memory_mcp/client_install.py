"""Install the repository-owned MCP server and skill into supported clients."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any


SUPPORTED_CLIENTS = (
    "claude-code",
    "claude-desktop",
    "copilot",
    "opencode",
    "vscode",
    "agent-skills",
)

SKILL_DESCRIPTION = (
    "Use when meaningful work produces durable knowledge that future agents "
    "should retain, when the user asks to remember or recall something, or "
    "when Graphify-backed Markdown memory needs retrieval, organization, "
    "consolidation, conflict handling, session or handoff capture, or refresh. "
    "Invoke automatically during substantive work and before completion; do "
    "not wait for the user to ask."
)


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _strip_jsonc(text: str) -> str:
    """Remove JSONC comments without treating comment markers in strings as syntax."""
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == "/" and next_char == "/":
            output.extend((" ", " "))
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                output.append(" ")
                index += 1
            continue
        if char == "/" and next_char == "*":
            output.extend((" ", " "))
            index += 2
            while index < len(text):
                if (
                    text[index] == "*"
                    and index + 1 < len(text)
                    and text[index + 1] == "/"
                ):
                    output.extend((" ", " "))
                    index += 2
                    break
                output.append("\n" if text[index] == "\n" else " ")
                index += 1
            continue
        output.append(char)
        index += 1
    return _remove_trailing_commas("".join(output))


def _remove_trailing_commas(text: str) -> str:
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "}]":
                index += 1
                continue
        output.append(char)
        index += 1
    return "".join(output)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        return {}
    data = json.loads(_strip_jsonc(text))
    if not isinstance(data, dict):
        raise ValueError(f"Configuration root must be an object: {path}")
    return data


def _backup(path: Path) -> Path | None:
    if not path.is_file():
        return None
    backup = path.with_name(f"{path.name}.backup-{_timestamp()}-ai-memory")
    shutil.copy2(path, backup)
    return backup


def _write_text(path: Path, content: str) -> Path | None:
    existing = path.read_text(encoding="utf-8-sig") if path.is_file() else None
    if existing == content:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup(path)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _write_json(path: Path, data: dict[str, Any]) -> Path | None:
    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    return _write_text(path, content)


def _set_nested(
    data: dict[str, Any],
    keys: tuple[str, ...],
    name: str,
    value: dict[str, Any],
) -> bool:
    node = data
    for key in keys:
        child = node.get(key)
        if child is None:
            child = {}
            node[key] = child
        if not isinstance(child, dict):
            raise ValueError(f"Configuration key must be an object: {'.'.join(keys)}")
        node = child
    if node.get(name) == value:
        return False
    node[name] = value
    return True


def _stdio_parts(repository_root: Path) -> tuple[str, list[str]]:
    python = repository_root / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        raise FileNotFoundError(
            f"Application environment is missing: {python}. Run scripts/setup.ps1."
        )
    args = ["-m", "ai_memory_mcp.server", "--transport", "stdio"]
    return str(python), args


def _skill_stub(repository_root: Path) -> str:
    source = (
        repository_root / "skill" / "ai-memory" / "SKILL.md"
    ).as_posix()
    return (
        "---\n"
        "name: ai-memory\n"
        f"description: {SKILL_DESCRIPTION}\n"
        "---\n\n"
        "Before following this stub, read the canonical `SKILL.md` in full from "
        f"`{source}`.\n"
    )


def _install_skill(repository_root: Path, destination: Path) -> Path | None:
    source = repository_root / "skill" / "ai-memory" / "SKILL.md"
    if not source.is_file():
        raise FileNotFoundError(f"Canonical AI Memory skill is missing: {source}")
    return _write_text(destination, _skill_stub(repository_root))


def _opencode_major() -> int | None:
    executable = shutil.which("opencode2") or shutil.which("opencode")
    if not executable:
        return None
    result = subprocess.run(
        [executable, "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    match = re.search(r"(\d+)\.", result.stdout)
    return int(match.group(1)) if match else None


def _opencode_container(
    data: dict[str, Any],
    major_version: int | None,
) -> tuple[str, ...]:
    # OpenCode v1 stores servers under `mcp`; the separate v2 CLI uses `mcp.servers`.
    mcp = data.get("mcp")
    if isinstance(mcp, dict):
        if isinstance(mcp.get("servers"), dict):
            return ("mcp", "servers")
        if any(
            isinstance(value, dict) and ("command" in value or "url" in value)
            for value in mcp.values()
        ):
            return ("mcp",)
    return ("mcp", "servers") if major_version and major_version >= 2 else ("mcp",)


def _client_paths(home: Path, appdata: Path) -> dict[str, Path]:
    opencode_root = home / ".config" / "opencode"
    opencode_config = opencode_root / "opencode.jsonc"
    if not opencode_config.exists():
        opencode_config = opencode_root / "opencode.json"
    return {
        "claude-code": home / ".claude.json",
        "claude-desktop": appdata / "Claude" / "claude_desktop_config.json",
        "copilot": home / ".copilot" / "mcp-config.json",
        "opencode": opencode_config,
        "vscode": appdata / "Code" / "User" / "mcp.json",
    }


def install_client(
    client: str,
    repository_root: Path,
    home: Path,
    appdata: Path,
    opencode_major: int | None = None,
) -> list[Path]:
    command, args = _stdio_parts(repository_root)
    paths = _client_paths(home, appdata)
    changed: list[Path] = []

    if client == "agent-skills":
        skill = home / ".agents" / "skills" / "ai-memory" / "SKILL.md"
        if _install_skill(repository_root, skill) is not None:
            changed.append(skill)
        return changed

    path = paths[client]
    data = _load_json(path)
    before = deepcopy(data)

    if client in {"claude-code", "claude-desktop"}:
        entry = {"type": "stdio", "command": command, "args": args, "env": {}}
        _set_nested(data, ("mcpServers",), "ai-memory", entry)
    elif client == "copilot":
        entry = {
            "type": "local",
            "command": command,
            "args": args,
            "env": {},
            "tools": ["*"],
        }
        _set_nested(data, ("mcpServers",), "ai-memory", entry)
    elif client == "opencode":
        entry = {
            "type": "local",
            "command": [command, *args],
            "enabled": True,
        }
        container = _opencode_container(data, opencode_major)
        _set_nested(data, container, "ai-memory", entry)
    elif client == "vscode":
        entry = {"type": "stdio", "command": command, "args": args}
        _set_nested(data, ("servers",), "ai-memory", entry)
    else:
        raise ValueError(f"Unsupported client: {client}")

    if data != before:
        _write_json(path, data)
        changed.append(path)

    if client == "vscode":
        settings_path = appdata / "Code" / "User" / "settings.json"
        settings = _load_json(settings_path)
        if settings.get("github.copilot.chat.skillTool.enabled") is not True:
            settings["github.copilot.chat.skillTool.enabled"] = True
            _write_json(settings_path, settings)
            changed.append(settings_path)

    skill_paths = {
        "claude-code": home / ".claude" / "skills" / "ai-memory" / "SKILL.md",
        "copilot": home / ".copilot" / "skills" / "ai-memory" / "SKILL.md",
        "opencode": (
            home / ".config" / "opencode" / "skills" / "ai-memory" / "SKILL.md"
        ),
        # VS Code discovers personal skills from the Copilot and Agents locations.
        "vscode": home / ".copilot" / "skills" / "ai-memory" / "SKILL.md",
    }
    skill = skill_paths.get(client)
    if skill and _install_skill(repository_root, skill) is not None:
        changed.append(skill)
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install AI Memory MCP into supported clients."
    )
    parser.add_argument("--repository-root", required=True, type=Path)
    parser.add_argument(
        "--client",
        action="append",
        choices=SUPPORTED_CLIENTS,
        dest="clients",
    )
    args = parser.parse_args()

    repository_root = args.repository_root.resolve()
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    clients = args.clients or list(SUPPORTED_CLIENTS)
    opencode_major = _opencode_major() if "opencode" in clients else None

    for client in clients:
        changed = install_client(
            client,
            repository_root,
            home,
            appdata,
            opencode_major,
        )
        state = "updated" if changed else "current"
        print(f"{client}: {state}")


if __name__ == "__main__":
    main()
