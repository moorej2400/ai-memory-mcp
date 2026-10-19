from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path
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


@lru_cache(maxsize=4)
def _load_cached_model(model: str):
    from huggingface_hub import snapshot_download
    from model2vec import StaticModel

    # model2vec's cache-only path turns a repo ID into a Windows Path before
    # its cache lookup, so the backslash-separated ID misses the cached model.
    # Resolve the exact cached revision here and pass a local path to model2vec;
    # normal recall and sync must not wait for a network model check.
    local_path = Path(model)
    if not local_path.is_dir():
        local_path = Path(snapshot_download(repo_id=model, local_files_only=True))
    return StaticModel.from_pretrained(local_path)


class Model2VecProvider:
    """Static local embeddings from a pre-provisioned cached model."""

    name = "model2vec"

    def __init__(self, model: str = ""):
        self.model = model or DEFAULT_MODEL2VEC_MODEL
        self._model = _load_cached_model(self.model)
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
        except Exception as exc:  # missing cache, import, or model-load failure
            raise EmbeddingUnavailable(
                f"model2vec provider unavailable: {exc}"
            ) from exc
    if normalized == "auto":
        try:
            return Model2VecProvider(model)
        except Exception:
            return HashedProvider(dimensions)
    raise EmbeddingUnavailable(f"Unknown embedding provider: {name}")
