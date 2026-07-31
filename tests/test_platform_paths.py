from __future__ import annotations

import types
from pathlib import Path

import pytest

from ai_memory_mcp import platform_paths
from ai_memory_mcp.client_install import _client_paths


def _reload_for(monkeypatch, os_name: str, sys_platform: str):
    """Re-evaluate the module-level platform constants for a target OS."""
    monkeypatch.setattr(platform_paths, "WINDOWS", os_name == "nt")
    monkeypatch.setattr(platform_paths, "MACOS", sys_platform == "darwin")
    monkeypatch.setattr(
        platform_paths, "EXECUTABLE_SUFFIX", ".exe" if os_name == "nt" else ""
    )


def test_windows_environment_layout(monkeypatch, tmp_path: Path) -> None:
    _reload_for(monkeypatch, "nt", "win32")
    venv = tmp_path / ".venv"

    assert platform_paths.venv_bin_dir(venv) == venv / "Scripts"
    assert platform_paths.venv_python(venv) == venv / "Scripts" / "python.exe"
    assert (
        platform_paths.venv_executable(venv, "graphify-mcp")
        == venv / "Scripts" / "graphify-mcp.exe"
    )
    assert platform_paths.venv_site_packages(venv) == [
        venv / "Lib" / "site-packages"
    ]


def test_posix_environment_layout(monkeypatch, tmp_path: Path) -> None:
    _reload_for(monkeypatch, "posix", "linux")
    venv = tmp_path / ".venv"

    assert platform_paths.venv_bin_dir(venv) == venv / "bin"
    assert (
        platform_paths.venv_executable(venv, "graphify-mcp")
        == venv / "bin" / "graphify-mcp"
    )

    # Without the conventional alias the versioned interpreter is used.
    assert platform_paths.venv_python(venv) == venv / "bin" / "python3"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("", encoding="utf-8")
    assert platform_paths.venv_python(venv) == venv / "bin" / "python"


def test_posix_site_packages_is_discovered_not_assumed(
    monkeypatch, tmp_path: Path
) -> None:
    _reload_for(monkeypatch, "posix", "linux")
    venv = tmp_path / ".venv"
    expected = venv / "lib" / "python3.12" / "site-packages"
    expected.mkdir(parents=True)

    assert platform_paths.venv_site_packages(venv) == [expected]


@pytest.mark.parametrize(
    ("os_name", "sys_platform", "environment", "expected"),
    [
        ("nt", "win32", {"APPDATA": "C:/Roaming"}, Path("C:/Roaming")),
        ("posix", "darwin", {}, Path.home() / "Library" / "Application Support"),
        ("posix", "linux", {}, Path.home() / ".config"),
        ("posix", "linux", {"XDG_CONFIG_HOME": "/xdg"}, Path("/xdg")),
    ],
)
def test_user_app_data_dir_per_platform(
    monkeypatch,
    os_name: str,
    sys_platform: str,
    environment: dict[str, str],
    expected: Path,
) -> None:
    _reload_for(monkeypatch, os_name, sys_platform)
    for name in ("APPDATA", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    assert platform_paths.user_app_data_dir() == expected


def test_path_key_uses_filesystem_identity_for_existing_paths(
    tmp_path: Path,
) -> None:
    """Two names for one directory must share a key, whatever the platform."""
    target = tmp_path / "Memory"
    target.mkdir()
    other = tmp_path / "Other"
    other.mkdir()

    # A second route to the same directory resolves to the same identity.
    (tmp_path / "sub").mkdir()
    indirect = tmp_path / "sub" / ".." / "Memory"
    assert platform_paths.path_key(target) == platform_paths.path_key(indirect)

    # Genuinely distinct directories never collide.
    assert platform_paths.path_key(target) != platform_paths.path_key(other)

    # Whether "memory" matches "Memory" is decided by the volume, not the OS
    # name, so assert agreement with the filesystem rather than a fixed answer.
    lowered = tmp_path / "memory"
    same_key = platform_paths.path_key(target) == platform_paths.path_key(lowered)
    assert same_key == lowered.exists()


def test_path_key_falls_back_for_paths_that_do_not_exist(
    monkeypatch, tmp_path: Path
) -> None:
    missing = tmp_path / "absent"

    _reload_for(monkeypatch, "posix", "linux")
    assert platform_paths.path_key(missing) != platform_paths.path_key(
        tmp_path / "ABSENT"
    )

    _reload_for(monkeypatch, "nt", "win32")
    assert platform_paths.path_key(missing) == platform_paths.path_key(
        tmp_path / "ABSENT"
    )


def test_client_config_layout_is_shared_across_platforms(tmp_path: Path) -> None:
    """The same relative layout must hold under each platform's data root."""
    home = tmp_path / "home"
    for app_data in (
        home / "AppData" / "Roaming",
        home / "Library" / "Application Support",
        home / ".config",
    ):
        paths = _client_paths(home, app_data)
        assert (
            paths["claude-desktop"]
            == app_data / "Claude" / "claude_desktop_config.json"
        )
        assert paths["vscode"] == app_data / "Code" / "User" / "mcp.json"
        # Home-relative clients stay identical on every platform.
        assert paths["claude-code"] == home / ".claude.json"
        assert paths["copilot"] == home / ".copilot" / "mcp-config.json"


def test_module_exposes_only_helpers_that_are_used(tmp_path: Path) -> None:
    """Guard against the shared module accumulating unused platform helpers."""
    public = {
        name
        for name, value in vars(platform_paths).items()
        if not name.startswith("_")
        and isinstance(value, types.FunctionType)
        and value.__module__ == platform_paths.__name__
    }
    assert public == {
        "venv_bin_dir",
        "venv_python",
        "venv_executable",
        "venv_site_packages",
        "user_app_data_dir",
        "path_key",
    }
