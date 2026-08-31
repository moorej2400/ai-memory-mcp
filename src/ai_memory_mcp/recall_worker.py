from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .audit import append_event
from .config import MemorySource, Settings
from .models import RecallResponse
from .service import MemoryService


_MAX_WORKER_RESULT_BYTES = 16 * 1024 * 1024
_MAX_WORKER_REQUEST_BYTES = 1024 * 1024
_PROCESS_STOP_GRACE_SECONDS = 1.0
_TIMEOUT_AUDIT_LOCK_SECONDS = 0.1


class WorkerDeadlineExceeded(TimeoutError):
    def __init__(self, worker_pid: int | None):
        super().__init__("The memory recall worker exceeded its time limit.")
        self.worker_pid = worker_pid


class WorkerExecutionFailed(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RecallWork:
    settings: Settings
    arguments: dict[str, Any]


def _decode_worker_payload(raw: bytes) -> Any:
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
        error_type = payload.get("error_type")
        safe_type = error_type if isinstance(error_type, str) else "UnknownError"
        raise WorkerExecutionFailed(
            f"The memory recall worker failed with {safe_type}."
        )
    return payload.get("result")


def _execute_recall(work: RecallWork) -> dict[str, Any]:
    response = MemoryService(work.settings).recall(**work.arguments)
    return response.model_dump(mode="json")


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
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.communicate(timeout=_PROCESS_STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=_PROCESS_STOP_GRACE_SECONDS)
    if process.poll() is None:
        raise RuntimeError("The memory recall worker could not stop.")


def _run_worker_command(
    command: list[str],
    request: bytes,
    timeout_seconds: float,
) -> bytes:
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        try:
            output, _ = process.communicate(
                input=request,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            worker_pid = process.pid
            _stop_subprocess(process)
            raise WorkerDeadlineExceeded(worker_pid) from exc
        return output
    finally:
        _stop_subprocess(process)


def _run_recall_subprocess(
    settings: Settings,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    request = json.dumps(
        {
            "settings": _serialize_settings(settings),
            "arguments": arguments,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(request) > _MAX_WORKER_REQUEST_BYTES:
        raise WorkerExecutionFailed(
            "The memory recall worker request exceeded its size limit."
        )
    # A dedicated interpreter keeps its result pipe separate from FastMCP stdio.
    # The parent can terminate and reap this exact process at the deadline.
    output = _run_worker_command(
        [sys.executable, "-m", "ai_memory_mcp.recall_worker", "--child"],
        request,
        settings.recall_timeout_seconds,
    )
    if len(output) > _MAX_WORKER_RESULT_BYTES:
        raise WorkerExecutionFailed(
            "The memory recall worker result exceeded its size limit."
        )
    if not output:
        raise WorkerExecutionFailed(
            "The memory recall worker ended without a valid result."
        )
    result = _decode_worker_payload(output)
    if not isinstance(result, dict):
        raise WorkerExecutionFailed(
            "The memory recall worker returned an invalid result."
        )
    return result


def _child_main() -> int:
    raw = sys.stdin.buffer.read(_MAX_WORKER_REQUEST_BYTES + 1)
    if len(raw) > _MAX_WORKER_REQUEST_BYTES:
        payload: dict[str, Any] = {
            "status": "error",
            "error_type": "WorkerRequestTooLarge",
        }
    else:
        try:
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("The worker request must be an object.")
            settings_payload = request.get("settings")
            arguments = request.get("arguments")
            if not isinstance(settings_payload, dict) or not isinstance(
                arguments,
                dict,
            ):
                raise ValueError("The worker request is incomplete.")
            result = _execute_recall(
                RecallWork(
                    settings=_deserialize_settings(settings_payload),
                    arguments=arguments,
                )
            )
            payload = {"status": "ok", "result": result}
        except BaseException as exc:
            payload = {"status": "error", "error_type": type(exc).__name__}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_WORKER_RESULT_BYTES:
        encoded = b'{"status":"error","error_type":"WorkerResultTooLarge"}'
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return 0


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
) -> RecallResponse:
    started = time.perf_counter()
    try:
        result = _run_recall_subprocess(settings, arguments)
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
    return RecallResponse.model_validate(result)


if __name__ == "__main__" and sys.argv[1:] == ["--child"]:
    raise SystemExit(_child_main())
