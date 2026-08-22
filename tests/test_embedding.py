from __future__ import annotations

import sys
import sqlite3
from pathlib import Path

import pytest

from ai_memory_mcp.config import Settings
from ai_memory_mcp.ann import ANN_BANDS, available as ann_available
from ai_memory_mcp.embedding import (
    EmbeddingUnavailable,
    HashedProvider,
    resolve_provider,
)
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


def test_markdown_index_embeds_only_changed_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    for index in range(2):
        _write_note(
            settings.memory_root,
            f"Tools/Record-{index}.md",
            f"mem-record-{index}",
            f"Record {index}",
            f"Stable procedure {index}.",
        )
    provider = HashedProvider(dimensions=settings.semantic_dimensions)
    original_embed = provider.embed
    calls: list[str] = []

    def counted_embed(text: str) -> dict[int, float]:
        calls.append(text)
        return original_embed(text)

    monkeypatch.setattr(provider, "embed", counted_embed)
    monkeypatch.setattr(
        "ai_memory_mcp.index.resolve_provider",
        lambda *_args, **_kwargs: provider,
    )
    first = build_index(settings, force=True)
    assert first["added"] == 2
    first_calls = len(calls)

    changed = settings.memory_root / "Tools" / "Record-1.md"
    changed.write_text(
        changed.read_text(encoding="utf-8") + "\nA corrected detail.\n",
        encoding="utf-8",
    )
    second = build_index(settings)

    assert second["changed"] == 1
    assert second["unchanged"] == 1
    assert len(calls) == first_calls + 1


@pytest.mark.skipif(not ann_available(), reason="NumPy ANN backend is unavailable")
def test_markdown_backend_transition_rebuilds_all_ann_buckets(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    for index in range(2):
        _write_note(
            settings.memory_root,
            f"Tools/Record-{index}.md",
            f"mem-record-{index}",
            f"Record {index}",
            f"Stable semantic procedure {index}.",
        )
    first = build_index(settings, force=True)
    with sqlite3.connect(first["snapshot"]) as connection:
        connection.execute(
            "UPDATE metadata SET value = 'exact' WHERE key = 'ann_backend'"
        )
        connection.execute("DELETE FROM chunk_ann_buckets")
        connection.commit()
    changed = settings.memory_root / "Tools" / "Record-1.md"
    changed.write_text(
        changed.read_text(encoding="utf-8") + "\nA changed detail.\n",
        encoding="utf-8",
    )

    second = build_index(settings)
    with sqlite3.connect(second["snapshot"]) as connection:
        chunks = int(connection.execute("SELECT count(*) FROM chunks").fetchone()[0])
        buckets = int(
            connection.execute("SELECT count(*) FROM chunk_ann_buckets").fetchone()[0]
        )
    assert buckets == chunks * ANN_BANDS


def test_auto_falls_back_to_hashed_without_model2vec(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "model2vec", None)
    provider = resolve_provider("auto", dimensions=64)
    assert provider.name == "hashed"
    assert provider.dimensions == 64


def test_explicit_model2vec_raises_when_missing(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "model2vec", None)
    with pytest.raises(EmbeddingUnavailable) as error:
        resolve_provider("model2vec")
    # The failure must name the unavailable provider, not report an unknown
    # name: the difference tells an operator to install versus to reconfigure.
    assert "model2vec provider unavailable" in str(error.value)


def test_auto_prefers_model2vec_when_loadable(monkeypatch) -> None:
    class _FakeModel2Vec:
        name = "model2vec"
        model = "fake-model"
        dimensions = 8

        def __init__(self, model: str = ""):
            pass

        def embed(self, text: str) -> dict[int, float]:
            return {0: 1.0}

    monkeypatch.setattr(
        "ai_memory_mcp.embedding.Model2VecProvider", _FakeModel2Vec
    )
    assert resolve_provider("auto", dimensions=64).name == "model2vec"
    assert resolve_provider("model2vec").name == "model2vec"


def test_engine_disables_semantic_when_provider_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    _write_note(
        tmp_path / "vault",
        "Tools/Proxy.md",
        "mem-proxy",
        "Proxy Restart",
        "Restart the proxy with the launch script.",
    )
    build_index(settings, force=True)

    def _raise(*args: object, **kwargs: object):
        raise EmbeddingUnavailable("provider gone")

    monkeypatch.setattr("ai_memory_mcp.retrieval.resolve_provider", _raise)
    engine = RetrievalEngine(settings)
    assert engine.provider is None
    assert "Semantic retrieval disabled" in engine.provider_warning
    packet = engine.search("proxy restart")
    assert packet.diagnostics["candidate_counts"]["semantic"] == 0


def test_large_vector_corpus_uses_ann_candidates_with_exact_reranking(
    tmp_path: Path,
) -> None:
    pytest.importorskip("numpy")
    settings = _settings(tmp_path)
    for index in range(2_050):
        text = f"Semantic operations procedure number {index} uses stable evidence."
        if index == 2_049:
            text += " The unique semantic target is heliotrope-cascade."
        _write_note(
            settings.memory_root,
            f"References/Record-{index}.md",
            f"mem-record-{index}",
            f"Record {index}",
            text,
        )
    build_index(settings, force=True)

    packet = RetrievalEngine(settings).search(
        "unique semantic target heliotrope-cascade",
        limit=3,
    )

    assert packet.results[0].memory_id == "mem-record-2049"
    assert (
        packet.diagnostics["semantic_search"]["backend"]
        == "random-projection-lsh-v1"
    )
    assert packet.diagnostics["semantic_search"]["candidates"] < 2_050
