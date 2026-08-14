"""Install the repository-owned MCP server and skills into supported clients."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from .platform_paths import user_app_data_dir, venv_python


SUPPORTED_CLIENTS = (
    "claude-code",
    "claude-desktop",
    "copilot",
    "opencode",
    "vscode",
    "agent-skills",
)

SKILLS = {
    "ai-memory": Path("skill") / "ai-memory" / "SKILL.md",
    "graphify": Path("graphify-codebase") / "skill" / "graphify" / "SKILL.md",
}


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
    python = venv_python(repository_root / ".venv")
    if not python.is_file():
        raise FileNotFoundError(
            f"Application environment is missing: {python}. Run scripts/setup.py."
        )
    args = ["-m", "ai_memory_mcp.server", "--transport", "stdio"]
    return str(python), args


def _skill_stub(repository_root: Path, skill_name: str) -> str:
    relative = SKILLS[skill_name]
    canonical_text = (repository_root / relative).read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", canonical_text, re.DOTALL)
    if match is None:
        raise ValueError(f"Canonical skill has no valid YAML header: {relative}")
    metadata = dict(
        line.split(":", 1)
        for line in match.group(1).splitlines()
        if ":" in line
    )
    canonical_name = metadata.get("name", "").strip()
    description = metadata.get("description", "").strip()
    if canonical_name != skill_name or not description:
        raise ValueError(f"Canonical skill metadata is invalid: {relative}")
    source = (repository_root / relative).as_posix()
    # A stub must also tell the agent to refresh itself when the canonical
    # metadata moves on, otherwise a stale description silently stops the host
    # from triggering the skill. This matches the stub Codex receives.
    return (
        "---\n"
        f"name: {skill_name}\n"
        f"description: {description}\n"
        "---\n\n"
        "Before following any instruction in this stub, first check the "
        f"canonical skill header in '{source}'. If the source skill metadata "
        "has changed and this stub is out of date, update this stub to match "
        "the current source skill metadata before proceeding.\n\n"
        f"Then read the SKILL.md in full from '{source}'\n"
    )


def _install_skill(
    repository_root: Path,
    destination_root: Path,
    skill_name: str,
) -> list[Path]:
    relative = SKILLS[skill_name]
    source = repository_root / relative
    if not source.is_file():
        raise FileNotFoundError(f"Canonical skill is missing: {source}")
    destination = destination_root / skill_name / "SKILL.md"
    changed = [
        path
        for path in (
            _write_text(
                destination,
                _skill_stub(repository_root, skill_name),
            ),
        )
        if path is not None
    ]
    if skill_name == "graphify":
        requirements = (
            repository_root / "requirements-graphify.txt"
        ).read_text(encoding="utf-8")
        match = re.search(
            r"^graphifyy(?:\[[^\]]+\])?==([^\s]+)$",
            requirements,
            re.MULTILINE,
        )
        if match is None:
            raise ValueError("Pinned Graphify version is missing.")
        marker = _write_text(
            destination.with_name(".graphify_version"),
            f"{match.group(1)}\n",
        )
        if marker is not None:
            changed.append(marker)
    return changed


def _install_skills(
    repository_root: Path,
    destination_root: Path,
) -> list[Path]:
    changed: list[Path] = []
    for skill_name in SKILLS:
        changed.extend(
            _install_skill(
                repository_root,
                destination_root,
                skill_name,
            )
        )
    return changed


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
    """Map each client to its configuration file.

    ``appdata`` is the host's per-user application data root (see
    ``platform_paths.user_app_data_dir``). Claude Desktop and VS Code keep the
    same relative layout beneath it on Windows, macOS, and Linux, so passing the
    correct root is all that platform support requires here.
    """
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
        changed.extend(
            _install_skills(repository_root, home / ".agents" / "skills")
        )
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

    skill_roots = {
        "claude-code": home / ".claude" / "skills",
        "copilot": home / ".copilot" / "skills",
        "opencode": home / ".config" / "opencode" / "skills",
        # VS Code discovers personal skills from the Copilot and Agents locations.
        "vscode": home / ".copilot" / "skills",
    }
    skill_root = skill_roots.get(client)
    if skill_root:
        changed.extend(_install_skills(repository_root, skill_root))
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
    # Claude Desktop and VS Code use the same relative layout under each
    # platform's per-user application data root, so only the root varies.
    appdata = user_app_data_dir()
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
