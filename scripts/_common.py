"""Shared helpers for the cross-platform maintenance scripts.

These scripts run before ``ai_memory_mcp`` is installed, so this module must
stay importable with only the standard library and must not import the package.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

WINDOWS = os.name == "nt"
MACOS = sys.platform == "darwin"

EXECUTABLE_SUFFIX = ".exe" if WINDOWS else ""

SOURCE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")

DEFAULT_MCP_URL = "http://127.0.0.1:4324/mcp"


class ScriptError(RuntimeError):
    """A failure that should be reported without a traceback."""


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def venv_bin_dir(venv_root: Path) -> Path:
    return venv_root / ("Scripts" if WINDOWS else "bin")


def venv_python(venv_root: Path) -> Path:
    bin_dir = venv_bin_dir(venv_root)
    if WINDOWS:
        return bin_dir / "python.exe"
    interpreter = bin_dir / "python"
    return interpreter if interpreter.exists() else bin_dir / "python3"


def venv_executable(venv_root: Path, name: str) -> Path:
    return venv_bin_dir(venv_root) / f"{name}{EXECUTABLE_SUFFIX}"


def app_python(root: Path | None = None) -> Path:
    return venv_python((root or repository_root()) / ".venv")


def graphify_runtime_root(root: Path | None = None) -> Path:
    return (root or repository_root()) / ".graphify-runtime"


def graphify_python(root: Path | None = None) -> Path:
    configured = os.environ.get("AI_MEMORY_GRAPHIFY_PYTHON", "").strip()
    if configured:
        return Path(configured)
    return venv_python(graphify_runtime_root(root))


def graphify_executable(root: Path | None = None) -> Path:
    return venv_executable(graphify_runtime_root(root), "graphify")


def graphify_mcp_executable(root: Path | None = None) -> Path:
    configured = os.environ.get("AI_MEMORY_GRAPHIFY_MCP_EXE", "").strip()
    if configured:
        return Path(configured)
    return venv_executable(graphify_runtime_root(root), "graphify-mcp")


def load_environment(root: Path | None = None) -> None:
    """Load repository ``.env`` values without overriding explicit variables.

    Launch-time environment variables are intentional overrides, so an existing
    value always wins over the file.
    """
    env_path = (root or repository_root()) / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if not name or not name.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if not os.environ.get(name, "").strip():
            os.environ[name] = value


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser()


def graphify_state_root() -> Path:
    configured = os.environ.get("AI_MEMORY_GRAPHIFY_STATE_DIR", "").strip()
    if configured:
        return expand_path(configured)
    return Path.home() / ".graphify"


def path_key(path: Path) -> str:
    """Return a key that is equal for two paths naming the same directory.

    Mirrors ``ai_memory_mcp.platform_paths.path_key``. Case sensitivity belongs
    to the volume rather than the operating system — macOS can be formatted
    case-sensitive — so an existing path is identified by its filesystem
    identity, and only a path that does not exist falls back to the host's
    usual convention.
    """
    resolved = path.resolve()
    try:
        status = resolved.stat()
    except OSError:
        text = str(resolved)
        return text.casefold() if (WINDOWS or MACOS) else text
    return f"id:{status.st_dev}:{status.st_ino}"


def mcp_url() -> str:
    return os.environ.get("GRAPHIFY_GLOBAL_MCP_URL", "").strip() or DEFAULT_MCP_URL


@dataclass(frozen=True)
class MemorySource:
    source_id: str
    root: Path
    writable: bool


def memory_sources() -> list[MemorySource]:
    """Resolve every configured memory source, mirroring ``config.Settings``."""
    primary_root = (
        os.environ.get("AI_MEMORY_WORK_DIR", "").strip()
        or os.environ.get("AI_MEMORY_DIR", "").strip()
    )
    if not primary_root:
        raise ScriptError("AI_MEMORY_WORK_DIR is not set.")

    primary_id = (
        os.environ.get("AI_MEMORY_PRIMARY_SOURCE_ID", "").strip().casefold()
        or "core"
    )
    configured: dict[str, str] = {primary_id: primary_root}

    raw = os.environ.get("AI_MEMORY_RETRIEVAL_SOURCES", "").strip()
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ScriptError(
                "AI_MEMORY_RETRIEVAL_SOURCES must be a JSON object."
            ) from exc
        if not isinstance(payload, dict):
            raise ScriptError(
                "AI_MEMORY_RETRIEVAL_SOURCES must map source IDs to directories."
            )
        for key, value in payload.items():
            source_id = str(key).strip().casefold()
            if source_id in configured:
                raise ScriptError(f"Duplicate memory source ID: {source_id}")
            configured[source_id] = str(value)

    personal = os.environ.get("AI_MEMORY_PERSONAL_DIR", "").strip()
    if personal and "personal" not in configured:
        configured["personal"] = personal

    sources: list[MemorySource] = []
    seen_roots: dict[str, str] = {}
    for source_id, value in configured.items():
        if not SOURCE_ID_PATTERN.fullmatch(source_id):
            raise ScriptError(f"Invalid memory source ID: {source_id}")
        if not value.strip():
            raise ScriptError(f"Memory source '{source_id}' has no directory.")
        root = expand_path(value)
        if not root.is_dir():
            raise ScriptError(f"Memory source directory is missing: {root}")
        key = path_key(root)
        if key in seen_roots:
            raise ScriptError(f"Duplicate memory source directory: {root}")
        seen_roots[key] = source_id
        sources.append(
            MemorySource(
                source_id=source_id,
                root=root.resolve(),
                writable=source_id == primary_id,
            )
        )
    return sources


def info(message: str) -> None:
    print(message, flush=True)


def run_main(entry) -> None:
    """Run one script entry point, reporting expected failures without a trace."""
    try:
        entry()
    except ScriptError as error:
        print(f"error: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from error
