"""Opt-in private query traces, separate from the digest-only audit stream."""
from __future__ import annotations

import atexit
import contextvars
import functools
import inspect
import json
import os
import threading
import time
import uuid
from collections import deque
from typing import Any

from .audit import _rotate_recoverably, file_lock, utc_now
from .config import Settings


_MAX_PENDING_BYTES = 32 * 1024 * 1024
_MAX_PENDING_EVENTS = 256
_CONDITION = threading.Condition()
_PENDING: deque[tuple[Settings, bytes]] = deque()
_PENDING_BYTES = 0
_WRITING = False
_WRITER: threading.Thread | None = None
_FAILED_EVENTS = 0
_LAST_ERROR: str | None = None
_WORKER_FAILED_EVENTS = 0
_LAST_WORKER_ERROR: str | None = None
_CURRENT: contextvars.ContextVar[QueryTrace | None] = contextvars.ContextVar("query_trace", default=None)
_STAGES: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar("query_stages", default=())


def _failure(error: str) -> None:
    global _FAILED_EVENTS, _LAST_ERROR
    with _CONDITION:
        _FAILED_EVENTS += 1
        _LAST_ERROR = error


def _write(settings: Settings, encoded: bytes) -> bool:
    try:
        directory = settings.resolved_log_dir / "queries"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink():
            raise OSError("The private query log directory must not be a symbolic link.")
        if os.name != "nt":
            directory.chmod(0o700)
        # This wait is on the bounded background writer, not the query path.
        # Reuse the audit timeout so brief cross-process contention does not drop records.
        with file_lock(directory / "query.lock", settings.audit_lock_timeout_seconds):
            path = directory / "requests.jsonl"
            if path.is_symlink():
                raise OSError("The private query log must not be a symbolic link.")
            _rotate_recoverably(path, settings.audit_log_max_bytes)
            flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            with os.fdopen(descriptor, "ab") as handle:
                if os.name != "nt":
                    os.fchmod(handle.fileno(), 0o600)
                handle.write(encoded)
                handle.flush()
        return True
    except (OSError, TimeoutError, ValueError) as exc:
        _failure(f"{type(exc).__name__}: {exc}")
        return False


def _drain() -> None:
    global _PENDING_BYTES, _WRITING
    while True:
        with _CONDITION:
            while not _PENDING:
                _CONDITION.wait()
            settings, encoded = _PENDING.popleft()
            _WRITING = True
        try:
            _write(settings, encoded)
        except Exception as exc:
            # A writer failure must not strand the remaining queue.
            _failure(f"{type(exc).__name__}: The private query log writer failed.")
        finally:
            with _CONDITION:
                _PENDING_BYTES -= len(encoded)
                _WRITING = False
                _CONDITION.notify_all()


def _submit(settings: Settings, encoded: bytes) -> bool:
    global _PENDING_BYTES, _WRITER
    with _CONDITION:
        # Slow disks must not block the MCP event loop or retain unbounded text.
        # A rejected record increments a visible counter; it is never truncated.
        if len(_PENDING) >= _MAX_PENDING_EVENTS or _PENDING_BYTES + len(encoded) > _MAX_PENDING_BYTES:
            _failure("The private query log queue reached its resource limit.")
            return False
        _PENDING.append((settings, encoded))
        _PENDING_BYTES += len(encoded)
        if _WRITER is None or not _WRITER.is_alive():
            _WRITER = threading.Thread(target=_drain, daemon=True, name="query-log-writer")
            _WRITER.start()
        _CONDITION.notify_all()
        return True


def flush_query_logs(timeout_seconds: float = 1.0) -> bool:
    """Wait a bounded time for pending log writes."""
    deadline = time.monotonic() + timeout_seconds
    with _CONDITION:
        while _PENDING or _WRITING:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _CONDITION.wait(remaining)
    return True


# Normal worker retirement happens after its response is available to the parent.
# Give that background-only exit enough time to drain ordinary stage bursts.
atexit.register(flush_query_logs, 5.0)


def query_logging_status(settings: Settings) -> dict[str, Any]:
    with _CONDITION:
        return {
            "content_enabled": settings.query_log_content and settings.audit_logging_enabled,
            "directory": str(settings.resolved_log_dir / "queries"),
            "pending_events": len(_PENDING) + int(_WRITING),
            "pending_bytes": _PENDING_BYTES,
            "failed_events": _FAILED_EVENTS,
            "last_error": _LAST_ERROR,
            "worker_failed_events": _WORKER_FAILED_EVENTS,
            "last_worker_error": _LAST_WORKER_ERROR,
        }


def accept_worker_log_report(report: Any) -> None:
    """Accumulate failure deltas once per received worker response."""
    global _WORKER_FAILED_EVENTS, _LAST_WORKER_ERROR
    if not isinstance(report, dict):
        return
    failures = report.get("failed_events")
    if not isinstance(failures, int) or failures < 0:
        return
    with _CONDITION:
        _WORKER_FAILED_EVENTS += failures
        if failures:
            _LAST_WORKER_ERROR = str(report.get("last_error") or "A worker query log record failed.")
    trace = current_trace()
    if trace is not None:
        trace.diagnostics["worker_query_log"] = report


