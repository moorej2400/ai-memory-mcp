from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_memory_mcp.client_install import (
    _opencode_container,
    _strip_jsonc,
    install_client,
)
from ai_memory_mcp.platform_paths import WINDOWS, venv_bin_dir, venv_python


@pytest.fixture
def portable_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "ai-memory-mcp"
    skill = repository / "skill" / "ai-memory" / "SKILL.md"
    graphify_skill = (
        repository
        / "graphify-codebase"
        / "skill"
        / "graphify"
        / "SKILL.md"
    )
    # Mirror the environment layout of the host platform, including the
    # unsuffixed POSIX alias a real venv creates, so the fixture exercises the
    # same interpreter path the installer resolves.
    bin_dir = venv_bin_dir(repository / ".venv")
    bin_dir.mkdir(parents=True)
    for name in ("python.exe",) if WINDOWS else ("python", "python3"):
        (bin_dir / name).write_text("", encoding="utf-8")
    (repository / "requirements-graphify.txt").write_text(
        "graphifyy[openai,mcp]==0.9.26\n",
        encoding="utf-8",
    )
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: ai-memory\ndescription: Test AI Memory skill.\n---\n",
        encoding="utf-8",
    )
    graphify_skill.parent.mkdir(parents=True)
    graphify_skill.write_text(
        "---\nname: graphify\ndescription: Test Graphify skill.\n---\n",
        encoding="utf-8",
    )
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
    # The registered interpreter must be the host platform's venv layout, not a
    # hard-coded Windows path.
    assert data[container]["ai-memory"]["command"] == str(
        venv_python(portable_repository / ".venv")
    )
    assert path in changed
    assert list(path.parent.glob(f"{path.name}.backup-*-ai-memory"))
    if client == "vscode":
        settings_path = appdata / "Code" / "User" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        assert settings["github.copilot.chat.skillTool.enabled"] is True
        assert settings_path in changed
    skill_roots = {
        "claude-code": home / ".claude" / "skills",
        "copilot": home / ".copilot" / "skills",
        "vscode": home / ".copilot" / "skills",
    }
    if skill_root := skill_roots.get(client):
        assert (skill_root / "ai-memory" / "SKILL.md").is_file()
        graphify_stub = (
            skill_root / "graphify" / "SKILL.md"
        ).read_text(encoding="utf-8")
        assert "graphify-codebase/skill/graphify/SKILL.md" in graphify_stub
        assert (
            skill_root / "graphify" / ".graphify_version"
        ).read_text(encoding="utf-8") == "0.9.26\n"


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
    graphify_stub = (
        home
        / ".config"
        / "opencode"
        / "skills"
        / "graphify"
        / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "graphify-codebase/skill/graphify/SKILL.md" in graphify_stub


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
    graphify = home / ".agents" / "skills" / "graphify" / "SKILL.md"
    graphify_version = (
        home / ".agents" / "skills" / "graphify" / ".graphify_version"
    )
    assert changed == [skill, graphify, graphify_version]
    assert skill.is_file()
    assert graphify.is_file()


def test_skill_stub_follows_the_documented_contract(
    tmp_path: Path,
    portable_repository: Path,
) -> None:
    """A stub must carry discovery metadata and keep itself current.

    A stub whose description drifts from the canonical skill stops the host
    triggering it, so the self-update instruction is part of the contract
    rather than a nicety.
    """
    home = tmp_path / "home"
    install_client(
        "agent-skills", portable_repository, home, home / "AppData" / "Roaming"
    )
    stub = (home / ".agents" / "skills" / "ai-memory" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    canonical = portable_repository / "skill" / "ai-memory" / "SKILL.md"

    # Discovery metadata, copied exactly from the canonical skill.
    assert stub.startswith("---\n")
    assert "name: ai-memory\n" in stub
    assert "description: Test AI Memory skill.\n" in stub

    # Redirects to the canonical source rather than copying its body.
    assert canonical.as_posix() in stub
    assert "read the SKILL.md in full" in stub

    # Tells the agent to refresh the stub when the canonical metadata moves on.
    assert "check the canonical skill header" in stub
    assert "update this stub" in stub


def test_jsonc_parser_keeps_comment_markers_inside_strings() -> None:
    parsed = json.loads(
        _strip_jsonc(
            '{"url":"https://example.test/a//b",/* comment */"items":[1,],}'
        )
    )
    assert parsed == {"url": "https://example.test/a//b", "items": [1]}
