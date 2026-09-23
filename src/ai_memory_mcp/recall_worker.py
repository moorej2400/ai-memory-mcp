from __future__ import annotations

import hashlib
import atexit
import asyncio
import concurrent.futures
import contextvars
import json
import queue
import struct
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .audit import append_event
from .config import MemorySource, Settings
from .models import RecallExecutionState, RecallResponse
from .service import MemoryService
from .query_log import QueryTrace, accept_worker_log_report, current_trace, query_logging_status


_MAX_WORKER_RESULT_BYTES = 16 * 1024 * 1024
_MAX_WORKER_REQUEST_BYTES = 1024 * 1024
_PROCESS_STOP_GRACE_SECONDS = 1.0
_PROCESS_RETIRE_GRACE_SECONDS = 5.25
_TIMEOUT_AUDIT_LOCK_SECONDS = 0.1


class WorkerDeadlineExceeded(TimeoutError):
    def __init__(self, worker_pid: int | None):
        super().__init__("The memory recall worker exceeded its time limit.")
        self.worker_pid = worker_pid


class WorkerExecutionFailed(RuntimeError):
    pass


class WorkerQueueFull(RuntimeError):
    pass


class WorkerCancelled(RuntimeError):
    pass




class RecallArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    source_id: str | None = None
    root_scope: str | None = None
    domain: str | None = None
    record_type: str | None = None
    collection: str | None = None
    scope_kind: str | None = None
    scope_id: str | None = None
    repository: str | None = None
    project: str | None = None
    ticket: str | None = None
    status: Literal["active", "needs-review", "superseded", "archived"] = "active"
    path_prefix: str | None = None
    source_label: str | None = None
    source_instance: str | None = None
    artifact_kind: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    limit: int = Field(default=8, ge=1, le=20)

    @field_validator("date_from", "date_to")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Artifact date arguments must include a timezone.")
        return value


class RecallWorkerEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settings: dict[str, Any]
    arguments: RecallArguments
    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex)


def _decode_worker_payload(raw: bytes, expected_request_id: str | None = None) -> Any:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerExecutionFailed(
            "The memory recall worker returned an invalid result."
        ) from exc
    if not isinstance(payload, dict) or payload.get("status") not in {
        "ok",
        "error",
    }:
        raise WorkerExecutionFailed(
            "The memory recall worker returned an invalid result."
        )
    if payload["status"] == "error":
        accept_worker_log_report(payload.get("_query_log"))
        error_type = payload.get("error_type")
        safe_type = error_type if isinstance(error_type, str) else "UnknownError"
        raise WorkerExecutionFailed(
            f"The memory recall worker failed with {safe_type}."
        )
    if expected_request_id is not None and payload.get("request_id") != expected_request_id:
        raise WorkerExecutionFailed(
            "The memory recall worker returned a mismatched request identifier."
        )
    accept_worker_log_report(payload.get("_query_log"))
    return payload.get("result")




_SETTINGS_PATH_FIELDS = (
    "memory_root",
    "state_dir",
    "graph_path",
    "log_dir",
    "artifact_db",
    "artifact_objects_dir",
    "artifact_backup_dir",
)


def _serialize_settings(settings: Settings) -> dict[str, Any]:
    payload = asdict(settings)
    for name in _SETTINGS_PATH_FIELDS:
        value = payload[name]
        payload[name] = str(value) if value is not None else None
    payload["retrieval_sources"] = [
        {
            "source_id": source.source_id,
            "root": str(source.root),
            "writable": source.writable,
        }
        for source in settings.retrieval_sources
    ]
    return payload


def _deserialize_settings(payload: dict[str, Any]) -> Settings:
    values = dict(payload)
    for name in _SETTINGS_PATH_FIELDS:
        value = values[name]
        values[name] = Path(value) if value is not None else None
    values["retrieval_sources"] = tuple(
        MemorySource(
            source_id=str(source["source_id"]),
            root=Path(str(source["root"])),
            writable=bool(source.get("writable", False)),
        )
        for source in values.get("retrieval_sources", [])
    )
    return Settings(**values)


