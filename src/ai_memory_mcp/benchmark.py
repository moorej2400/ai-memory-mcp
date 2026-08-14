from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .index import build_index
from .service import MemoryService


def _benchmark_root() -> Path:
    return Path(__file__).resolve().parents[2] / "benchmarks"


def contract_digest(root: Path) -> tuple[str, list[str]]:
    paths = [root / "cases.json", root / "fixtures" / "graph.json"]
    paths.extend(sorted((root / "fixtures" / "vault").rglob("*.md")))
    digest = hashlib.sha256()
    names: list[str] = []
    for path in paths:
        relative = path.relative_to(root).as_posix()
        names.append(relative)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest(), names


def verify_contract(root: Path) -> dict[str, Any]:
    lock_path = root / "benchmark-lock.json"
    if not lock_path.exists():
        raise RuntimeError("Benchmark is not frozen: benchmark-lock.json is missing")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    digest, files = contract_digest(root)
    if lock.get("sha256") != digest or lock.get("files") != files:
        raise RuntimeError(
            "Frozen benchmark drift detected; restore the contract instead of "
            "changing it during performance work"
        )
    return lock


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)
    return ordered[max(index, 0)]


def _benchmark_settings(root: Path, state_dir: Path) -> Settings:
    return Settings(
        memory_root=root / "fixtures" / "vault",
        state_dir=state_dir,
        graph_path=root / "fixtures" / "graph.json",
        graphify_mcp_url="",
        embedding_provider="hashed",
        artifact_db=state_dir / "artifacts.sqlite3",
        artifact_objects_dir=state_dir / "artifact-objects",
        artifact_backup_dir=state_dir / "artifact-backups",
    )


def run_benchmark(label: str) -> dict[str, Any]:
    root = _benchmark_root()
    lock = verify_contract(root)
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    state_dir = root / "runs" / f"state-{run_stamp}"
    settings = _benchmark_settings(root, state_dir)
    index_result = build_index(settings, force=True)
    service = MemoryService(settings)
    cases = json.loads((root / "cases.json").read_text(encoding="utf-8"))["cases"]
    results: list[dict[str, Any]] = []
    latencies: list[float] = []
    reciprocal_ranks: list[float] = []
    recall_at_1 = recall_at_5 = correct_no_answer = 0
    no_answer_cases = 0
    scope_leaks = citation_failures = 0
    tag_stats: dict[str, list[bool]] = defaultdict(list)
    for case in cases:
        scope = case.get("scope", {})
        started = time.perf_counter()
        packet = service.recall(case["query"], limit=5, **scope)
        elapsed = (time.perf_counter() - started) * 1000
        latencies.append(elapsed)
        ids = [item.memory_id for item in packet.evidence]
        expected = set(case.get("expected_any", []))
        forbidden = set(case.get("forbidden", []))
        if case.get("no_answer"):
            no_answer_cases += 1
            passed = packet.status == "no_answer"
            correct_no_answer += int(passed)
            rank = None
        else:
            ranks = [ids.index(value) + 1 for value in expected if value in ids]
            rank = min(ranks) if ranks else None
            passed = rank is not None
            reciprocal_ranks.append(1.0 / rank if rank else 0.0)
            recall_at_1 += int(rank == 1)
            recall_at_5 += int(rank is not None and rank <= 5)
        leaked = bool(forbidden & set(ids))
        scope_leaks += int(leaked)
        expected_path = case.get("expected_path")
        citation_ok = not expected_path or any(
            citation.path.endswith(expected_path)
            for citation in packet.citations
        )
        citation_failures += int(not citation_ok)
        final_pass = passed and not leaked and citation_ok
        for tag in case.get("tags", []):
            tag_stats[tag].append(final_pass)
        results.append(
            {
                "id": case["id"],
                "passed": final_pass,
                "rank": rank,
                "status": packet.status,
                "returned": ids,
                "latency_ms": round(elapsed, 3),
                "leaked": leaked,
                "citation_ok": citation_ok,
            }
        )
    answered_cases = len(cases) - no_answer_cases
    metrics = {
        "cases": len(cases),
        "passed": sum(int(result["passed"]) for result in results),
        "pass_rate": statistics.mean(result["passed"] for result in results),
        "recall_at_1": recall_at_1 / answered_cases,
        "recall_at_5": recall_at_5 / answered_cases,
        "mrr": statistics.mean(reciprocal_ranks),
        "no_answer_accuracy": correct_no_answer / no_answer_cases,
        "scope_leakage_rate": scope_leaks / len(cases),
        "citation_failure_rate": citation_failures / len(cases),
        "latency_p50_ms": statistics.median(latencies),
        "latency_p95_ms": _percentile(latencies, 0.95),
        "index_elapsed_ms": index_result["elapsed_ms"],
        "tag_pass_rate": {
            tag: statistics.mean(values) for tag, values in sorted(tag_stats.items())
        },
    }
    report = {
        "label": label,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "benchmark_sha256": lock["sha256"],
        "metrics": metrics,
        "cases": results,
    }
    output = root / "runs" / f"{run_stamp}-{label}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["output"] = str(output)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the frozen AI-memory benchmark")
    parser.add_argument("--label", default="manual")
    args = parser.parse_args()
    print(json.dumps(run_benchmark(args.label), indent=2))


if __name__ == "__main__":
    main()
