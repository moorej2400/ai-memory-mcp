from __future__ import annotations

from pathlib import Path

from ai_memory_mcp.config import Settings
from ai_memory_mcp.embedding import HashedProvider, resolve_provider
from ai_memory_mcp.index import build_index
from ai_memory_mcp.retrieval import RetrievalEngine
from ai_memory_mcp.text import semantic_vector


def _write_note(
    root: Path, relative: str, memory_id: str, title: str, text: str
) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            (
                "---",
                f"memory_id: {memory_id}",
                f"title: {title}",
                "status: active",
                "updated: 2026-07-01",
                "---",
                "",
                f"# {title}",
                "",
                text,
                "",
            )
        ),
        encoding="utf-8",
    )


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    return Settings(
        memory_root=vault,
        state_dir=tmp_path / "state",
        graph_path=tmp_path / "graph.json",
        graphify_mcp_url="",
        embedding_provider="hashed",
        **overrides,
    )


def test_hashed_provider_matches_legacy_semantic_vector() -> None:
    provider = resolve_provider("hashed", dimensions=256)
    assert isinstance(provider, HashedProvider)
    assert provider.name == "hashed"
    assert provider.dimensions == 256
    text = "restart the proxy without a terminal window"
    assert provider.embed(text) == semantic_vector(text, 256)


def test_index_records_embedding_fingerprint(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_note(
        tmp_path / "vault",
        "Tools/Proxy.md",
        "mem-proxy",
        "Proxy Restart",
        "Restart the proxy with the launch script.",
    )
    build_index(settings, force=True)
    engine = RetrievalEngine(settings)
    metadata = engine.index.metadata()
    assert metadata["embedding_provider"] == "hashed"
    assert metadata["embedding_fingerprint"] == "hashed::1024"
    assert engine.provider is not None
    assert engine.provider.name == "hashed"
    assert engine.provider_warning == ""


def test_provider_change_triggers_full_reembed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_note(
        tmp_path / "vault",
        "Tools/Proxy.md",
        "mem-proxy",
        "Proxy Restart",
        "Restart the proxy with the launch script.",
    )
    first = build_index(settings, force=True)
    assert first["added"] == 1
    second = build_index(settings, force=False)
    assert second["unchanged"] == 1
    resized = _settings(tmp_path, semantic_dimensions=128)
    third = build_index(resized, force=False)
    assert third["added"] == 1, "fingerprint change must re-embed every document"
    engine = RetrievalEngine(resized)
    assert engine.index.metadata()["embedding_fingerprint"] == "hashed::128"
