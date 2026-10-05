"""Cross-platform process discovery and port readiness checks.

The PowerShell originals relied on ``Win32_Process`` and ``Get-NetTCPConnection``,
which exist only on Windows. These helpers use the standard library plus the
process listing tool each platform ships with, so no third-party dependency is
required.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass

WINDOWS = os.name == "nt"


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    command_line: str


def list_processes() -> list[ProcessInfo]:
    """Return every visible process with its command line."""
    if WINDOWS:
        return _list_processes_windows()
    return _list_processes_posix()


def _list_processes_posix() -> list[ProcessInfo]:
    # `ps -eo pid=,args=` is specified by POSIX and behaves the same on macOS
    # and Linux, so one invocation covers both.
    result = subprocess.run(
        ["ps", "-eo", "pid=,args="],
        check=False,
        capture_output=True,
        text=True,
    )
    processes: list[ProcessInfo] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        if pid_text.isdigit():
            processes.append(ProcessInfo(int(pid_text), command.strip()))
    return processes


def _list_processes_windows() -> list[ProcessInfo]:
    # CIM replaced WMIC on current Windows builds, so PowerShell is the portable
    # way to read command lines without adding a dependency.
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Get-CimInstance Win32_Process | "
            "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    processes: list[ProcessInfo] = []
    for line in result.stdout.splitlines():
        pid_text, _, command = line.partition("\t")
        pid_text = pid_text.strip()
        if pid_text.isdigit():
            processes.append(ProcessInfo(int(pid_text), command.strip()))
    return processes


def find_processes(*fragments: str) -> list[ProcessInfo]:
    """Return processes whose command line contains every fragment.

    This script and its ancestors are always excluded. A shell or supervisor
    that merely mentions the fragments on its own command line would otherwise
    match, and callers use these results to terminate processes.
    """
    needles = [fragment.casefold() for fragment in fragments]
    excluded = _self_and_ancestors()
    return [
        process
        for process in list_processes()
        if process.pid not in excluded
        and all(needle in process.command_line.casefold() for needle in needles)
    ]


def _self_and_ancestors() -> set[int]:
    parents = _parent_map()
    current = os.getpid()
    chain = {current}
    seen = set()
    while current in parents and current not in seen:
        seen.add(current)
        current = parents[current]
        if current <= 0:
            break
        chain.add(current)
    return chain


def descendant_pids(root_pid: int) -> set[int]:
    """Return ``root_pid`` together with every descendant process ID."""
    children: dict[int, list[int]] = {}
    for pid, parent in _parent_map().items():
        children.setdefault(parent, []).append(pid)

    found = {root_pid}
    queue = [root_pid]
    while queue:
        current = queue.pop()
        for child in children.get(current, ()):
            if child not in found:
                found.add(child)
                queue.append(child)
    return found


def _parent_map() -> dict[int, int]:
    if WINDOWS:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-CimInstance Win32_Process | "
                "ForEach-Object { \"$($_.ProcessId) $($_.ParentProcessId)\" }",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    else:
        result = subprocess.run(
            ["ps", "-eo", "pid=,ppid="],
            check=False,
            capture_output=True,
            text=True,
        )
    mapping: dict[int, int] = {}
    for line in result.stdout.split("\n"):
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            mapping[int(parts[0])] = int(parts[1])
    return mapping


def terminate_tree(root_pid: int, timeout: float = 10.0) -> None:
    """Stop a process and its descendants, escalating only if they linger.

    Children are stopped before their parents so a supervising process cannot
    respawn them, and the graceful signal is given time to work before a forced
    kill, which would leave shared state unflushed.

    Windows stops forcefully straight away, matching the PowerShell
    implementation on `main`. A detached, windowless process there has no
    console signal to receive, so attempting a graceful stop first would only
    stall until the timeout before the forced stop that actually works.
    """
    targets = sorted(descendant_pids(root_pid), reverse=True)

    if WINDOWS:
        for pid in targets:
            _signal_process(pid, graceful=False)
        return

    for pid in targets:
        _signal_process(pid, graceful=True)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _reap(targets)
        if not any(process_exists(pid) for pid in targets):
            return
        time.sleep(0.2)

    for pid in targets:
        if process_exists(pid):
            _signal_process(pid, graceful=False)
    _reap(targets)


def _reap(pids: list[int]) -> None:
    """Collect any of these processes that are our own exited children.

    Without this the exit status is never claimed, the entry lingers as a
    zombie, and the liveness check below would wait out the whole timeout for a
    process that already stopped.
    """
    if WINDOWS:
        return
    for pid in pids:
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, PermissionError, OSError):
            continue


def _signal_process(pid: int, graceful: bool) -> None:
    if WINDOWS:
        # Windows has no graceful console signal for a detached process, so the
        # taskkill tree stop is the only reliable option.
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T"] + ([] if graceful else ["/F"]),
            check=False,
            capture_output=True,
        )
        return
    try:
        os.kill(pid, signal.SIGTERM if graceful else signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def process_exists(pid: int) -> bool:
    if WINDOWS:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            check=False,
            capture_output=True,
            text=True,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers signal checks but has already stopped running, so
    # treating it as alive would stall every caller that waits for exit.
    return not _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip().startswith("Z")


def port_is_serving(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return whether something accepts TCP connections on ``host:port``.

    Connecting is portable and, unlike inspecting the listener's owning PID,
    also confirms the socket is actually reachable by clients.
    """
    families = socket.getaddrinfo(
        host or "127.0.0.1", port, proto=socket.IPPROTO_TCP
    )
    for family, socket_type, proto, _, address in families:
        with socket.socket(family, socket_type, proto) as probe:
            probe.settimeout(timeout)
            if probe.connect_ex(address) == 0:
                return True
    return False


def wait_for_port(
    host: str,
    port: int,
    deadline_seconds: float,
    process=None,
) -> bool:
    """Wait until the port serves, failing fast if the child process exits."""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            return False
        if port_is_serving(host, port):
            return True
        time.sleep(0.5)
    return False


def detached_popen(command: list[str], stdout, stderr) -> subprocess.Popen:
    """Start a background process that outlives this script.

    Detaching keeps the service running after the launcher exits, and differs
    per platform: POSIX needs a new session, Windows needs an explicit flag.
    """
    kwargs: dict[str, object] = {"stdout": stdout, "stderr": stderr}
    if WINDOWS:
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        kwargs["start_new_session"] = True
    if sys.platform != "win32":
        kwargs["close_fds"] = True
    return subprocess.Popen(command, **kwargs)  # type: ignore[arg-type]
