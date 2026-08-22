"""Run a bounded gate through the public AI Memory recall pipeline."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from ai_memory_mcp.config import Settings  # noqa: E402
from ai_memory_mcp.service import MemoryService  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import load_environment, repository_root  # noqa: E402


def retrieval_cases() -> tuple[tuple[str, str], ...]:
    raw = os.getenv("GRAPHIFY_MEMORY_RETRIEVAL_EVAL_CASES", "").strip()
    if not raw:
        return ()
    configured = json.loads(raw)
    if not isinstance(configured, list):
        raise ValueError("Retrieval evaluation cases must be a JSON list.")
    cases: list[tuple[str, str]] = []
    for item in configured:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(value, str) and value for value in item)
        ):
            raise ValueError(
                "Each retrieval evaluation case must contain two strings."
            )
        cases.append((item[0], item[1]))
    return tuple(cases)


def main() -> int:
    load_environment(repository_root())
    settings = Settings.from_env()
    service = MemoryService(settings)
    cases = retrieval_cases()
    if not cases:
        raise ValueError(
            "Set GRAPHIFY_MEMORY_RETRIEVAL_EVAL_CASES to real questions and "
            "expected evidence markers."
        )
    failures: list[str] = []
    for index, (question, expected) in enumerate(cases, start=1):
        response = service.recall(question, limit=5)
        evidence = json.dumps(
            {
                "evidence": [
                    item.model_dump(mode="json") for item in response.evidence
                ],
                "citations": [
                    item.model_dump(mode="json") for item in response.citations
                ],
            }
        )
        if response.status != "answered" or expected not in evidence:
            failures.append(f"Retrieval case {index} did not return its marker.")

    status = service.status()
    summary = {
        "cases": len(cases),
        "passed": len(cases) - len(failures),
        "pipeline": "memory_recall",
        "generationId": status.generation.generation_id,
        "generationConsistent": status.generation.consistent,
        "failures": failures,
    }
    print(json.dumps(summary, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
