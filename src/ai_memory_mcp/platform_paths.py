"""Resolve interpreter, executable, and per-user directories for each host OS.

Every platform-conditional path in this project funnels through this module so
that Windows, macOS, and Linux differences live in exactly one place instead of
being rediscovered at each call site.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

WINDOWS = os.name == "nt"
MACOS = sys.platform == "darwin"

# Console scripts carry an extension only on Windows.
EXECUTABLE_SUFFIX = ".exe" if WINDOWS else ""


def venv_bin_dir(venv_root: Path) -> Path:
    """Return the directory holding one environment's console scripts."""
    return venv_root / ("Scripts" if WINDOWS else "bin")


def venv_python(venv_root: Path) -> Path:
    """Return the interpreter of one environment.

    POSIX environments always provide ``python3``; the unsuffixed ``python``
    symlink is conventional but omitted by some distribution builds, so it is
    only used when it actually exists.
    """
    bin_dir = venv_bin_dir(venv_root)
    if WINDOWS:
        return bin_dir / "python.exe"
    interpreter = bin_dir / "python"
    return interpreter if interpreter.exists() else bin_dir / "python3"


def venv_executable(venv_root: Path, name: str) -> Path:
    """Return one console script of an environment with the platform suffix."""
    return venv_bin_dir(venv_root) / f"{name}{EXECUTABLE_SUFFIX}"


def venv_site_packages(venv_root: Path) -> list[Path]:
    """Return every ``site-packages`` directory of one environment.

    Windows uses a single version-independent ``Lib`` directory. POSIX nests
    ``site-packages`` under a version-specific directory, so the layout is
    discovered rather than assumed.
    """
    if WINDOWS:
        return [venv_root / "Lib" / "site-packages"]
    return sorted((venv_root / "lib").glob("python*/site-packages"))


def subprocess_python(
    environment: Mapping[str, str] | None = None,
) -> tuple[str, dict[str, str] | None]:
    """Return a Python command whose PID belongs to the supervised interpreter.

    Some Windows venv launchers create a second interpreter process. The
    launcher PID cannot identify worker leases or reliably stop that child.
    """
    inherited = dict(os.environ if environment is None else environment)
    if not WINDOWS or sys.prefix == sys.base_prefix:
        return sys.executable, inherited if environment is not None else None

    base_executable = getattr(sys, "_base_executable", None)
    site_packages = venv_site_packages(Path(sys.prefix))
    if (
        not base_executable
        or not Path(base_executable).is_file()
        or not site_packages
    ):
        return sys.executable, inherited if environment is not None else None

    # The base interpreter does not read the venv configuration, so expose the
    # editable source and venv dependencies explicitly before ambient paths.
    paths = [
        str(Path(__file__).resolve().parents[1]),
        *(str(path) for path in site_packages),
        inherited.get("PYTHONPATH", ""),
    ]
    inherited["PYTHONPATH"] = os.pathsep.join(path for path in paths if path)
    inherited["PYTHONNOUSERSITE"] = "1"
    inherited["VIRTUAL_ENV"] = str(Path(sys.prefix))
    inherited.pop("PYTHONHOME", None)
    return str(base_executable), inherited


def user_app_data_dir() -> Path:
    """Return the per-user application data directory of the host platform.

    Claude Desktop and VS Code both store per-user configuration beneath this
    directory using the same relative layout on every OS, so callers only need
    to vary the root.
    """
    home = Path.home()
    if WINDOWS:
        configured = os.environ.get("APPDATA")
        return Path(configured) if configured else home / "AppData" / "Roaming"
    if MACOS:
        return home / "Library" / "Application Support"
    configured = os.environ.get("XDG_CONFIG_HOME")
    return Path(configured) if configured else home / ".config"


def path_key(path: Path) -> str:
    """Return a key that is equal for two paths naming the same directory.

    For a path that exists the key is the filesystem's own identity for it
    (device plus inode). That is authoritative regardless of platform, which
    matters because case sensitivity is a property of the volume rather than
    the operating system: macOS can be formatted case-sensitive, and an
    external volume mounted on Linux can be case-insensitive.

    A path that does not exist yet has no identity to read, so it falls back to
    the host's usual convention.
    """
    resolved = path.resolve()
    try:
        status = resolved.stat()
    except OSError:
        text = str(resolved)
        return text.casefold() if (WINDOWS or MACOS) else text
    return f"id:{status.st_dev}:{status.st_ino}"
