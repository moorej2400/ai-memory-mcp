from __future__ import annotations

import asyncio
import concurrent.futures
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from ai_memory_mcp.ann import tie_aware_candidate_recall_at_k
from ai_memory_mcp.artifacts.bursts import MAX_CONTEXT_CHARACTERS, build_representations
from ai_memory_mcp.artifacts.models import ArtifactBurstRecord, ArtifactSearchHit
from ai_memory_mcp.models import EvidencePacket, SearchHit
from ai_memory_mcp.recall_worker import (
    _close_pools, _pool_for, _WarmWorker, _stop_subprocess,
    recall_in_worker_async, WorkerCancelled, WorkerDeadlineExceeded,
)
from ai_memory_mcp.retrieval import RetrievalEngine, merge_artifact_evidence
from ai_memory_mcp.server import create_server
from ai_memory_mcp.service import MemoryService
from ai_memory_mcp.text import fts_expressions, tokenize


def _hit(identity, text, score, ranks, *, segment="tail"):
    return SearchHit(
        memory_id=identity, source_id="core", path=f"core/{identity}.md",
        title=identity, heading=segment, text=text, score=score, ranks=ranks,
        segment_id=segment,
    )


def test_graph_prefix_cannot_replace_a_matching_passage(artifact_settings):
    engine = object.__new__(RetrievalEngine)
    engine.settings = artifact_settings
    lexical = _hit("manual", "The recovery marker is ORBIT-721.", .4, {"lexical": 1})
    semantic = _hit("manual", lexical.text, .8, {"semantic": 1})
    graph = _hit("manual", "General introduction.", 1.0, {"graph": 1}, segment="first")
    for rankings in (
        {"lexical": [lexical], "semantic": [semantic], "graph": [graph]},
        {"graph": [graph], "semantic": [semantic], "lexical": [lexical]},
    ):
        result = engine._fuse(deepcopy(rankings))[0]
        assert result.segment_id == "tail"
        assert "ORBIT-721" in result.text


def test_raw_lead_cannot_reverse_markdown_answer_ranking(artifact_settings):
    correct = _hit("correct", "The recovery marker is ORBIT-721.", .31, {"lexical": 1, "semantic": 2})
    unrelated = _hit("unrelated", "Storage overview.", .10, {"lexical": 2, "semantic": 1, "graph": 1})
    packet = EvidencePacket("ORBIT-721 recovery", "answered", [correct, unrelated], {}, {})
    raw = ArtifactSearchHit(
        artifact_id="art_" + "a" * 32, artifact_uri="artifact://message/art_" + "a" * 32,
        entity="message", source="synthetic", source_instance="sample",
        text="A recovery question.", score=.1,
    )
    result = merge_artifact_evidence(
        packet.query, packet, [raw], settings=artifact_settings,
        now=datetime.now(timezone.utc), limit=1,
    )
    assert result.results[0].memory_id == "correct"
    assert result.answer_status == "answered"


def test_fts_preserves_every_accepted_query_term():
    terms = [f"term{index:03}" for index in range(200)]
    query = " ".join(terms)
    assert len(query) < 2000
    assert set(terms) <= set(tokenize(" ".join(fts_expressions(query))))


def test_ann_ties_require_real_qualifying_candidates():
    assert tie_aware_candidate_recall_at_k([("a", .9)], [], 10) == 0
    assert tie_aware_candidate_recall_at_k([("a", .7), ("b", .7)], [], 10) == 0
    exact = [("a", .9), ("b", .7), ("c", .7)]
    assert tie_aware_candidate_recall_at_k(exact, [("a", .9), ("d", .7), ("e", .7)], 3) == 1
    assert tie_aware_candidate_recall_at_k(exact, [("a", .9), ("d", .2)], 3) == pytest.approx(1/3)
    assert tie_aware_candidate_recall_at_k(exact, [("d", .7)] * 3, 3) == pytest.approx(1/3)


