from __future__ import annotations

import pytest

pytest.importorskip("model2vec")

from ai_memory_mcp.embedding import resolve_provider  # noqa: E402


def _provider():
    try:
        return resolve_provider("model2vec")
    except Exception as exc:  # model not cached and no network
        pytest.skip(f"model2vec model unavailable: {exc}")


def _cosine(left: dict[int, float], right: dict[int, float]) -> float:
    return sum(value * right.get(key, 0.0) for key, value in left.items())


def test_model2vec_reports_probed_dimensions() -> None:
    provider = _provider()
    assert provider.name == "model2vec"
    assert provider.model == "minishlab/potion-base-8M"
    assert provider.dimensions == 256


def test_model2vec_connects_paraphrase() -> None:
    provider = _provider()
    query = provider.embed("how do I log in to the log console")
    target = provider.embed("authentication steps for the log console server")
    distractor = provider.embed("quarterly marketing budget review meeting")
    assert _cosine(query, target) > _cosine(query, distractor)


def test_auto_prefers_model2vec_when_available() -> None:
    _provider()
    assert resolve_provider("auto").name == "model2vec"
