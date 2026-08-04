from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ai_memory_mcp.config import Settings
from ai_memory_mcp.index import build_index
from ai_memory_mcp.retrieval import RetrievalEngine


def _write_note(
    root: Path,
    relative: str,
    memory_id: str,
    title: str,
    text: str,
    updated: str,
    review_after: str = "",
) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"memory_id: {memory_id}",
        f"title: {title}",
        "status: active",
        f"updated: {updated}",
    ]
    if review_after:
        lines.append(f"review_after: {review_after}")
    lines.extend(("---", "", f"# {title}", "", text, ""))
    path.write_text("\n".join(lines), encoding="utf-8")


def _engine(tmp_path: Path) -> RetrievalEngine:
    settings = Settings(
        memory_root=tmp_path / "vault",
        state_dir=tmp_path / "state",
        graph_path=tmp_path / "graph.json",
        graphify_mcp_url="",
        embedding_provider="hashed",
    )
    build_index(settings, force=True)
    engine = RetrievalEngine(settings)
    engine.now = lambda: datetime(2026, 8, 3, tzinfo=timezone.utc)
    return engine


def test_newer_note_outranks_stale_twin(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_note(
        vault,
        "Tools/Log Console Login 2024.md",
        "mem-log-console-old",
        "Log Console Login 2024",
        "Log in to the log console with LDAP at logs.example.internal.",
        "2024-01-05",
    )
    _write_note(
        vault,
        "Tools/Log Console Login.md",
        "mem-log-console-new",
        "Log Console Login",
        "Log in to the log console with SSO at logs.example.internal.",
        "2026-07-20",
    )
    packet = _engine(tmp_path).search("how to log in to the log console")
    ordered = [hit.memory_id for hit in packet.results]
    assert ordered.index("mem-log-console-new") < ordered.index("mem-log-console-old")
    top = packet.results[0]
    assert top.signals["freshness"] > 0.02
    assert "recently updated" in top.reasons


def test_review_overdue_note_is_penalized_and_flagged(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    # Titles chosen so the deterministic tiebreak (reverse-alphabetical title)
    # would rank the overdue note FIRST if the penalty did not exist.
    _write_note(
        vault,
        "Tools/Zeta Runbook.md",
        "mem-runbook-overdue",
        "Zeta Runbook",
        "Rotate the deploy token in the vault console.",
        "2026-06-01",
        review_after="2026-07-01",
    )
    _write_note(
        vault,
        "Tools/Alpha Runbook.md",
        "mem-runbook-current",
        "Alpha Runbook",
        "Rotate the deploy token in the vault console.",
        "2026-06-01",
    )
    packet = _engine(tmp_path).search("rotate the deploy token")
    ordered = [hit.memory_id for hit in packet.results]
    assert ordered.index("mem-runbook-current") < ordered.index(
        "mem-runbook-overdue"
    )
    overdue = next(
        hit for hit in packet.results if hit.memory_id == "mem-runbook-overdue"
    )
    assert "review overdue" in overdue.reasons


def test_unparseable_dates_are_ignored(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_note(
        vault,
        "Tools/No Date.md",
        "mem-no-date",
        "No Date Note",
        "Restart the collector service after config changes.",
        "sometime last spring",
    )
    packet = _engine(tmp_path).search("restart the collector service")
    top = packet.results[0]
    assert top.memory_id == "mem-no-date"
    assert "freshness" not in top.signals
    assert "review overdue" not in top.reasons
