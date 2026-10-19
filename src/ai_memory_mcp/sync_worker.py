"""Durable, cross-process MCP sync scheduling; the CLI service stays synchronous.

The append-only journal and its lock are in the configured index directory. The
worker is a separate interpreter so a blocked native index build cannot hold an
MCP request open. No job file is removed by this module.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .audit import file_lock
from .config import Settings
from .models import SyncJobResponse, SyncResponse
from .platform_paths import subprocess_python
from .recall_worker import _deserialize_settings, _serialize_settings
from .service import MemoryService

_SCHEMA = "ai-memory/sync-job@1"
_LOCK_SECONDS = 5.0
_DEFAULT_DEADLINE_SECONDS = 1800.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _paths(settings: Settings) -> tuple[Path, Path]:
    return settings.state_dir / "sync-job.lock", settings.state_dir / "sync-jobs.jsonl"


def _read(journal: Path) -> dict | None:
    if not journal.exists():
        return None
    # A torn or unknown record is an error, not permission to start another sync.
    with journal.open("rb") as handle:
        lines = handle.readlines()
    if not lines or not lines[-1].endswith(b"\n"):
        raise RuntimeError("The sync job journal is incomplete.")
    try:
        record = json.loads(lines[-1])
        if record["schema"] != _SCHEMA:
            raise ValueError("Unknown sync job schema")
        SyncJobResponse.model_validate(record["job"])
        return record
    except (KeyError, ValueError, TypeError) as exc:
        raise RuntimeError("The sync job journal is invalid.") from exc


def _append(journal: Path, record: dict) -> None:
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("ab") as handle:
        handle.write((json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _reconcile(journal: Path, record: dict | None) -> dict | None:
    if record is None:
        return None
    job = record["job"]
    if job["state"] not in ("queued", "running"):
        return record
    expired = time.time() >= record["deadline_epoch"]
    # A queued job can briefly have no PID while the launcher starts it.
    pid = record.get("pid", 0)
    # A child can mark itself running before the launcher records its PID.
    # Give that narrow handoff time to finish before treating PID 0 as dead.
    abandoned = (bool(pid) or time.time() - record["queued_epoch"] > 5) and not _alive(pid)
    # Do not admit a replacement while the old worker can still publish data.
    if (expired and _alive(record.get("pid", 0))) or (not expired and not abandoned):
        return record
    failed = {**record, "job": {**job, "state": "failed", "stage": "deadline" if expired else "worker-exited", "finished_at": _now(), "error": "The sync worker did not report a result. Publication is unknown. Check generation status."}}
    _append(journal, failed)
    return failed


def sync_job_status(settings: Settings, job_id: str | None = None) -> SyncJobResponse | None:
    lock, journal = _paths(settings)
    with file_lock(lock, _LOCK_SECONDS):
        record = _reconcile(journal, _read(journal))
        if job_id is not None and record is not None and record["job"]["job_id"] != job_id:
            with journal.open("r", encoding="utf-8") as handle:
                for line in handle:
                    candidate = json.loads(line)
                    if candidate["job"]["job_id"] == job_id:
                        record = candidate
            if record["job"]["job_id"] != job_id:
                return None
        return SyncJobResponse.model_validate(record["job"]) if record else None


def enqueue_sync(settings: Settings) -> SyncJobResponse:
    lock, journal = _paths(settings)
    with file_lock(lock, _LOCK_SECONDS):
        record = _reconcile(journal, _read(journal))
        if record and record["job"]["state"] in ("queued", "running"):
            if record["job"]["state"] == "running" and not record["job"]["pending_refresh"]:
                record = {**record, "job": {**record["job"], "pending_refresh": True}}
                _append(journal, record)
            return SyncJobResponse.model_validate({**record["job"], "coalesced": True})
        seconds = float(os.getenv("AI_MEMORY_MCP_SYNC_TIMEOUT_SECONDS", str(_DEFAULT_DEADLINE_SECONDS)))
        if not 1 <= seconds <= 86400:
            raise ValueError("The sync deadline must be from 1 through 86400 seconds.")
        job_id = uuid.uuid4().hex
        deadline = time.time() + seconds
        job = SyncJobResponse(job_id=job_id, state="queued", stage="waiting-for-worker", queued_at=_now(), deadline_at=datetime.fromtimestamp(deadline, timezone.utc).isoformat())
        record = {"schema": _SCHEMA, "job": job.model_dump(mode="json"), "queued_epoch": time.time(), "deadline_epoch": deadline, "pid": 0}
        # Store the exact settings at admission. A later server process can have
        # different environment variables, so the child must not reload .env.
        envelope = settings.state_dir / f"sync-job-{job_id}.json"
        # Settings carry local paths, not credentials. Restrict the envelope
        # anyway because paths can disclose the layout of a private vault.
        descriptor = os.open(envelope, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(_serialize_settings(settings), handle)
        _append(journal, record)
        python, environment = subprocess_python()
        command = [python, "-m", "ai_memory_mcp.sync_worker", str(envelope), job_id]
        options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS} if os.name == "nt" else {"start_new_session": True})
        try:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment, close_fds=True, **options)
        except OSError as exc:
            failed = {**record, "job": {**record["job"], "state": "failed", "stage": "launch", "finished_at": _now(), "error": f"The sync worker did not start: {type(exc).__name__}."}}
            _append(journal, failed)
            return SyncJobResponse.model_validate(failed["job"])
        current = _read(journal)
        if current and current["job"]["job_id"] == job_id and current["job"]["state"] in ("queued", "running"):
            # The child can enter running before Popen returns. Never replace its
            # newer state with the original queued record.
            _append(journal, {**current, "pid": child.pid})
        return job


def _transition(settings: Settings, job_id: str, **changes: object) -> bool:
    lock, journal = _paths(settings)
    with file_lock(lock, _LOCK_SECONDS):
        record = _read(journal)
        if record is None or record["job"]["job_id"] != job_id or record["job"]["state"] not in ("queued", "running"):
            return False
        if time.time() >= record["deadline_epoch"]:
            _reconcile(journal, record)
            return False
        _append(journal, {**record, "job": {**record["job"], **changes}})
        return True


def _run(envelope: Path, job_id: str) -> None:
    if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
        raise ValueError("Invalid sync job identifier")
    settings = _deserialize_settings(json.loads(envelope.read_text(encoding="utf-8")))
    lock, journal = _paths(settings)
    with file_lock(lock, _LOCK_SECONDS):
        record = _read(journal)
        if record is None or record["job"]["job_id"] != job_id or record["job"]["state"] != "queued":
            return
        deadline = record["deadline_epoch"]
    if not _transition(settings, job_id, state="running", stage="building-generation", started_at=_now()):
        return

    finished = threading.Event()

    def enforce_deadline() -> None:
        if finished.wait(max(0.0, deadline - time.time())):
            return
        # Exit the interpreter, not a Python thread blocked in native embedding code.
        os._exit(124)

    threading.Thread(target=enforce_deadline, daemon=True, name="sync-deadline").start()
    try:
        while True:
            result = MemoryService(settings).sync()
            lock, journal = _paths(settings)
            with file_lock(lock, _LOCK_SECONDS):
                latest = _read(journal)
                if latest is None or latest["job"]["job_id"] != job_id or latest["job"]["state"] != "running":
                    return
                if time.time() >= latest["deadline_epoch"]:
                    return
                if result.ok and latest["job"]["pending_refresh"]:
                    # Keep the same job nonterminal until the pass requested by
                    # a concurrent caller publishes. This transition is atomic
                    # with the pending flag, so no follow-up can be lost.
                    _append(journal, {**latest, "job": {**latest["job"], "stage": "building-followup", "pending_refresh": False}})
                    continue
                _append(journal, {**latest, "job": {**latest["job"], "state": "completed" if result.ok else "failed", "stage": "finished", "finished_at": _now(), "result": result.model_dump(mode="json"), "error": None if result.ok else "The generation build failed."}})
                return
    except BaseException as exc:
        _transition(settings, job_id, state="failed", stage="worker-error", finished_at=_now(), error=f"The sync worker failed: {type(exc).__name__}.")
    finally:
        finished.set()


def main() -> None:
    _run(Path(sys.argv[1]), sys.argv[2])


if __name__ == "__main__":
    main()
