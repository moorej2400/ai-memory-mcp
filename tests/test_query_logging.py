from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import threading
import time
from dataclasses import replace

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ai_memory_mcp import query_log
from ai_memory_mcp import ann
from ai_memory_mcp.ann import quantized_shortlist
from ai_memory_mcp.models import RecallResponse
from ai_memory_mcp.real_world_benchmark import _mcp_environment
from ai_memory_mcp.recall_worker import _close_pools, _pool_for, _stop_subprocess, _WarmWorker, WorkerExecutionFailed
from ai_memory_mcp.server import _execute_recall, create_server
from ai_memory_mcp.service import MemoryService


def records(settings):
    assert query_log.flush_query_logs(3)
    directory = settings.resolved_log_dir / "queries"
    return [json.loads(line) for path in sorted(directory.glob("requests*.jsonl"))
            for line in path.read_text().splitlines()]


def injected_worker(settings, code):
    pool = _pool_for(settings)
    original = pool.available.get_nowait()
    _stop_subprocess(original.process)
    program = code + "\nfrom ai_memory_mcp.recall_worker import _pool_child_main\n_pool_child_main()"
    process = subprocess.Popen([sys.executable, "-c", program], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    pool.available.put(_WarmWorker(process))
    return process


@pytest.mark.parametrize("block_size", [3, 31, 1000])
@pytest.mark.parametrize("limit", [1, 17, 200])
def test_compact_shortlist_keeps_the_previous_candidate_order(block_size, limit):
    np = pytest.importorskip("numpy")
    rng = np.random.default_rng(12)
    # A small value range creates many boundary ties. The local partition and
    # global score/identity ordering must match the previous implementation.
    vectors = rng.integers(-2, 3, (143, 12), dtype=np.int8)
    query = rng.integers(-2, 3, 12, dtype=np.int8)
    rows = [(f"vector-{index:04}", vector.tobytes()) for index, vector in enumerate(vectors)]
    expected = []
    for offset in range(0, len(rows), block_size):
        block = rows[offset:offset + block_size]
        scores = vectors[offset:offset + block_size] @ query.astype(np.float32)
        keep = min(limit, len(block))
        selected = np.argpartition(scores, -keep)[-keep:] if keep < len(block) else range(len(block))
        expected.extend((-float(scores[index]), block[index][0]) for index in selected)
        expected = sorted(expected)[:limit]
    assert quantized_shortlist(iter(rows), query.tobytes(), 12, limit, block_size=block_size) == expected


def test_compact_shortlist_does_not_pad_every_candidate_to_the_longest_identity(monkeypatch):
    np = pytest.importorskip("numpy")
    class NumpyProxy:
        def __getattr__(self, name):
            return getattr(np, name)

        def asarray(self, values, *args, **kwargs):
            if values and isinstance(values[0], str):
                assert kwargs.get("dtype") is object
            return np.asarray(values, *args, **kwargs)

    monkeypatch.setattr(ann, "_numpy", lambda: NumpyProxy())
    query = np.ones(4, dtype=np.int8).tobytes()
    rows = [("x" * 65_536, query), ("short-id", query)]
    assert len(quantized_shortlist(rows, query, 4, 1, block_size=2)) == 1


def test_content_logging_is_explicit_and_does_not_change_digest_audit(benchmark_settings, tmp_path):
    settings = replace(benchmark_settings, log_dir=tmp_path / "logs")
    assert MemoryService(settings).sync().ok
    MemoryService(settings).recall("ALPHA-142")
    assert not (settings.resolved_log_dir / "queries").exists()
    settings = replace(settings, query_log_content=True)
    response = MemoryService(settings).recall("ALPHA-142", limit=3)
    events = records(settings)
    start = next(event for event in events if event["event"] == "query_started")
    end = next(event for event in events if event["event"] == "query_completed")
    assert start["arguments"]["query"] == "ALPHA-142"
    assert start["arguments"]["limit"] == 3
    assert end["response"] == response.model_dump(mode="json")
    assert end["request_id"] == start["request_id"]
    assert end["elapsed_ms"] > 0
    assert end["diagnostics"]["generation_id"]
    assert end["diagnostics"]["pin_ms"] >= 0
    assert end["execution_state"]["execution"] == "complete"
    assert "ALPHA-142" not in (settings.resolved_log_dir / "retrieval.jsonl").read_text()
    assert MemoryService(settings).status().logging.query_log.content_enabled
    if os.name != "nt":
        directory = settings.resolved_log_dir / "queries"
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE((directory / "requests.jsonl").stat().st_mode) == 0o600


def test_rotation_preserves_complete_records(artifact_settings):
    settings = replace(artifact_settings, query_log_content=True, audit_log_max_bytes=1)
    for number in range(6):
        with query_log.QueryTrace(settings, {"query": f"synthetic query {number}"}, "library"):
            pass
    events = records(settings)
    assert len(events) == 12
    assert len(list((settings.resolved_log_dir / "queries").glob("requests*.jsonl"))) == 12
    assert len({event["request_id"] for event in events}) == 6


@pytest.mark.parametrize("transport", ["library", "worker"])
def test_slow_writer_is_bounded_and_reports_rejected_records(artifact_settings, monkeypatch, transport):
    assert query_log.flush_query_logs(3)
    settings = replace(artifact_settings, query_log_content=True)
    original = query_log._write
    entered, release = threading.Event(), threading.Event()
    def slow_write(*args):
        entered.set()
        release.wait(3)
        return original(*args)
    monkeypatch.setattr(query_log, "_write", slow_write)
    monkeypatch.setattr(query_log, "_MAX_PENDING_EVENTS", 2)
    before = query_log.query_logging_status(settings)["failed_events"]
    try:
        started = time.monotonic()
        with query_log.QueryTrace(settings, {"query": "first query"}, transport):
            assert entered.wait(1)
        for number in range(10):
            with query_log.QueryTrace(settings, {"query": f"query {number}"}, transport):
                pass
        assert time.monotonic() - started < 1
        status = query_log.query_logging_status(settings)
        assert status["failed_events"] > before
        assert status["pending_events"] <= 3  # Two queued events and one active write.
        assert not query_log.flush_query_logs(.01)
    finally:
        release.set()
        assert query_log.flush_query_logs(3)


def test_log_failure_does_not_replace_a_result(artifact_settings):
    settings = replace(artifact_settings, query_log_content=True)
    settings.resolved_log_dir.mkdir(parents=True)
    (settings.resolved_log_dir / "queries").write_text("A synthetic conflicting file.")
    before = query_log.query_logging_status(settings)["failed_events"]
    with query_log.QueryTrace(settings, {"query": "unwritable log"}, "library") as trace:
        trace.set_result(RecallResponse(query="unwritable log", status="no_answer", intent="search"))
    assert query_log.flush_query_logs(3)
    assert query_log.query_logging_status(settings)["failed_events"] == before + 2


def test_brief_cross_process_lock_contention_does_not_drop_records(artifact_settings):
    from ai_memory_mcp.audit import file_lock
    settings = replace(artifact_settings, query_log_content=True, audit_lock_timeout_seconds=1)
    before = query_log.query_logging_status(settings)["failed_events"]
    directory = settings.resolved_log_dir / "queries"
    with file_lock(directory / "query.lock", 1):
        started = time.monotonic()
        with query_log.QueryTrace(settings, {"query": "contended log"}, "library"):
            pass
        assert time.monotonic() - started < .1
        # The writer can wait past a scheduler slice without dropping the record.
        time.sleep(.2)
    assert len(records(settings)) == 2
    assert query_log.query_logging_status(settings)["failed_events"] == before


def test_audit_switch_and_benchmark_environment_control_private_logging(artifact_settings, monkeypatch):
    monkeypatch.setenv("AI_MEMORY_QUERY_LOG_CONTENT", "true")
    assert _mcp_environment(artifact_settings)["AI_MEMORY_QUERY_LOG_CONTENT"] == "false"
    settings = replace(artifact_settings, query_log_content=True, audit_logging_enabled=False)
    with query_log.QueryTrace(settings, {"query": "do not record"}, "library"):
        pass
    assert records(settings) == []


def test_serialized_mcp_response_matches_private_log_for_both_versions(benchmark_settings, tmp_path):
    settings = replace(benchmark_settings, query_log_content=True, log_dir=tmp_path / "logs")
    assert MemoryService(settings).sync().ok
    async def run():
        parameters = StdioServerParameters(command=sys.executable,
            args=["-m", "ai_memory_mcp.server", "--transport", "stdio"], env=_mcp_environment(settings))
        responses = {}
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                for version in ("1", "2"):
                    result = await session.call_tool("memory_recall", {
                        "query": "ALPHA-142", "response_version": version, "limit": 3,
                    })
                    assert not result.isError
                    responses[version] = result.structuredContent
                # Parent completion does not flush the worker's independent queue.
                # Keep the server alive until both correlated worker traces arrive.
                for _ in range(100):
                    events = records(settings)
                    completed = [e for e in events if e["event"] == "query_completed"]
                    request_ids = {event["request_id"] for event in completed}
                    worker_ids = {event["request_id"] for event in events
                                  if event["event"] == "worker_started"}
                    staged_ids = {event["request_id"] for event in events
                                  if event["event"] == "stage_completed"}
                    if len(completed) == 2 and request_ids <= worker_ids & staged_ids:
                        break
                    await asyncio.sleep(.01)
                else:
                    pytest.fail("Correlated worker query logs did not arrive before server shutdown.")
        return responses
    responses = asyncio.run(run())
    events = records(settings)
    for version, response in responses.items():
        end = next(e for e in events if e["event"] == "query_completed" and e["response_version"] == version)
        assert end["response"] == response
        assert end["timing"]["queue_ms"] >= 0
        assert end["timing"]["worker_ms"] > 0
        assert end["timing"]["worker_pid"] > 0
        assert end["diagnostics"]["generation_id"]
        matching = [e for e in events if e["request_id"] == end["request_id"]]
        assert len([e for e in matching if e["event"] == "query_started"]) == 1
        assert any(e["event"] == "worker_started" for e in matching)
        assert any(e["event"] == "stage_started" and e["stage"] == "generation_pin" for e in matching)
        assert any(e["event"] == "stage_completed" for e in matching)


@pytest.mark.parametrize("cancel", [False, True])
def test_stuck_worker_leaves_correlated_progress_and_terminal_log(benchmark_settings, tmp_path, cancel):
    settings = replace(benchmark_settings, query_log_content=True, log_dir=tmp_path / "logs",
                       recall_worker_count=1, recall_timeout_seconds=2)
    assert MemoryService(settings).sync().ok
    pool = _pool_for(settings)
    original = pool.available.get_nowait()
    _stop_subprocess(original.process)
    # Inject one stuck provider into the actual framed child. Real generation
    # pinning, request IDs, supervision, and worker replacement stay enabled.
    code = "\n".join([
        "import time", "from ai_memory_mcp.service import MemoryService",
        "from ai_memory_mcp.query_log import query_stage",
        "from ai_memory_mcp.recall_worker import _pool_child_main",
        "@query_stage('synthetic_stuck_provider')",
        "def stuck(self, *args, **kwargs): time.sleep(60)",
        "MemoryService._recall = stuck", "_pool_child_main()",
    ])
    process = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    pool.available.put(_WarmWorker(process))
    async def run():
        task = asyncio.create_task(_execute_recall(settings, {"query": "stuck source"}, "2"))
        if cancel:
            for _ in range(150):
                if any(e.get("stage") == "synthetic_stuck_provider" for e in records(settings)):
                    break
                await asyncio.sleep(.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            response = await task
            assert response.reason_codes == ["deadline_exceeded"]
    try:
        asyncio.run(run())
        events = records(settings)
        last = next(e for e in events if e["event"] == ("query_cancelled" if cancel else "query_completed"))
        assert last["timing"]["outcome"] == ("cancelled" if cancel else "deadline_exceeded")
        assert last["timing"]["replacement"]
        assert last["timing"]["worker_pid"] == process.pid
        assert process.poll() is not None
        assert any(e["request_id"] == last["request_id"] and e.get("stage") == "synthetic_stuck_provider"
                   and e["event"] == "stage_started" for e in events)
        assert not any(e.get("stage") == "synthetic_stuck_provider" and e["event"] == "stage_completed" for e in events)
    finally:
        _close_pools()


@pytest.mark.parametrize("cancel", [False, True])
def test_expiry_before_executor_start_records_queue_time(artifact_settings, cancel):
    settings = replace(artifact_settings, query_log_content=True, recall_worker_count=1,
                       recall_timeout_seconds=.15)
    pool = _pool_for(settings)
    entered, release = threading.Event(), threading.Event()
    def occupy_manager():
        entered.set()
        release.wait(3)
    blocked = pool.managers.submit(occupy_manager)
    assert entered.wait(1)
    async def run():
        task = asyncio.create_task(_execute_recall(settings, {"query": "queued request"}, "2"))
        if cancel:
            await asyncio.sleep(.04)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task).reason_codes == ["deadline_exceeded"]
    try:
        asyncio.run(run())
        end = next(e for e in records(settings) if e["event"] == ("query_cancelled" if cancel else "query_completed"))
        assert end["timing"]["queue_ms"] >= 20
        assert end["timing"]["executor_queue_ms"] == end["timing"]["queue_ms"]
        assert "worker_pid" not in end["timing"]
    finally:
        release.set()
        blocked.result(3)
        _close_pools()


@pytest.mark.parametrize("version", ["1", "2"])
def test_worker_error_envelope_has_failed_timing(benchmark_settings, tmp_path, version):
    settings = replace(benchmark_settings, query_log_content=True, log_dir=tmp_path / "logs", recall_worker_count=1)
    assert MemoryService(settings).sync().ok
    injected_worker(settings, "\n".join([
        "from ai_memory_mcp.service import MemoryService",
        "def failed(self, *args, **kwargs): raise RuntimeError('Synthetic provider failure.')",
        "MemoryService._recall = failed",
    ]))
    try:
        if version == "1":
            with pytest.raises(WorkerExecutionFailed):
                asyncio.run(_execute_recall(settings, {"query": "provider failure"}, version))
        else:
            response = asyncio.run(_execute_recall(settings, {"query": "provider failure"}, version))
            assert response.reason_codes == ["worker_failed"]
        end = next(e for e in records(settings) if e["event"] == ("query_failed" if version == "1" else "query_completed"))
        assert end["timing"]["outcome"] == "worker_failed"
        assert "RuntimeError" in end["diagnostics"]["worker_error"]["message"]
    finally:
        _close_pools()


def test_worker_terminal_log_failure_reaches_parent_status(benchmark_settings, tmp_path):
    settings = replace(benchmark_settings, query_log_content=True, log_dir=tmp_path / "logs", recall_worker_count=1)
    assert MemoryService(settings).sync().ok
    before = query_log.query_logging_status(settings)["worker_failed_events"]
    injected_worker(settings, "\n".join([
        "from ai_memory_mcp import query_log",
        "original = query_log._submit",
        "def fail_terminal(settings, encoded):",
        "    if b'\"event\":\"worker_completed\"' in encoded:",
        "        query_log._failure('Synthetic terminal log failure.')",
        "        return False",
        "    return original(settings, encoded)",
        "query_log._submit = fail_terminal",
    ]))
    try:
        for index in range(2):
            response = asyncio.run(_execute_recall(settings, {"query": "ALPHA-142"}, "2"))
            assert response.execution == "complete"
            # The child sends a delta, not its cumulative total on every reply.
            assert MemoryService(settings).status().logging.query_log.worker_failed_events == before + index + 1
        end = [e for e in records(settings) if e["event"] == "query_completed"][-1]
        assert end["diagnostics"]["log_events_failed"] == 1
        assert end["diagnostics"]["worker_query_log"]["failed_events"] == 1
    finally:
        _close_pools()


def test_blocked_worker_log_disk_does_not_fail_recall(benchmark_settings, tmp_path):
    settings = replace(benchmark_settings, query_log_content=True, log_dir=tmp_path / "logs",
                       recall_worker_count=1, recall_timeout_seconds=3)
    assert MemoryService(settings).sync().ok
    injected_worker(settings, "\n".join([
        "import time", "from ai_memory_mcp import query_log",
        "def blocked(*args): time.sleep(60)", "query_log._write = blocked",
    ]))
    try:
        response = asyncio.run(_execute_recall(settings, {"query": "ALPHA-142"}, "2"))
        assert response.execution == "complete"
        assert response.evidence
        end = next(e for e in records(settings) if e["event"] == "query_completed")
        assert end["diagnostics"]["worker_query_log"]["pending_events"] > 0
        assert end["timing"]["outcome"] == "complete"
    finally:
        _close_pools()


def test_planned_worker_retirement_flushes_queued_logs(benchmark_settings, tmp_path):
    settings = replace(benchmark_settings, query_log_content=True, log_dir=tmp_path / "logs",
                       recall_worker_count=1, recall_worker_max_requests=1)
    assert MemoryService(settings).sync().ok
    process = injected_worker(settings, "\n".join([
        "import time", "from ai_memory_mcp import query_log",
        "original = query_log._write",
        "def delayed(*args): time.sleep(.15); return original(*args)",
        "query_log._write = delayed",
    ]))
    try:
        response = asyncio.run(_execute_recall(settings, {"query": "ALPHA-142"}, "2"))
        assert response.execution == "complete"
        replacement = _pool_for(settings).available.get_nowait()
        try:
            assert replacement.process.pid != process.pid
            assert replacement.process.poll() is None
        finally:
            _pool_for(settings).available.put(replacement)
        for _ in range(300):
            events = records(settings)
            completed = next(event for event in events if event["event"] == "query_completed")
            matching = [event for event in events if event["request_id"] == completed["request_id"]]
            if process.poll() is not None and any(
                event["event"] == "worker_completed" for event in matching
            ):
                break
            time.sleep(.01)
        else:
            pytest.fail("The retiring worker did not flush its correlated logs.")
        assert any(event["event"] == "worker_completed" for event in matching)
        assert any(event["event"] == "stage_completed" for event in matching)
    finally:
        _close_pools()


def test_slow_retirement_cannot_bypass_the_worker_process_bound(benchmark_settings):
    settings = replace(benchmark_settings, recall_worker_count=1, recall_worker_max_requests=1)
    pool = _pool_for(settings)
    worker = pool.available.get_nowait()
    with pool.retiring_lock:
        pool.retiring.add(worker.process)
    try:
        assert pool._retire(worker) is worker
        assert len(pool.retiring) == settings.recall_worker_count
    finally:
        with pool.retiring_lock:
            pool.retiring.discard(worker.process)
        pool.available.put(worker)
        _close_pools()


def test_ordered_artifact_read_keeps_mcp_operation_and_response(artifact_settings):
    from ai_memory_mcp.artifacts.identity import artifact_id, artifact_uri
    from ai_memory_mcp.artifacts.store import ArtifactStore
    from test_artifact_retrieval import _raw_batch
    settings = replace(artifact_settings, query_log_content=True)
    ArtifactStore(settings).apply_batch(_raw_batch())
    reference = artifact_uri("message", artifact_id("chat-source", "workspace", "message", "message-1"))
    server = create_server(settings)
    async def run():
        return await server._tool_manager.call_tool("memory_artifact_read", {"reference": reference})
    response = asyncio.run(run())
    events = records(settings)
    starts = [e for e in events if e["event"] == "query_started"]
    assert len(starts) == 1
    assert starts[0]["operation"] == "memory_artifact_read"
    assert starts[0]["transport"] == "mcp"
    end = next(e for e in events if e["event"] == "query_completed")
    assert end["response"] == response.model_dump(mode="json")
