from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_memory_mcp.client_install import (
    _opencode_container,
    _strip_jsonc,
    install_client,
)


@pytest.fixture
def portable_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "ai-memory-mcp"
    python = repository / ".venv" / "Scripts" / "python.exe"
    skill = repository / "skill" / "ai-memory" / "SKILL.md"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: ai-memory\n---\n", encoding="utf-8")
    return repository


@pytest.mark.parametrize(
    ("client", "config_path", "container"),
    [
        ("claude-code", ".claude.json", "mcpServers"),
        (
            "claude-desktop",
            "AppData/Roaming/Claude/claude_desktop_config.json",
            "mcpServers",
        ),
        ("copilot", ".copilot/mcp-config.json", "mcpServers"),
        ("vscode", "AppData/Roaming/Code/User/mcp.json", "servers"),
    ],
)
def test_installs_json_client_and_preserves_existing_servers(
    tmp_path: Path,
    portable_repository: Path,
    client: str,
    config_path: str,
    container: str,
) -> None:
    home = tmp_path / "home"
    appdata = home / "AppData" / "Roaming"
    path = home / config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({container: {"existing": {"command": "existing"}}}),
        encoding="utf-8",
    )

    changed = install_client(client, portable_repository, home, appdata)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data[container]["existing"]["command"] == "existing"
    assert data[container]["ai-memory"]["args"][-2:] == ["--transport", "stdio"]
    assert path in changed
    assert list(path.parent.glob(f"{path.name}.backup-*-ai-memory"))
    if client == "vscode":
        settings_path = appdata / "Code" / "User" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        assert settings["github.copilot.chat.skillTool.enabled"] is True
        assert settings_path in changed


def test_installs_opencode_v1_and_repo_linked_skill(
    tmp_path: Path,
    portable_repository: Path,
) -> None:
    home = tmp_path / "home"
    appdata = home / "AppData" / "Roaming"
    path = home / ".config" / "opencode" / "opencode.jsonc"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{\n  // Keep this server.\n  "mcp": {\n'
        '    "existing": {"type": "remote", "url": "https://example.test"},\n'
        "  },\n}\n",
        encoding="utf-8",
    )

    install_client(
        "opencode",
        portable_repository,
        home,
        appdata,
        opencode_major=1,
    )

    data = json.loads(path.read_text(encoding="utf-8"))
    assert "existing" in data["mcp"]
    assert data["mcp"]["ai-memory"]["command"][1:3] == [
        "-m",
        "ai_memory_mcp.server",
    ]
    stub = (
        home / ".config" / "opencode" / "skills" / "ai-memory" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert portable_repository.as_posix() in stub


def test_opencode_v2_uses_servers_container() -> None:
    assert _opencode_container({}, 2) == ("mcp", "servers")
    assert _opencode_container({"mcp": {"servers": {}}}, 1) == (
        "mcp",
        "servers",
    )


def test_new_shared_skill_reports_a_change(
    tmp_path: Path,
    portable_repository: Path,
) -> None:
    home = tmp_path / "home"

    changed = install_client(
        "agent-skills",
        portable_repository,
        home,
        home / "AppData" / "Roaming",
    )

    skill = home / ".agents" / "skills" / "ai-memory" / "SKILL.md"
    assert changed == [skill]
    assert skill.is_file()


def test_jsonc_parser_keeps_comment_markers_inside_strings() -> None:
    parsed = json.loads(
        _strip_jsonc(
            '{"url":"https://example.test/a//b",/* comment */"items":[1,],}'
        )
    )
    assert parsed == {"url": "https://example.test/a//b", "items": [1]}