def _stop_subprocess(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=_PROCESS_STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=_PROCESS_STOP_GRACE_SECONDS)
    if process.poll() is None:
        raise RuntimeError("The memory recall worker could not stop.")
    # communicate() can wait for a pipe lock held by the blocked writer that
    # this supervisor is cancelling. Reap first; closed child handles unblock IO.
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _close_retired_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None and process.stdin is not None:
        try:
            # EOF exits the child loop normally, which runs its atexit log flush.
            process.stdin.close()
        except OSError:
            pass
    if process.poll() is None:
        try:
            process.wait(timeout=_PROCESS_RETIRE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _stop_subprocess(process)
            return
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass




def _read_exact(stream: Any, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError("The memory recall worker closed its result stream.")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_frame(stream: Any, maximum: int = _MAX_WORKER_RESULT_BYTES) -> bytes:
    header = _read_exact(stream, 4)
    length = struct.unpack(">I", header)[0]
    if length > maximum:
        raise WorkerExecutionFailed(
            "The memory recall worker result exceeded its size limit."
        )
    return _read_exact(stream, length)


def _write_frame(stream: Any, payload: bytes) -> None:
    stream.write(struct.pack(">I", len(payload)))
    stream.write(payload)
    stream.flush()


@dataclass(slots=True)
class _WarmWorker:
    process: subprocess.Popen[bytes]
    request_count: int = 0


class _WorkerPool:
    """Own bounded reusable workers and replace any worker after a hard stop."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.available: queue.Queue[_WarmWorker] = queue.Queue()
        self.capacity = threading.BoundedSemaphore(
            settings.recall_worker_count + settings.recall_queue_capacity
        )
        self.closed = False
        self.lock = threading.Lock()
        self.retiring: set[subprocess.Popen[bytes]] = set()
        self.retiring_lock = threading.Lock()
        self.managers = concurrent.futures.ThreadPoolExecutor(
            max_workers=settings.recall_worker_count,
            thread_name_prefix="recall-manager",
        )
        for _ in range(settings.recall_worker_count):
            self.available.put(self._spawn())

    @staticmethod
    def _spawn() -> _WarmWorker:
        process = subprocess.Popen(
            [sys.executable, "-m", "ai_memory_mcp.recall_worker", "--pool-child"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        return _WarmWorker(process=process)

    def _replace(self, worker: _WarmWorker) -> _WarmWorker:
        _stop_subprocess(worker.process)
        _archive_worker_generation_leases(self.settings, worker.process.pid)
        return self._spawn()

    def _retire(self, worker: _WarmWorker) -> _WarmWorker:
        process = worker.process
        with self.retiring_lock:
            # Keep retirement outside the active worker count, but cap its
            # temporary model/process memory under slow diagnostic storage.
            if len(self.retiring) >= self.settings.recall_worker_count:
                return worker
            self.retiring.add(process)
        try:
            replacement = self._spawn()
        except OSError:
            with self.retiring_lock:
                self.retiring.discard(process)
            return worker

        def finish() -> None:
            try:
                _close_retired_process(process)
                _archive_worker_generation_leases(self.settings, process.pid)
            finally:
                with self.retiring_lock:
                    self.retiring.discard(process)

        # The completed response does not wait for diagnostic disk I/O.
        threading.Thread(target=finish, daemon=True, name="recall-worker-retirement").start()
        return replacement

    def request(
        self,
        payload: bytes,
        timeout_seconds: float,
        cancel_event: threading.Event | None = None,
        timing: dict[str, Any] | None = None,
        *,
        deadline: float | None = None,
        capacity_reserved: bool = False,
    ) -> bytes:
        started = time.monotonic()
        deadline = deadline if deadline is not None else started + timeout_seconds
        if timing is not None:
            timing.clear()
            timing.update({"outcome": "failed", "replacement": False})
        if not capacity_reserved and not self.capacity.acquire(blocking=False):
            if timing is not None:
                timing.update(
                    {
                        "outcome": "queue_full",
                        "queue_ms": 0.0,
                        "total_ms": round((time.monotonic() - started) * 1000, 3),
                    }
                )
            raise WorkerQueueFull("The memory recall worker queue is full.")
        worker: _WarmWorker | None = None
        worker_started: float | None = None
        try:
            while worker is None:
                if cancel_event is not None and cancel_event.is_set():
                    if timing is not None:
                        timing["outcome"] = "cancelled"
                    raise WorkerCancelled("The memory recall request was cancelled.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if timing is not None:
                        timing["outcome"] = "deadline_exceeded"
                    raise WorkerDeadlineExceeded(None)
                try:
                    worker = self.available.get(timeout=min(0.05, remaining))
                except queue.Empty:
                    continue
            if timing is not None:
                timing["queue_ms"] = round((time.monotonic() - started) * 1000, 3)
                timing["cold"] = worker.request_count == 0
                timing["worker_pid"] = worker.process.pid
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if timing is not None:
                    timing["outcome"] = "deadline_exceeded"
                raise WorkerDeadlineExceeded(worker.process.pid)
            if (
                worker.process.poll() is not None
                or worker.process.stdin is None
                or worker.process.stdout is None
            ):
                worker = self._replace(worker)
                if timing is not None:
                    timing["replacement"] = True
                    timing["worker_pid"] = worker.process.pid
                    timing["cold"] = True
            try:
                worker_started = time.monotonic()
                executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                process = worker.process

                def exchange() -> bytes:
                    _write_frame(process.stdin, payload)
                    return _read_frame(process.stdout)

                # Both request writes and result reads run under supervision.
                # A child that stops reading must not bypass the deadline.
                future = executor.submit(exchange)
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        worker = self._replace(worker)
                        if timing is not None:
                            timing.update(
                                {"outcome": "cancelled", "replacement": True}
                            )
                        executor.shutdown(wait=True, cancel_futures=True)
                        raise WorkerCancelled(
                            "The memory recall request was cancelled."
                        )
                    wait = min(
                        0.05,
                        max(0.0, deadline - time.monotonic()),
                    )
                    if wait <= 0:
                        worker_pid = worker.process.pid
                        worker = self._replace(worker)
                        if timing is not None:
                            timing.update(
                                {"outcome": "deadline_exceeded", "replacement": True}
                            )
                        executor.shutdown(wait=True, cancel_futures=True)
                        raise WorkerDeadlineExceeded(worker_pid)
                    try:
                        output = future.result(timeout=wait)
                    except concurrent.futures.TimeoutError:
                        continue
                    executor.shutdown(wait=True)
                    break
            except (WorkerDeadlineExceeded, WorkerCancelled):
                raise
            except (BrokenPipeError, EOFError, OSError, WorkerExecutionFailed) as exc:
                worker = self._replace(worker)
                executor.shutdown(wait=True, cancel_futures=True)
                if timing is not None:
                    timing.update({"outcome": "worker_failed", "replacement": True})
                raise WorkerExecutionFailed(
                    "The memory recall worker ended without a valid result."
                ) from exc
            worker.request_count += 1
            if timing is not None:
                timing["worker_ms"] = round(
                    (time.monotonic() - worker_started) * 1000, 3
                )
                timing["outcome"] = "complete"
            if worker.request_count >= self.settings.recall_worker_max_requests:
                retired = self._retire(worker)
                if timing is not None and retired is not worker:
                    timing["replacement"] = True
                worker = retired
            return output
        finally:
            if timing is not None:
                timing["total_ms"] = round((time.monotonic() - started) * 1000, 3)
                if worker_started is not None and "worker_ms" not in timing:
                    timing["worker_ms"] = round((time.monotonic() - worker_started) * 1000, 3)
                timing.setdefault("queue_ms", timing["total_ms"])
            if worker is not None:
                if self.closed:
                    _stop_subprocess(worker.process)
                    _archive_worker_generation_leases(self.settings, worker.process.pid)
                else:
                    self.available.put(worker)
            if not capacity_reserved:
                self.capacity.release()

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.managers.shutdown(wait=False, cancel_futures=True)
            while True:
                try:
                    worker = self.available.get_nowait()
                except queue.Empty:
                    break
                _stop_subprocess(worker.process)
                _archive_worker_generation_leases(self.settings, worker.process.pid)
            with self.retiring_lock:
                retiring = list(self.retiring)
            for process in retiring:
                _stop_subprocess(process)
                _archive_worker_generation_leases(self.settings, process.pid)


_POOLS: dict[str, _WorkerPool] = {}
_POOLS_LOCK = threading.Lock()


def _pool_for(settings: Settings) -> _WorkerPool:
    key = json.dumps(_serialize_settings(settings), sort_keys=True)
    with _POOLS_LOCK:
        pool = _POOLS.get(key)
        if pool is None:
            pool = _WorkerPool(settings)
            _POOLS[key] = pool
        return pool


def _close_pools() -> None:
    with _POOLS_LOCK:
        pools = list(_POOLS.values())
        _POOLS.clear()
    for pool in pools:
        pool.close()


atexit.register(_close_pools)


def _run_recall_subprocess(
    settings: Settings,
    arguments: dict[str, Any],
    cancel_event: threading.Event | None = None,
    timing: dict[str, Any] | None = None,
    *,
    deadline: float | None = None,
    capacity_reserved: bool = False,
) -> dict[str, Any]:
    # JSON-mode serialization preserves explicit date offsets and rejects
    # unrecognized request values before a worker can open source data.
    envelope = RecallWorkerEnvelope(
        settings=_serialize_settings(settings),
        arguments=RecallArguments.model_validate(arguments),
        request_id=current_trace().request_id if current_trace() else uuid.uuid4().hex,
    )
    request = envelope.model_dump_json().encode("utf-8")
    if len(request) > _MAX_WORKER_REQUEST_BYTES:
        raise WorkerExecutionFailed(
            "The memory recall worker request exceeded its size limit."
        )
    # A dedicated interpreter keeps its result pipe separate from FastMCP stdio.
    # The parent can terminate and reap this exact process at the deadline.
    output = _pool_for(settings).request(
        request,
        settings.recall_timeout_seconds,
        cancel_event,
        timing,
        deadline=deadline,
        capacity_reserved=capacity_reserved,
    )
    if len(output) > _MAX_WORKER_RESULT_BYTES:
        raise WorkerExecutionFailed(
            "The memory recall worker result exceeded its size limit."
        )
    if not output:
        raise WorkerExecutionFailed(
            "The memory recall worker ended without a valid result."
        )
    result = _decode_worker_payload(output, envelope.request_id)
    if not isinstance(result, dict):
        raise WorkerExecutionFailed(
            "The memory recall worker returned an invalid result."
        )
    return result




def _pool_child_main() -> int:
    service: MemoryService | None = None
    serialized_settings = ""
    reported_log_failures = 0
    while True:
        try:
            raw = _read_frame(sys.stdin.buffer, _MAX_WORKER_REQUEST_BYTES)
        except EOFError:
            return 0
        try:
            request = RecallWorkerEnvelope.model_validate_json(raw)
            settings_key = json.dumps(request.settings, sort_keys=True)
            if service is None or settings_key != serialized_settings:
                service = MemoryService(_deserialize_settings(request.settings))
                serialized_settings = settings_key
            with QueryTrace(service.settings, {}, "worker", request_id=request.request_id) as trace:
                response = service.recall(**request.arguments.model_dump())
                trace.set_result(response)
            response._diagnostics["log_events_failed"] = trace.failed_events
            payload: dict[str, Any] = {
                "status": "ok",
                "request_id": request.request_id,
                "result": {
                    **response.model_dump(mode="json"),
                    "_execution_state": response._execution_state.model_dump(mode="json"),
                    "_diagnostics": response._diagnostics,
                },
            }
        except BaseException as exc:
            payload = {
                "status": "error",
                "error_type": type(exc).__name__,
            }
        if service is not None and service.settings.query_log_content:
            log_status = query_logging_status(service.settings)
            # Include terminal-enqueue failures and asynchronous failures observed
            # since the previous response. Later disk failures arrive on a later
            # response; pending counts distinguish that delay from confirmed loss.
            payload["_query_log"] = {
                "failed_events": log_status["failed_events"] - reported_log_failures,
                "last_error": log_status["last_error"],
                "pending_events": log_status["pending_events"],
                "pending_bytes": log_status["pending_bytes"],
            }
            reported_log_failures = log_status["failed_events"]
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > _MAX_WORKER_RESULT_BYTES:
            encoded = b'{"status":"error","error_type":"WorkerResultTooLarge"}'
        _write_frame(sys.stdout.buffer, encoded)


def _archive_worker_generation_leases(
    settings: Settings,
    worker_pid: int,
) -> int:
    archive = settings.state_dir / "retired-generation-leases"
    moved = 0
    for lease in settings.state_dir.glob(
        f".generation-lease-*-{worker_pid}-*.json"
    ):
        archive.mkdir(parents=True, exist_ok=True)
        destination = archive / lease.name
        if destination.exists():
            destination = archive / f"{lease.stem}-{uuid.uuid4().hex}{lease.suffix}"
        try:
            lease.replace(destination)
        except FileNotFoundError:
            continue
        moved += 1
    return moved


def recall_in_worker(
    settings: Settings,
    arguments: dict[str, Any],
    cancel_event: threading.Event | None = None,
    timing: dict[str, Any] | None = None,
    *,
    deadline: float | None = None,
    capacity_reserved: bool = False,
) -> RecallResponse:
    started = time.perf_counter()
    try:
        result = _run_recall_subprocess(
            settings, arguments, cancel_event, timing,
            deadline=deadline, capacity_reserved=capacity_reserved,
        )
    except WorkerDeadlineExceeded as exc:
        archived_leases = (
            _archive_worker_generation_leases(settings, exc.worker_pid)
            if exc.worker_pid is not None
            else 0
        )
        # Timeout telemetry must not add the normal ten-second audit lock wait
        # after the supervised worker has already reached its deadline.
        audit_settings = replace(
            settings,
            audit_lock_timeout_seconds=min(
                settings.audit_lock_timeout_seconds,
                _TIMEOUT_AUDIT_LOCK_SECONDS,
            ),
        )
        query = str(arguments.get("query", ""))
        append_event(
            audit_settings,
            "retrieval",
            "retrieval_timed_out",
            {
                "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
                "query_characters": len(query),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "timeout_seconds": settings.recall_timeout_seconds,
                "archived_generation_leases": archived_leases,
            },
        )
        raise
    state = result.pop("_execution_state", {})
    diagnostics = result.pop("_diagnostics", {})
    response = RecallResponse.model_validate(result)
    response._execution_state = RecallExecutionState.model_validate(state)
    response._diagnostics = diagnostics
    return response


async def recall_in_worker_async(
    settings: Settings, arguments: dict[str, Any]
) -> RecallResponse:
    deadline = time.monotonic() + settings.recall_timeout_seconds
    pool = _pool_for(settings)
    # Admission precedes executor submission. Its queue cannot add uncounted
    # work, and the same absolute deadline includes all scheduling delay.
    if not pool.capacity.acquire(blocking=False):
        trace = current_trace()
        if trace is not None:
            trace.timing.update({"outcome": "queue_full", "queue_ms": 0.0})
        raise WorkerQueueFull("The memory recall worker queue is full.")
    cancelled = threading.Event()
    future = None
    submitted = time.monotonic()
    timing: dict[str, Any] = {}

    def run():
        scheduled_ms = (time.monotonic() - submitted) * 1000
        try:
            return recall_in_worker(settings, arguments, cancelled, timing,
                                    deadline=deadline, capacity_reserved=True)
        except WorkerExecutionFailed:
            # A successful pipe exchange is not proof of a valid worker result.
            timing["outcome"] = "worker_failed"
            raise
        finally:
            timing["executor_queue_ms"] = round(scheduled_ms, 3)
            timing["queue_ms"] = round(timing.get("queue_ms", 0.0) + scheduled_ms, 3)

    try:
        # Context does not propagate through ThreadPoolExecutor automatically.
        # Carry the parent trace ID into the worker envelope, not a new request ID.
        context = contextvars.copy_context()
        future = pool.managers.submit(
            context.run, run,
        )
        wrapped = asyncio.wrap_future(future)
        try:
            return await asyncio.wait_for(
                asyncio.shield(wrapped), max(0.0, deadline - time.monotonic())
            )
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            cancelled.set()
            if future.cancel():
                queued_ms = round((time.monotonic() - submitted) * 1000, 3)
                timing.update({"queue_ms": queued_ms, "executor_queue_ms": queued_ms})
            else:
                try:
                    await asyncio.shield(wrapped)
                except BaseException:
                    pass
            if isinstance(exc, asyncio.CancelledError):
                timing["outcome"] = "cancelled"
                raise
            timing["outcome"] = "deadline_exceeded"
            raise WorkerDeadlineExceeded(None) from exc
    finally:
        trace = current_trace()
        if trace is not None:
            trace.timing.update(timing)
            trace.timing["total_ms"] = round((time.monotonic() - (deadline - settings.recall_timeout_seconds)) * 1000, 3)
        pool.capacity.release()


if __name__ == "__main__" and sys.argv[1:] == ["--pool-child"]:
    raise SystemExit(_pool_child_main())
