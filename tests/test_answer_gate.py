from __future__ import annotations

from pathlib import Path

import pytest

from ai_memory_mcp.config import Settings
from ai_memory_mcp.index import build_index
from ai_memory_mcp.service import MemoryService

NOTES = (
    (
        "Tools/Log Console Access.md",
        "mem-console",
        "Log Console Access",
        "Authenticate to the central log console with your SSO account. "
        "Request the observability role first.",
    ),
    (
        "Tools/Deploy Runbook.md",
        "mem-deploy",
        "Deploy Runbook",
        "Rotate the deploy token before each release.",
    ),
    (
        "Tools/Backup Policy.md",
        "mem-backup",
        "Backup Policy",
        "Nightly snapshots are retained for thirty days in cold storage.",
    ),
)


def _service(tmp_path: Path, provider: str) -> MemoryService:
    vault = tmp_path / "vault"
    (vault / "Tools").mkdir(parents=True, exist_ok=True)
    for relative, memory_id, title, body in NOTES:
        (vault / relative).write_text(
            "\n".join(
                (
                    "---",
                    f"memory_id: {memory_id}",
                    f"title: {title}",
                    "status: active",
                    "updated: 2026-07-20",
                    "---",
                    "",
                    f"# {title}",
                    "",
                    body,
                    "",
                )
            ),
            encoding="utf-8",
        )
    settings = Settings(
        memory_root=vault,
        state_dir=tmp_path / f"state-{provider}",
        graph_path=tmp_path / "graph.json",
        graphify_mcp_url="",
        embedding_provider=provider,
    )
    build_index(settings, force=True)
    return MemoryService(settings)


def _providers() -> list[str]:
    providers = ["hashed"]
    try:  # the shipped default; skipped when the model is not cached
        from ai_memory_mcp.embedding import resolve_provider

        resolve_provider("model2vec")
        providers.append("model2vec")
    except Exception:
        pass
    return providers


@pytest.mark.parametrize("provider", _providers())
def test_paraphrase_match_is_answered(tmp_path: Path, provider: str) -> None:
    """A question answered in different words must not report no_answer."""
    service = _service(tmp_path, provider)
    packet = service.recall("how do I log in to view logs")
    assert packet.status == "answered", provider
    assert packet.citations[0].memory_id == "mem-console"


@pytest.mark.parametrize("provider", _providers())
def test_low_overlap_paraphrase_is_answered(
    tmp_path: Path, provider: str
) -> None:
    service = _service(tmp_path, provider)
    packet = service.recall(
        "steps to authenticate to the observability platform"
    )
    assert packet.status == "answered", provider
    assert packet.citations[0].memory_id == "mem-console"


@pytest.mark.parametrize("provider", _providers())
@pytest.mark.parametrize(
    "query",
    (
        "what is the launch date of Project Zephyr",
        "what is the production database password",
        "who approved the vendor contract in March",
    ),
)
def test_absent_knowledge_still_reports_no_answer(
    tmp_path: Path, provider: str, query: str
) -> None:
    """The gate must not claim an answer the corpus does not contain."""
    service = _service(tmp_path, provider)
    packet = service.recall(query)
    assert packet.status == "no_answer", f"{provider}: {query}"
