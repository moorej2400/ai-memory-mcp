from __future__ import annotations

import math
from typing import Protocol

from .text import semantic_vector


class EmbeddingProvider(Protocol):
    """Query-time and index-time text vectors must come from one provider."""

    name: str
    model: str
    dimensions: int

    def embed(self, text: str) -> dict[int, float]: ...


class EmbeddingUnavailable(RuntimeError):
    pass


class HashedProvider:
    """Deterministic hashed features with no model dependency."""

    name = "hashed"
    model = ""

    def __init__(self, dimensions: int = 1024):
        self.dimensions = dimensions

    def embed(self, text: str) -> dict[int, float]:
        return semantic_vector(text, self.dimensions)


DEFAULT_MODEL2VEC_MODEL = "minishlab/potion-base-8M"


class Model2VecProvider:
    """Static local embeddings. Downloads once, then runs fully offline."""

    name = "model2vec"

    def __init__(self, model: str = ""):
        from model2vec import StaticModel  # deferred: optional dependency

        self.model = model or DEFAULT_MODEL2VEC_MODEL
        self._model = StaticModel.from_pretrained(self.model)
        self.dimensions = int(len(self._model.encode("dimension probe")))

    def embed(self, text: str) -> dict[int, float]:
        values = [float(value) for value in self._model.encode(text or " ")]
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return {
            index: value / norm
            for index, value in enumerate(values)
            if value
        }


def fingerprint(provider: EmbeddingProvider) -> str:
    return f"{provider.name}:{provider.model}:{provider.dimensions}"


def resolve_provider(
    name: str,
    *,
    model: str = "",
    dimensions: int = 1024,
) -> EmbeddingProvider:
    normalized = (name or "auto").strip().casefold()
    if normalized in {"hashed", ""}:
        return HashedProvider(dimensions)
    if normalized == "model2vec":
        try:
            return Model2VecProvider(model)
        except EmbeddingUnavailable:
            raise
        except Exception as exc:  # import, download, or model-load failure
            raise EmbeddingUnavailable(
                f"model2vec provider unavailable: {exc}"
            ) from exc
    if normalized == "auto":
        try:
            return Model2VecProvider(model)
        except Exception:
            return HashedProvider(dimensions)
    raise EmbeddingUnavailable(f"Unknown embedding provider: {name}")
