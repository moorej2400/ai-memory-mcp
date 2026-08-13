from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import uuid
from pathlib import Path

from ai_memory_mcp.config import Settings

from .models import ObjectVerification, StoredObject

SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
COPY_CHUNK_BYTES = 1024 * 1024


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)


def _private_file(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)


def _discard_generated_partial(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as stream:
        while chunk := stream.read(COPY_CHUNK_BYTES):
            digest.update(chunk)
            byte_count += len(chunk)
    return digest.hexdigest(), byte_count


def _object_relative_path(digest: str) -> Path:
    return Path("sha256") / digest[:2] / digest


def verify_object(settings: Settings, sha256: str) -> ObjectVerification:
    """Verify one content-addressed object without changing it."""
    if not SHA256_PATTERN.fullmatch(sha256):
        raise ValueError("The object hash must be a lowercase SHA-256 digest.")
    path = settings.artifact_objects_dir / _object_relative_path(sha256)
    if not path.is_file():
        return ObjectVerification(sha256=sha256, ok=False, byte_count=0)
    actual, byte_count = _hash_file(path)
    return ObjectVerification(
        sha256=sha256,
        ok=actual == sha256,
        byte_count=byte_count,
    )


def store_object(
    settings: Settings,
    source_path: Path,
    expected_sha256: str | None = None,
) -> StoredObject:
    """Copy one regular file into the private content-addressed object tree."""
    source = source_path.expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("The object source must be a regular file.")
    object_root = settings.artifact_objects_dir.expanduser().resolve()
    if source.is_relative_to(object_root):
        raise ValueError("The object source cannot be inside the object directory.")
    if expected_sha256 is not None and not SHA256_PATTERN.fullmatch(expected_sha256):
        raise ValueError("The expected object hash has an invalid format.")

    digest, byte_count = _hash_file(source)
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError("The object source hash does not match the expected hash.")
    relative = _object_relative_path(digest)
    destination = object_root / relative
    media_type = mimetypes.guess_type(source.name)[0] or ""

    if destination.exists():
        verification = verify_object(settings, digest)
        if not verification.ok:
            raise ValueError(
                "The existing object path contains data with a different hash."
            )
        return StoredObject(
            sha256=digest,
            byte_count=verification.byte_count,
            media_type=media_type,
            relative_path=relative.as_posix(),
        )

    _private_directory(object_root)
    _private_directory(destination.parent.parent)
    _private_directory(destination.parent)
    temporary = destination.parent / (
        f".{digest}.partial-{os.getpid()}-{uuid.uuid4().hex}"
    )
    try:
        copied_digest = hashlib.sha256()
        copied_bytes = 0
        with source.open("rb") as incoming, temporary.open("xb") as outgoing:
            while chunk := incoming.read(COPY_CHUNK_BYTES):
                outgoing.write(chunk)
                copied_digest.update(chunk)
                copied_bytes += len(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        _private_file(temporary)
        if copied_digest.hexdigest() != digest or copied_bytes != byte_count:
            raise ValueError("The staged object hash does not match the source hash.")

        # Only one writer can claim the digest path. Losers verify and reuse it.
        try:
            os.link(temporary, destination)
        except FileExistsError:
            verification = verify_object(settings, digest)
            if not verification.ok or verification.byte_count != byte_count:
                raise ValueError(
                    "The existing object path contains data with a different hash."
                )
        _private_file(destination)
    finally:
        _discard_generated_partial(temporary)
    _sync_directory(destination.parent)

    return StoredObject(
        sha256=digest,
        byte_count=byte_count,
        media_type=media_type,
        relative_path=relative.as_posix(),
    )
