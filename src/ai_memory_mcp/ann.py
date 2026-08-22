from __future__ import annotations

from functools import lru_cache
from typing import Any

ANN_BACKEND = "random-projection-lsh-v1"
ANN_BANDS = 8
ANN_BITS_PER_BAND = 8


def _numpy() -> Any | None:
    try:
        import numpy
    except ImportError:
        return None
    return numpy


def available() -> bool:
    return _numpy() is not None


@lru_cache(maxsize=8)
def _projection_matrix(dimensions: int) -> Any:
    numpy = _numpy()
    if numpy is None:
        raise RuntimeError("The ANN backend requires NumPy.")
    # This fixed seed is part of the index format. It gives every installation
    # the same buckets without storing a machine-specific projection matrix.
    random = numpy.random.default_rng(0xA11CE5)
    return random.choice(
        numpy.asarray((-1.0, 1.0), dtype=numpy.float32),
        size=(ANN_BANDS * ANN_BITS_PER_BAND, dimensions),
    )


def vector_buckets(
    vector: dict[int, float],
    dimensions: int,
) -> tuple[tuple[int, int], ...]:
    numpy = _numpy()
    if numpy is None or dimensions <= 0 or not vector:
        return ()
    dense = numpy.zeros(dimensions, dtype=numpy.float32)
    for index, value in vector.items():
        if 0 <= index < dimensions:
            dense[index] = value
    projections = _projection_matrix(dimensions) @ dense
    buckets: list[tuple[int, int]] = []
    for band in range(ANN_BANDS):
        value = 0
        offset = band * ANN_BITS_PER_BAND
        for bit in range(ANN_BITS_PER_BAND):
            if projections[offset + bit] >= 0:
                value |= 1 << bit
        buckets.append((band, value))
    return tuple(buckets)


def bucket_clause(
    buckets: tuple[tuple[int, int], ...],
    *,
    table_alias: str,
) -> tuple[str, list[int]]:
    clause = " OR ".join(
        f"({table_alias}.band = ? AND {table_alias}.bucket = ?)"
        for _ in buckets
    )
    parameters = [value for pair in buckets for value in pair]
    return clause, parameters


def multiprobe_buckets(
    buckets: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    """Include each exact bucket and its one-bit and two-bit neighbors."""
    probes: list[tuple[int, int]] = []
    for band, bucket in buckets:
        probes.append((band, bucket))
        probes.extend(
            (band, bucket ^ (1 << bit))
            for bit in range(ANN_BITS_PER_BAND)
        )
        probes.extend(
            (band, bucket ^ (1 << first) ^ (1 << second))
            for first in range(ANN_BITS_PER_BAND)
            for second in range(first + 1, ANN_BITS_PER_BAND)
        )
    return tuple(probes)
