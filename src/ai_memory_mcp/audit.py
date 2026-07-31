from __future__ import annotations

import errno
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings


_PROCESS_LOCK = threading.RLock()
_LAST_ERROR: str | None = None
_RETRYABLE_LOCK_ERRORS = {
    errno.EACCES,
    errno.EAGAIN,
    errno.EDEADLK,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def file_lock(path: Path, timeout_seconds: float) -> Iterator[float]:
    """Serialize local writers because MCP clients can run separate server processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    acquired = False
    started = time.perf_counter()
    deadline = started + timeout_seconds
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            while not acquired:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError as exc:
                    if exc.errno not in _RETRYABLE_LOCK_ERRORS:
                        raise
                    if time.perf_counter() >= deadline:
                        raise TimeoutError(
                            f"Timed out waiting for local lock: {path}"
                        ) from exc
                    time.sleep(0.1)
        else:
            import fcntl

            while not acquired:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError as exc:
                    if exc.errno not in _RETRYABLE_LOCK_ERRORS:
                        raise
                    if time.perf_counter() >= deadline:
                        raise TimeoutError(
                            f"Timed out waiting for local lock: {path}"
                        ) from exc
                    time.sleep(0.1)
        yield round((time.perf_counter() - started) * 1000, 3)
    finally:
        if acquired:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _rotate_recoverably(path: Path, max_bytes: int) -> None:
    if not path.exists() or path.stat().st_size < max_bytes:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive = path.with_name(f"{path.stem}-{stamp}{path.suffix}")
    counter = 1
    while archive.exists():
        archive = path.with_name(
            f"{path.stem}-{stamp}-{counter}{path.suffix}"
        )
        counter += 1
    path.rename(archive)


def append_event(
    settings: "Settings",
    stream: str,
    event: str,
    payload: dict[str, Any],
) -> bool:
    """Write local audit data without making retrieval depend on log availability."""
    global _LAST_ERROR
    if not settings.audit_logging_enabled:
        return False
    record = {
        "timestamp": utc_now(),
        "event": event,
        **payload,
    }
    try:
        with _PROCESS_LOCK:
            log_dir = settings.resolved_log_dir
            log_dir.mkdir(parents=True, exist_ok=True)
            path = log_dir / f"{stream}.jsonl"
            with file_lock(
                log_dir / "audit.lock",
                settings.audit_lock_timeout_seconds,
            ):
                _rotate_recoverably(path, settings.audit_log_max_bytes)
                with path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(
                        json.dumps(
                            record,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            default=str,
                        )
                        + "\n"
                    )
                    handle.flush()
        _LAST_ERROR = None
        return True
    except (OSError, TimeoutError, TypeError, ValueError) as exc:
        _LAST_ERROR = f"{type(exc).__name__}: {exc}"
        return False


def logging_status(settings: "Settings") -> dict[str, Any]:
    log_dir = settings.resolved_log_dir
    streams = {}
    for name in ("index", "retrieval"):
        path = log_dir / f"{name}.jsonl"
        streams[name] = {
            "path": str(path),
            "exists": path.is_file(),
            "bytes": path.stat().st_size if path.is_file() else 0,
        }
    probe = log_dir if log_dir.exists() else log_dir.parent
    return {
        "enabled": settings.audit_logging_enabled,
        "directory": str(log_dir),
        "writable": probe.exists() and os.access(probe, os.W_OK),
        "streams": streams,
        "last_error": _LAST_ERROR,
    }
