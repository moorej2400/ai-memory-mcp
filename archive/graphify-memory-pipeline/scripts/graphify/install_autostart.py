#!/usr/bin/env python3
"""Install a login-time launcher for the Graphify global MCP.

Each platform has its own supported mechanism for per-user startup work:
Windows uses the Startup folder, macOS uses a launchd LaunchAgent, and Linux
uses a systemd user unit, falling back to an XDG autostart entry when no
working systemd user manager is present.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common import (  # noqa: E402
    MACOS,
    WINDOWS,
    ScriptError,
    app_python,
    graphify_state_root,
    info,
    load_environment,
    repository_root,
    run_main,
)

LABEL = "graphify-global-mcp"


def _start_command() -> list[str]:
    """Return the command that starts the listener.

    The application interpreter is preferred so the launcher keeps working even
    if the login session has no suitable Python on PATH.
    """
    root = repository_root()
    python = app_python(root)
    if not python.is_file():
        python = Path(sys.executable)
    return [str(python), str(Path(__file__).resolve().parent / "start_global_mcp.py")]


def _archive(path: Path) -> None:
    if not path.is_file():
        return
    # Keep launcher backups beside the configured Graphify state so every
    # platform preserves the launcher inside the same AI Memory data tree.
    archive_root = graphify_state_root() / "backups" / "startup"
    archive_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    destination = archive_root / f"{path.stem}-{stamp}{path.suffix}"
    shutil.copy2(path, destination)
    info(f"Preserved the previous startup launcher at {destination}")


def _write(path: Path, content: str, newline: str = "\n") -> bool:
    existing = path.read_text(encoding="utf-8") if path.is_file() else None
    if existing == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    _archive(path)
    path.write_text(content, encoding="utf-8", newline=newline)
    return True


def _quote_xml(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _install_windows() -> Path:
    appdata = os.environ.get("APPDATA")
    roaming = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    startup = (
        roaming / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    )
    launcher = startup / f"{LABEL}-start.vbs"
    # Keep the launch chain identical to the PowerShell implementation on
    # `main`, which is the validated Windows behaviour: the hidden WScript.Shell
    # call invokes the .ps1 entry point, which now forwards to the shared Python
    # implementation. The doubled quotes are VBScript's escape for a literal
    # quote, so the script path survives spaces.
    script = Path(__file__).resolve().parent / "start-graphify-global-mcp.ps1"
    content = (
        'Set shell = CreateObject("WScript.Shell")\n'
        "shell.Run \"powershell.exe -NoProfile -WindowStyle Hidden "
        f'-ExecutionPolicy Bypass -File ""{script}""", 0, False\n'
    )
    # Windows Script Host files are conventionally CRLF, matching .gitattributes.
    _write(launcher, content, newline="\r\n")
    return launcher


def _install_macos() -> Path:
    command = _start_command()
    arguments = "\n".join(
        f"    <string>{_quote_xml(part)}</string>" for part in command
    )
    logs = graphify_state_root() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    plist = Path.home() / "Library" / "LaunchAgents" / f"com.{LABEL}.plist"
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "  <key>Label</key>\n"
        f"  <string>com.{LABEL}</string>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        f"{arguments}\n"
        "  </array>\n"
        "  <key>RunAtLoad</key>\n"
        "  <true/>\n"
        "  <key>StandardOutPath</key>\n"
        f"  <string>{_quote_xml(str(logs / f'{LABEL}.out.log'))}</string>\n"
        "  <key>StandardErrorPath</key>\n"
        f"  <string>{_quote_xml(str(logs / f'{LABEL}.err.log'))}</string>\n"
        "</dict>\n"
        "</plist>\n"
    )
    if _write(plist, content):
        # Reload so the change takes effect without requiring a logout.
        subprocess.run(
            ["launchctl", "unload", str(plist)], check=False, capture_output=True
        )
        subprocess.run(
            ["launchctl", "load", str(plist)], check=False, capture_output=True
        )
    return plist


def _run_systemctl(arguments: list[str], purpose: str) -> None:
    """Run one `systemctl --user` command, surfacing a failure rather than
    reporting success for a launcher that was never actually registered."""
    result = subprocess.run(
        ["systemctl", "--user", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ScriptError(f"Could not {purpose}: {detail}")


def _systemd_user_manager_available() -> bool:
    """Report whether a usable per-user systemd manager is actually running.

    The `systemctl` binary being installed proves nothing: containers, WSL, and
    systems booted without a user session all ship it while `--user` commands
    fail. Querying the manager is the only reliable test.
    """
    if not shutil.which("systemctl"):
        return False
    result = subprocess.run(
        ["systemctl", "--user", "show-environment"],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


# The Desktop Entry specification reserves these inside an Exec value; an
# argument containing any of them has to be quoted or the field is malformed.
XDG_RESERVED = frozenset(" \t\n\"'\\><~|&;$*?#()`")


def _desktop_exec(parts: list[str]) -> str:
    """Render one command as a Desktop Entry ``Exec`` value.

    Quoting matters because a repository or home directory is free to contain
    any of the reserved characters, and an unquoted one would split the command
    or be interpreted as syntax.
    """
    rendered: list[str] = []
    for part in parts:
        # A literal percent must be doubled so it is not read as a field code.
        value = part.replace("%", "%%")
        if any(character in XDG_RESERVED for character in value):
            escaped = value
            # Only these four are escaped with a backslash inside quotes.
            for character in ("\\", '"', "`", "$"):
                escaped = escaped.replace(character, f"\\{character}")
            rendered.append(f'"{escaped}"')
        else:
            rendered.append(value)
    return " ".join(rendered)


def _systemd_argument(part: str) -> str:
    """Quote one ExecStart argument so whitespace does not split it."""
    if not any(character.isspace() for character in part):
        return part
    escaped = part.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _install_linux() -> Path:
    command = _start_command()
    if _systemd_user_manager_available():
        unit = Path.home() / ".config" / "systemd" / "user" / f"{LABEL}.service"
        # systemd splits ExecStart on whitespace unless arguments are quoted.
        rendered = " ".join(_systemd_argument(part) for part in command)
        content = (
            "[Unit]\n"
            "Description=Graphify global MCP listener\n"
            "After=default.target\n"
            "\n"
            "[Service]\n"
            "Type=oneshot\n"
            "RemainAfterExit=yes\n"
            f"ExecStart={rendered}\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        if _write(unit, content):
            _run_systemctl(["daemon-reload"], "reload the systemd user manager")
        _run_systemctl(
            ["enable", f"{LABEL}.service"], "enable the Graphify startup unit"
        )
        return unit

    # Desktop sessions without a systemd user manager still honour XDG entries.
    entry = Path.home() / ".config" / "autostart" / f"{LABEL}.desktop"
    rendered = _desktop_exec(command)
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Graphify global MCP\n"
        f"Exec={rendered}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    _write(entry, content)
    return entry


def main() -> None:
    load_environment(repository_root())
    start_script = Path(__file__).resolve().parent / "start_global_mcp.py"
    if not start_script.is_file():
        raise ScriptError(f"Start script is missing: {start_script}")

    if WINDOWS:
        installed = _install_windows()
    elif MACOS:
        installed = _install_macos()
    else:
        installed = _install_linux()

    info(f"Installed the Graphify startup launcher at {installed}")


if __name__ == "__main__":
    run_main(main)