def current_trace() -> QueryTrace | None:
    return _CURRENT.get()


class QueryTrace:
    """Correlate one accepted request with worker stages and its exact response."""

    def __init__(self, settings: Settings, arguments: dict[str, Any], transport: str,
                 *, request_id: str | None = None, response_version: str | None = None,
                 operation: str = "memory_recall"):
        self.settings = settings
        self.arguments = arguments
        self.transport = transport
        self.request_id = request_id or uuid.uuid4().hex
        self.response_version = response_version
        self.operation = operation
        self.started = time.monotonic()
        self.enabled = settings.query_log_content and settings.audit_logging_enabled
        self.timing: dict[str, Any] = {}
        self.diagnostics: dict[str, Any] = {}
        self.response: Any = None
        self.execution_state: dict[str, Any] | None = None
        self.failed_events = 0

    def emit(self, event: str, **payload: Any) -> None:
        if not self.enabled:
            return
        try:
            encoded = (json.dumps({
                "schema_version": 1, "timestamp": utc_now(), "event": event,
                "request_id": self.request_id, "process_id": os.getpid(),
                "transport": self.transport, "operation": self.operation, **payload,
            }, ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode("utf-8")
            # Stage persistence is best-effort in workers too. A blocked log disk
            # must not consume the retrieval deadline or block the MCP event loop.
            saved = _submit(self.settings, encoded)
            if not saved:
                self.failed_events += 1
        except Exception as exc:
            self.failed_events += 1
            _failure(f"{type(exc).__name__}: The private query log record could not be encoded.")

    def __enter__(self) -> QueryTrace:
        self._token = _CURRENT.set(self)
        self.emit("worker_started" if self.transport == "worker" else "query_started",
                  arguments=self.arguments if self.transport != "worker" else None,
                  response_version=self.response_version,
                  resource_limits={"workers": self.settings.recall_worker_count,
                                   "queue_capacity": self.settings.recall_queue_capacity,
                                   "deadline_seconds": self.settings.recall_timeout_seconds,
                                   "ann_candidate_limit": self.settings.ann_candidate_limit})
        return self

    def set_result(self, response: Any) -> Any:
        self.response = response
        self.diagnostics.update(getattr(response, "_diagnostics", {}))
        state = getattr(response, "_execution_state", None)
        if state is not None:
            self.execution_state = state.model_dump(mode="json")
        return response

    def __exit__(self, error_type, error, traceback) -> None:
        try:
            if not self.enabled:
                return
            cancelled = error_type is not None and error_type.__name__ in {"CancelledError", "WorkerCancelled"}
            outcome = "cancelled" if cancelled else "failed" if error_type else "completed"
            payload = (self.response.model_dump(mode="json")
                       if self.response is not None and self.transport != "worker" else None)
            self.emit(
                ("worker_" if self.transport == "worker" else "query_") + outcome,
                elapsed_ms=round((time.monotonic() - self.started) * 1000, 3),
                response=payload if self.transport != "worker" else None,
                response_version=self.response_version,
                execution_state=self.execution_state,
                error_type=error_type.__name__ if error_type else None,
                error=str(error) if error_type else None,
                timing=self.timing, diagnostics=self.diagnostics,
                log_events_failed=self.failed_events,
            )
        except Exception as exc:
            # Logging cannot replace a successful response or the original error.
            _failure(f"{type(exc).__name__}: The private query result could not be recorded.")
        finally:
            _CURRENT.reset(self._token)


def trace_progress(stage: str) -> None:
    trace = current_trace()
    if trace is not None:
        trace.emit("stage_started", stage=stage)


def query_stage(name: str):
    """Persist a provider boundary so killed workers leave a last known stage."""
    def decorate(function):
        @functools.wraps(function)
        def call(*args, **kwargs):
            trace = current_trace()
            if trace is None or not trace.enabled:
                return function(*args, **kwargs)
            stack = (*_STAGES.get(), name)
            token = _STAGES.set(stack)
            stage = "/".join(stack)
            started = time.monotonic()
            trace.emit("stage_started", stage=stage)
            try:
                result = function(*args, **kwargs)
            except BaseException as exc:
                trace.emit("stage_failed", stage=stage, error_type=type(exc).__name__,
                           elapsed_ms=round((time.monotonic() - started) * 1000, 3))
                raise
            else:
                trace.emit("stage_completed", stage=stage,
                           elapsed_ms=round((time.monotonic() - started) * 1000, 3))
                return result
            finally:
                _STAGES.reset(token)
        return call
    return decorate


def log_library_query(function):
    """Log direct service calls without duplicating the parent MCP request."""
    signature = inspect.signature(function)
    @functools.wraps(function)
    def call(self, *args, **kwargs):
        if current_trace() is not None:
            return function(self, *args, **kwargs)
        arguments = signature.bind(self, *args, **kwargs)
        arguments.apply_defaults()
        values = {key: value for key, value in arguments.arguments.items() if key != "self"}
        with QueryTrace(self.settings, values, "library", operation=f"memory_{function.__name__}") as trace:
            return trace.set_result(function(self, *args, **kwargs))
    return call