def test_long_context_covers_the_anchor_and_preserves_its_reply():
    def record(identity, text, position, reply=None):
        return ArtifactBurstRecord(
            artifact_id=identity, artifact_uri=f"artifact://message/{identity}",
            parent_artifact_id="conversation", source="synthetic", source_instance="sample",
            entity="message", text=text, source_position=position,
            reply_target_artifact_id=reply,
            occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    question = record("question", "Where can I buy apples?", 1)
    answer = record("answer", "The long answer. " * 1500, 2, "question")
    contexts = [
        value for value in build_representations([question, answer])
        if value.anchor_artifact_uri == answer.artifact_uri and value.representation_kind == "context"
    ]
    assert len(contexts) > 1
    assert all(len(value.text) <= MAX_CONTEXT_CHARACTERS for value in contexts)
    assert all("Where can I buy apples?" in value.text for value in contexts)
    covered = set()
    for value in contexts:
        covered.update(range(value.segment_start, value.segment_end))
    assert covered == set(range(len(answer.text)))


@pytest.mark.parametrize("scope", [{}, {"artifact_kind": "message"}])
def test_missing_providers_fail_in_the_real_worker(artifact_settings, scope):
    server = create_server(artifact_settings)
    async def call():
        return await server._tool_manager.call_tool("memory_recall", {"query": "missing source", **scope})
    try:
        response = asyncio.run(call())
    finally:
        _close_pools()
    assert response.execution == "failed"
    assert response.result_kind == "empty"
    assert "artifact_unavailable" in response.reason_codes


def test_both_versions_keep_flat_structured_output(benchmark_settings):
    service = MemoryService(benchmark_settings)
    assert service.sync().ok
    server = create_server(benchmark_settings)
    async def call(version):
        return await server._tool_manager.call_tool(
            "memory_recall", {"query": "ALPHA-142", "response_version": version},
            convert_result=True,
        )
    try:
        old = asyncio.run(call("1"))[1]
        new = asyncio.run(call("2"))[1]
    finally:
        _close_pools()
    assert old["status"] == "answered"
    assert new["execution"] == "complete"
    assert "result" not in old and "result" not in new
    assert "_execution_state" not in old


@pytest.mark.parametrize("cancel", [False, True])
def test_production_pool_supervises_a_blocked_large_request_write(benchmark_settings, cancel):
    settings = replace(benchmark_settings, recall_worker_count=1)
    pool = _pool_for(settings)
    original = pool.available.get_nowait()
    _stop_subprocess(original.process)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    pool.available.put(_WarmWorker(process))
    event = threading.Event()
    started = time.monotonic()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(pool.request, b"x" * 500_000, .3, event)
            if cancel:
                time.sleep(.05)
                event.set()
            with pytest.raises(WorkerCancelled if cancel else WorkerDeadlineExceeded):
                future.result(timeout=3)
        assert time.monotonic() - started < 3
        assert process.poll() is not None
        assert pool.available.queue[0].process.pid != process.pid
    finally:
        _close_pools()


def test_recall_does_not_queue_behind_the_default_async_executor(benchmark_settings):
    assert MemoryService(benchmark_settings).sync().ok
    async def run():
        loop = asyncio.get_running_loop()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(executor)
        gate = threading.Event()
        blocked = loop.run_in_executor(None, gate.wait)
        try:
            response = await asyncio.wait_for(
                recall_in_worker_async(benchmark_settings, {"query": "ALPHA-142"}), 4
            )
            assert response.status == "answered"
        finally:
            gate.set()
            await blocked
    try:
        asyncio.run(run())
    finally:
        _close_pools()


@pytest.mark.parametrize("cancel", [False, True])
def test_production_pool_stop_releases_the_sqlite_snapshot_and_lease(benchmark_settings, cancel):
    assert MemoryService(benchmark_settings).sync().ok
    settings = replace(benchmark_settings, recall_worker_count=1)
    pool = _pool_for(settings)
    original = pool.available.get_nowait()
    _stop_subprocess(original.process)
    marker = settings.state_dir / f"snapshot-opened-{cancel}-{time.time_ns()}"
    # Inject a stuck provider inside the actual framed child. Generation pinning,
    # supervision, replacement, and lease cleanup remain the production path.
    code = "\n".join([
        "import sqlite3, time",
        "from pathlib import Path",
        "from ai_memory_mcp.service import MemoryService",
        "from ai_memory_mcp.recall_worker import _pool_child_main",
        "def stuck(self, *args, **kwargs):",
        "    db = sqlite3.connect(self.settings.artifact_db)",
        "    db.execute('BEGIN')",
        "    db.execute('SELECT count(*) FROM artifacts').fetchone()",
        f"    Path({str(marker)!r}).touch()",
        "    time.sleep(60)",
        "MemoryService._recall = stuck",
        "_pool_child_main()",
    ])
    process = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    pool.available.put(_WarmWorker(process))
    from ai_memory_mcp.recall_worker import RecallWorkerEnvelope, _serialize_settings
    payload = RecallWorkerEnvelope(settings=_serialize_settings(settings),
                                   arguments={"query": "release snapshot"}).model_dump_json().encode()
    event = threading.Event()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(pool.request, payload, 2, event)
            deadline = time.monotonic() + 1.8
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            assert marker.exists()
            leases = list(settings.state_dir.glob(f".generation-lease-*-{process.pid}-*.json"))
            assert leases
            if cancel:
                event.set()
            with pytest.raises(WorkerCancelled if cancel else WorkerDeadlineExceeded):
                future.result(timeout=4)
        assert process.poll() is not None
        assert not list(settings.state_dir.glob(f".generation-lease-*-{process.pid}-*.json"))
        assert list((settings.state_dir / "retired-generation-leases").glob(f"*-{process.pid}-*.json"))
        with sqlite3.connect(settings.artifact_db, timeout=.2) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("BEGIN EXCLUSIVE")
            connection.rollback()
    finally:
        _close_pools()


def test_parent_status_freshness_reaches_the_worker(benchmark_settings, tmp_path):
    shutil.copytree(benchmark_settings.memory_root, tmp_path / "vault")
    benchmark_settings = replace(benchmark_settings, memory_root=tmp_path / "vault", state_dir=tmp_path / "state")
    service = MemoryService(benchmark_settings)
    assert service.sync().ok
    note = next(benchmark_settings.memory_root.rglob("*.md"))
    note.write_text(note.read_text() + "\nA new source revision.\n")
    assert service.status().index.stale
    try:
        response = asyncio.run(recall_in_worker_async(benchmark_settings, {"query": "ALPHA-142"}))
    finally:
        _close_pools()
    assert response._execution_state.execution == "partial"
    assert "markdown_stale" in response._execution_state.reason_codes


def test_server_background_reconciliation_detects_edits(benchmark_settings, tmp_path):
    shutil.copytree(benchmark_settings.memory_root, tmp_path / "vault")
    benchmark_settings = replace(benchmark_settings, memory_root=tmp_path / "vault", state_dir=tmp_path / "state")
    assert MemoryService(benchmark_settings).sync().ok
    server = create_server(benchmark_settings)
    async def run():
        async with server._mcp_server.lifespan(server._mcp_server):
            note = next(benchmark_settings.memory_root.rglob("*.md"))
            note.write_text(note.read_text() + "\nA background freshness change.\n")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                marker = json.loads((benchmark_settings.state_dir / "markdown-freshness.json").read_text())
                if marker["stale"]:
                    break
                await asyncio.sleep(.05)
            assert marker["stale"] is True
    asyncio.run(run())


def test_audit_process_lock_obeys_the_timeout_budget(benchmark_settings):
    from ai_memory_mcp.audit import _PROCESS_LOCK, append_event
    settings = replace(benchmark_settings, audit_lock_timeout_seconds=.05)
    with _PROCESS_LOCK:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            started = time.monotonic()
            assert executor.submit(append_event, settings, "retrieval", "synthetic-timeout", {}).result(timeout=1) is False
            assert time.monotonic() - started < .5


@pytest.mark.parametrize("error", ["queue_full", "worker_failed"])
def test_worker_failures_remain_serializable(benchmark_settings, monkeypatch, error):
    import ai_memory_mcp.server as module
    from ai_memory_mcp.recall_worker import WorkerQueueFull, WorkerExecutionFailed
    async def fail(*args, **kwargs):
        raise (WorkerQueueFull if error == "queue_full" else WorkerExecutionFailed)("Synthetic failure")
    monkeypatch.setattr(module, "recall_in_worker_async", fail)
    async def call():
        return await create_server(benchmark_settings)._tool_manager.call_tool(
            "memory_recall", {"query": "synthetic query"}, convert_result=True
        )
    payload = asyncio.run(call())[1]
    assert payload["execution"] == "failed"
    assert payload["result_kind"] == "empty"
    assert payload["reason_codes"] == [error]
