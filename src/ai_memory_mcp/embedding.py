from __future__ import annotations

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
    if normalized == "auto":
        return HashedProvider(dimensions)
    raise EmbeddingUnavailable(f"Unknown embedding provider: {name}")
