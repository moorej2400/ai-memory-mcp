from __future__ import annotations

import os
import time
from dataclasses import replace

import pytest

from ai_memory_mcp.models import SyncResponse
from ai_memory_mcp import sync_worker


def test_enqueue_coalesces_across_instances_and_keeps_journal(benchmark_settings, monkeypatch, tmp_path):
    settings = replace(benchmark_settings, state_dir=tmp_path / "jobs")
    launches = []

    class Child:
        pid = os.getpid()

    monkeypatch.setattr(sync_worker.subprocess, "Popen", lambda *args, **kwargs: launches.append(args) or Child())
    first = sync_worker.enqueue_sync(settings)
    second = sync_worker.enqueue_sync(settings)
    assert first.state == "queued"
    assert first.result is None
    assert second.job_id == first.job_id and second.coalesced
    assert len(launches) == 1
    assert sync_worker.sync_job_status(settings).job_id == first.job_id
    assert (settings.state_dir / "sync-jobs.jsonl").is_file()


def test_worker_completes_synthetic_sync_and_reports_result(benchmark_settings, monkeypatch, tmp_path):
    settings = replace(benchmark_settings, state_dir=tmp_path / "jobs")
    class Child:
        pid = os.getpid()
    monkeypatch.setattr(sync_worker.subprocess, "Popen", lambda *args, **kwargs: Child())
    job = sync_worker.enqueue_sync(settings)
    class Service:
        def __init__(self, settings):
            pass
        def sync(self):
            return SyncResponse(ok=True, generation_id="synthetic")
    monkeypatch.setattr(sync_worker, "MemoryService", Service)
    sync_worker._run(settings.state_dir / f"sync-job-{job.job_id}.json", job.job_id)
    final = sync_worker.sync_job_status(settings)
    assert final.state == "completed"
    assert final.result.ok and final.result.generation_id == "synthetic"
    assert final.started_at and final.finished_at


def test_request_during_running_build_schedules_followup(benchmark_settings, monkeypatch, tmp_path):
    settings = replace(benchmark_settings, state_dir=tmp_path / "jobs")
    launches = []

    class Child:
        pid = os.getpid()

    monkeypatch.setattr(sync_worker.subprocess, "Popen", lambda *args, **kwargs: launches.append(args) or Child())
    first = sync_worker.enqueue_sync(settings)
    observed = []
    passes = []

    class Service:
        def __init__(self, settings):
            pass
        def sync(self):
            passes.append(len(passes) + 1)
            if len(passes) == 1:
                observed.append(sync_worker.enqueue_sync(settings))
                assert sync_worker.sync_job_status(settings, first.job_id).state == "running"
            return SyncResponse(ok=True, generation_id=f"synthetic-{len(passes)}")

    monkeypatch.setattr(sync_worker, "MemoryService", Service)
    sync_worker._run(settings.state_dir / f"sync-job-{first.job_id}.json", first.job_id)
    assert observed[0].job_id == first.job_id and observed[0].pending_refresh
    followup = sync_worker.sync_job_status(settings, first.job_id)
    assert followup.job_id == first.job_id
    assert passes == [1, 2]
    assert followup.state == "completed" and followup.result.generation_id == "synthetic-2"
    assert len(launches) == 1


def test_exited_worker_is_failed_before_new_admission(benchmark_settings, monkeypatch, tmp_path):
    settings = replace(benchmark_settings, state_dir=tmp_path / "jobs")
    class Child:
        pid = 87654321
    monkeypatch.setattr(sync_worker.subprocess, "Popen", lambda *args, **kwargs: Child())
    monkeypatch.setattr(sync_worker, "_alive", lambda pid: False)
    first = sync_worker.enqueue_sync(settings)
    # The first launch has no PID heartbeat. A failed launch cannot be
    # re-admitted until the narrow startup grace passes.
    lock, journal = sync_worker._paths(settings)
    with sync_worker.file_lock(lock, 5):
        record = sync_worker._read(journal)
        sync_worker._append(journal, {**record, "queued_epoch": time.time() - 6})
    failed = sync_worker.sync_job_status(settings)
    assert failed.state == "failed"
    second = sync_worker.enqueue_sync(settings)
    assert second.job_id != first.job_id


def test_corrupt_journal_fails_closed(benchmark_settings, tmp_path):
    settings = replace(benchmark_settings, state_dir=tmp_path / "jobs")
    settings.state_dir.mkdir(parents=True)
    (settings.state_dir / "sync-jobs.jsonl").write_bytes(b"partial")
    with pytest.raises(RuntimeError, match="incomplete"):
        sync_worker.enqueue_sync(settings)


def test_deadline_does_not_claim_publication_failed_while_worker_lives(benchmark_settings, monkeypatch, tmp_path):
    settings = replace(benchmark_settings, state_dir=tmp_path / "jobs")
    class Child:
        pid = os.getpid()
    monkeypatch.setattr(sync_worker.subprocess, "Popen", lambda *args, **kwargs: Child())
    job = sync_worker.enqueue_sync(settings)
    lock, journal = sync_worker._paths(settings)
    with sync_worker.file_lock(lock, 5):
        record = sync_worker._read(journal)
        sync_worker._append(journal, {**record, "deadline_epoch": time.time() - 1})
    assert sync_worker.sync_job_status(settings).state == "queued"
    monkeypatch.setattr(sync_worker, "_alive", lambda pid: False)
    stale = sync_worker.sync_job_status(settings)
    assert stale.state == "failed"
    assert stale.result is None
    assert "Publication is unknown" in stale.error


def test_real_subprocess_publishes_isolated_generation(benchmark_settings, tmp_path):
    from ai_memory_mcp.artifacts.schema import migrate_artifact_db

    settings = replace(
        benchmark_settings,
        state_dir=tmp_path / "indexes",
        artifact_db=tmp_path / "raw" / "artifacts.sqlite3",
        artifact_objects_dir=tmp_path / "raw" / "objects",
        artifact_backup_dir=tmp_path / "backups",
        log_dir=tmp_path / "logs",
    )
    migrate_artifact_db(settings)
    job = sync_worker.enqueue_sync(settings)
    assert job.state == "queued" and job.result is None
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        current = sync_worker.sync_job_status(settings, job.job_id)
        if current.state in ("completed", "failed"):
            break
        time.sleep(0.2)
    else:
        pytest.fail("The isolated sync worker did not report a result within 45 seconds")
    assert current.state == "completed", current.error
    assert current.result is not None and current.result.ok
    assert current.result.generation_id
