from __future__ import annotations

from itertools import islice
from typing import Any

ANN_BACKEND = "int8-flat-v4"


def _numpy() -> Any | None:
    try:
        import numpy
    except ImportError:
        return None
    return numpy


def available() -> bool:
    return _numpy() is not None


def quantized_vector(vector: dict[int, float], dimensions: int) -> bytes:
    """Return one normalized int8 vector for bounded candidate selection."""
    numpy = _numpy()
    if numpy is None or dimensions <= 0 or not vector:
        return b""
    dense = numpy.zeros(dimensions, dtype=numpy.float32)
    for index, value in vector.items():
        if 0 <= index < dimensions:
            dense[index] = value
    norm = float(numpy.linalg.norm(dense))
    if norm == 0:
        return b""
    return numpy.rint(numpy.clip(dense / norm, -1.0, 1.0) * 127.0).astype(
        numpy.int8
    ).tobytes()


def quantized_shortlist(
    rows: Any,
    query_vector: bytes,
    dimensions: int,
    limit: int,
    *,
    block_size: int = 8192,
) -> list[tuple[float, str]]:
    """Select int8 cosine candidates with bounded working memory."""
    numpy = _numpy()
    if numpy is None or not query_vector or dimensions <= 0 or limit <= 0:
        return []
    query = numpy.frombuffer(query_vector, dtype=numpy.int8).astype(numpy.float32)
    iterator = iter(rows)
    best: list[tuple[float, str]] = []
    while True:
        block = list(islice(iterator, block_size))
        if not block:
            break
        identities = [str(row[0]) for row in block]
        payload = b"".join(bytes(row[1]) for row in block)
        if len(payload) != len(block) * dimensions:
            raise ValueError("Stored ANN vector has an invalid byte length.")
        values = numpy.frombuffer(payload, dtype=numpy.int8).reshape(
            len(block), dimensions
        )
        scores = values @ query
        keep = min(limit, len(block))
        if keep < len(block):
            selected = numpy.argpartition(scores, -keep)[-keep:]
        else:
            selected = range(len(block))
        best.extend((-float(scores[index]), identities[index]) for index in selected)
        if len(best) > limit:
            best = sorted(best, key=lambda item: (item[0], item[1]))[:limit]
    return sorted(best, key=lambda item: (item[0], item[1]))[:limit]


def candidate_recall_at_k(
    exact_ids: list[str] | tuple[str, ...],
    candidate_ids: list[str] | tuple[str, ...],
    k: int,
) -> float:
    """Measure approximate candidate retention against the exact neighbors."""
    if k <= 0:
        raise ValueError("The ANN recall rank must be positive.")
    expected = set(exact_ids[:k])
    if not expected:
        return 1.0
    return len(expected & set(candidate_ids)) / len(expected)


def tie_aware_candidate_recall_at_k(
    exact_items: list[tuple[str, float]] | tuple[tuple[str, float], ...],
    candidate_ids: list[str | tuple[str, float]] | tuple[str | tuple[str, float], ...],
    k: int,
    *,
    tolerance: float = 1e-5,
) -> float:
    """Measure required neighbors without assigning meaning to a boundary tie."""
    if k <= 0:
        raise ValueError("The ANN recall rank must be positive.")
    top = list(exact_items[:k])
    if not top:
        return 1.0
    boundary = top[-1][1]
    required = {
        identity for identity, score in top if score > boundary + tolerance
    }
    exact_ids = {identity for identity, _score in top}
    candidates = {
        item if isinstance(item, str) else item[0]: (
            None if isinstance(item, str) else item[1]
        )
        for item in candidate_ids
    }
    strict_matches = len(required & candidates.keys())
    # Equal-score substitutions need their exact rerank score as proof. Empty
    # candidate sets receive no credit even when every exact neighbor is tied.
    boundary_matches = sum(
        identity not in required
        and (
            score >= boundary - tolerance
            if score is not None
            else identity in exact_ids
        )
        for identity, score in candidates.items()
    )
    boundary_slots = len(exact_ids) - len(required)
    return (strict_matches + min(boundary_slots, boundary_matches)) / len(exact_ids)
