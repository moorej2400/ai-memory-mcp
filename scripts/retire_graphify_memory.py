#!/usr/bin/env python3
"""Retire the Graphify services that earlier AI Memory releases installed.

AI Memory now builds and reads its note graph natively. An upgraded machine can
still have the old Graphify global MCP listener, its login launcher, and the
old provider state. This script finds those items. With ``--apply`` it stops
the listener and moves each file to a recoverable archive. It never deletes a
file, and it never changes the pinned runtime that Graphify Codebase uses.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    MACOS,
    WINDOWS,
    ScriptError,
    expand_path,
    info,
    load_environment,
    repository_root,
    run_main,
)
from _processes import find_processes, terminate_tree  # noqa: E402

LAUNCHER_LABEL = "graphify-global-mcp"
# Only a launcher that starts the AI Memory listener script is retired.
LAUNCHER_MARKERS = ("start_global_mcp", "start-graphify-global-mcp")


@dataclass(frozen=True)
class Launcher:
    path: Path
    kind: str


def memory_root() -> Path:
    configured = (
        os.environ.get("AI_MEMORY_WORK_DIR", "").strip()
        or os.environ.get("AI_MEMORY_DIR", "").strip()
    )
    if not configured:
        raise ScriptError("AI_MEMORY_WORK_DIR is not set.")
    return expand_path(configured)


def legacy_state_root(root: Path) -> Path:
    configured = os.environ.get("AI_MEMORY_GRAPHIFY_STATE_DIR", "").strip()
    if configured:
        return expand_path(configured)
    return root / ".ai-memory" / "provider-state" / "graphify"


def launcher_candidates(home: Path) -> list[Launcher]:
    if WINDOWS:
        appdata = os.environ.get("APPDATA")
        roaming = Path(appdata) if appdata else home / "AppData" / "Roaming"
        startup = (
            roaming / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        )
        return [Launcher(startup / f"{LAUNCHER_LABEL}-start.vbs", "windows-startup")]
    if MACOS:
        return [
            Launcher(
                home / "Library" / "LaunchAgents" / f"com.{LAUNCHER_LABEL}.plist",
                "launchd",
            )
        ]
    return [
        Launcher(
            home / ".config" / "systemd" / "user" / f"{LAUNCHER_LABEL}.service",
            "systemd",
        ),
        Launcher(
            home / ".config" / "autostart" / f"{LAUNCHER_LABEL}.desktop",
            "xdg-autostart",
        ),
    ]


def installed_launchers(home: Path) -> list[Launcher]:
    found: list[Launcher] = []
    for launcher in launcher_candidates(home):
        if not launcher.path.is_file():
            continue
        text = launcher.path.read_text(encoding="utf-8", errors="replace")
        if any(marker in text for marker in LAUNCHER_MARKERS):
            found.append(launcher)
    return found


def unregister(launcher: Launcher) -> None:
    """Stop the service manager from using a launcher before it moves."""
    if launcher.kind == "launchd":
        subprocess.run(
            ["launchctl", "unload", str(launcher.path)],
            check=False,
            capture_output=True,
        )
    elif launcher.kind == "systemd" and shutil.which("systemctl"):
        subprocess.run(
            ["systemctl", "--user", "disable", launcher.path.name],
            check=False,
            capture_output=True,
        )


def native_generation_available() -> bool:
    """Report whether recall can use a native graph without the old state."""
    sys.path.insert(0, str(repository_root() / "src"))
    try:
        from ai_memory_mcp.config import Settings
        from ai_memory_mcp.generation import load_current_generation
    except ImportError as exc:
        raise ScriptError(
            "The AI Memory package is not installed. Run scripts/setup.py."
        ) from exc
    return load_current_generation(Settings.from_env()) is not None


def legacy_environment_keys(root: Path) -> list[str]:
    env_path = root / ".env"
    if not env_path.is_file():
        return []
    keys: list[str] = []
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        name = line.split("=", 1)[0].strip()
        if "=" in line and name.startswith("GRAPHIFY_"):
            keys.append(name)
    return keys


def move_to_archive(source: Path, archive: Path) -> Path:
    destination = archive / source.name
    if destination.exists():
        raise ScriptError(f"Archive destination already exists: {destination}")
    archive.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    return destination


def main(argv: list[str] | None = None, *, home: Path | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Find the Graphify services from earlier AI Memory releases. "
            "Use --apply to stop them and archive their files."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Stop the listener and move each item to the archive.",
    )
    args = parser.parse_args(argv)

    repository = repository_root()
    load_environment(repository)
    root = memory_root()
    state = legacy_state_root(root)
    graph = state / "global-graph.json"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    archive = root / ".ai-memory" / "backups" / "graphify-retirement" / stamp
    mode = "apply" if args.apply else "dry run"
    info(f"Graphify retirement ({mode}). Archive: {archive}")

    processes = find_processes("graphify-mcp", str(graph))
    launchers = installed_launchers(home or Path.home())
    state_present = state.is_dir()
    generation_ready = native_generation_available() if state_present else True

    for process in processes:
        info(f"Listener: PID {process.pid}")
        if args.apply:
            terminate_tree(process.pid)
            info(f"  stopped PID {process.pid}")
    if not processes:
        info("Listener: none found.")

    for launcher in launchers:
        info(f"Launcher: {launcher.path}")
        if args.apply:
            unregister(launcher)
            moved = move_to_archive(launcher.path, archive / "launchers")
            info(f"  moved to {moved}")
    if not launchers:
        info("Launcher: none found.")

    if state_present and not generation_ready:
        info(
            f"Legacy state: {state} (kept). Run ai-memory-sync first. "
            "Recall reads this state until a native generation exists."
        )
    elif state_present:
        info(f"Legacy state: {state}")
        if args.apply:
            moved = move_to_archive(state, archive / "provider-state")
            info(f"  moved to {moved}")
    else:
        info("Legacy state: none found.")

    keys = legacy_environment_keys(repository)
    if keys:
        info(
            "AI Memory does not read these .env keys: "
            + ", ".join(keys)
            + ". Remove them if no other tool uses them."
        )
    runtime = repository / ".graphify-runtime"
    if runtime.is_dir():
        info(f"Kept {runtime}. Graphify Codebase can use this runtime.")
    if not args.apply and (processes or launchers or state_present):
        info("No change was made. Run again with --apply to retire these items.")
    return 0


if __name__ == "__main__":
    run_main(main)
