from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .audit import append_event, file_lock
from .config import Settings

GENERATION_SCHEMA = "ai-memory/generation@1"
GENERATION_POINTER_SCHEMA = "ai-memory/generation-pointer@1"
_PROCESS_LEASE_LOCK = threading.RLock()
_PROCESS_LEASES: dict[str, tuple[Path, int]] = {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _state_child(settings: Settings, name: object) -> Path | None:
    if not isinstance(name, str) or not name or Path(name).name != name:
        return None
    candidate = settings.state_dir / name
    try:
        if candidate.resolve().parent != settings.state_dir.resolve():
            return None
    except OSError:
        return None
    return candidate


def load_current_generation(settings: Settings) -> dict[str, Any] | None:
    pointer = _read_json(settings.generation_pointer_path)
    if not pointer or pointer.get("schema") != GENERATION_POINTER_SCHEMA:
        return None
    manifest_path = _state_child(settings, pointer.get("manifest"))
    if manifest_path is None or not manifest_path.is_file():
        return None
    manifest = _read_json(manifest_path)
    if not manifest or manifest.get("schema") != GENERATION_SCHEMA:
        return None
    if manifest.get("generation_id") != pointer.get("generation_id"):
        return None
    return {**manifest, "manifest_path": str(manifest_path)}


def generation_component_path(
    settings: Settings,
    component: str,
) -> Path | None:
    generation = load_current_generation(settings)
    if generation is None:
        return None
    candidate = _state_child(settings, generation.get(component))
    return candidate if candidate is not None and candidate.is_file() else None


def manifest_component_path(
    settings: Settings,
    generation: dict[str, Any],
    component: str,
) -> Path | None:
    """Resolve one component from an already pinned generation manifest."""
    candidate = _state_child(settings, generation.get(component))
    return candidate if candidate is not None and candidate.is_file() else None


def current_graph_path(settings: Settings) -> Path:
    return generation_component_path(settings, "graph_snapshot") or settings.graph_path


def generation_health(settings: Settings) -> dict[str, Any]:
    return _read_json(settings.generation_health_path) or {}


def _publish_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(
        f".{path.name}.partial-{os.getpid()}-{time.time_ns()}"
    )
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        temporary.chmod(0o600)
    os.replace(temporary, path)


def _publish_json_no_overwrite(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(
        f".{path.name}.partial-{os.getpid()}-{time.time_ns()}"
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            temporary.chmod(0o600)
        _publish_file_no_overwrite(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _active_generation_leases(settings: Settings) -> set[str]:
    active: set[str] = set()
    now = time.time()
    for path in settings.state_dir.glob(".generation-lease-*.json"):
        payload = _read_json(path)
        generation_id = payload.get("generation_id") if payload else None
        pid = payload.get("pid") if payload else None
        try:
            age_seconds = max(0.0, now - path.stat().st_mtime)
        except OSError:
            continue
        live_process = isinstance(pid, int) and _pid_is_running(pid)
        if (
            isinstance(generation_id, str)
            and generation_id
            and (live_process or age_seconds <= settings.generation_lease_ttl_seconds)
        ):
            active.add(generation_id)
            continue
        # A crashed process can leave a lease. A bounded stale lease must not
        # retain derived snapshots forever.
        try:
            path.unlink()
        except OSError:
            pass
    return active


@contextmanager
def lease_current_generation(
    settings: Settings,
) -> Iterator[dict[str, Any] | None]:
    """Pin a manifest before retention can retire its derived components."""
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    lease_path: Path | None = None
    leased_generation_id: str | None = None
    generation: dict[str, Any] | None = None
    with _PROCESS_LEASE_LOCK:
        generation = load_current_generation(settings)
        if generation is not None:
            generation_id = str(generation["generation_id"])
            existing = _PROCESS_LEASES.get(generation_id)
            if existing is not None:
                lease_path, count = existing
                _PROCESS_LEASES[generation_id] = (lease_path, count + 1)
                leased_generation_id = generation_id
            else:
                # Only the first in-process reader needs the cross-process lock.
                # Later concurrent recalls share its durable lease file.
                with file_lock(
                    settings.state_dir / "generation-retention.lock",
                    settings.index_lock_timeout_seconds,
                ):
                    generation = load_current_generation(settings)
                    if generation is None:
                        lease_path = None
                    else:
                        generation_id = str(generation["generation_id"])
                        leased_generation_id = generation_id
                        existing = _PROCESS_LEASES.get(generation_id)
                        if existing is not None:
                            lease_path, count = existing
                            _PROCESS_LEASES[generation_id] = (
                                lease_path,
                                count + 1,
                            )
                        else:
                            lease_path = settings.state_dir / (
                                f".generation-lease-{generation_id}-{os.getpid()}-"
                                f"{uuid.uuid4().hex}.json"
                            )
                            with lease_path.open(
                                "x",
                                encoding="utf-8",
                                newline="\n",
                            ) as stream:
                                json.dump(
                                    {
                                        "generation_id": generation_id,
                                        "pid": os.getpid(),
                                        "created_at": _utc_now(),
                                    },
                                    stream,
                                    sort_keys=True,
                                )
                                stream.write("\n")
                                stream.flush()
                            _PROCESS_LEASES[generation_id] = (lease_path, 1)
    try:
        yield generation
    finally:
        if lease_path is not None and leased_generation_id is not None:
            with _PROCESS_LEASE_LOCK:
                existing = _PROCESS_LEASES.get(leased_generation_id)
                if existing is not None and existing[1] > 1:
                    _, count = existing
                    _PROCESS_LEASES[leased_generation_id] = (
                        lease_path,
                        count - 1,
                    )
                elif existing is not None:
                    _PROCESS_LEASES.pop(leased_generation_id, None)
                    try:
                        lease_path.unlink()
                    except FileNotFoundError:
                        pass


def _sqlite_metrics(path: Path, count_sql: str) -> dict[str, Any]:
    with sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro",
        uri=True,
    ) as connection:
        integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if integrity != "ok":
            raise RuntimeError(f"Derived SQLite integrity check failed: {path.name}")
        corpus_size = int(connection.execute(count_sql).fetchone()[0])
    return {
        "path": path.name,
        "bytes": path.stat().st_size,
        "corpus_size": corpus_size,
    }


def _graph_metrics(path: Path, markdown_snapshot: str) -> dict[str, Any]:
    payload = _read_json(path)
    if payload is None:
        raise RuntimeError("The generated graph is not valid JSON.")
    metadata = payload.get("graph")
    if not isinstance(metadata, dict) or metadata.get("index_snapshot") != markdown_snapshot:
        raise RuntimeError("The graph does not identify the Markdown snapshot.")
    nodes = payload.get("nodes")
    links = payload.get("links", payload.get("edges"))
    if not isinstance(nodes, list) or not isinstance(links, list):
        raise RuntimeError("The generated graph has an invalid structure.")
    return {
        "path": path.name,
        "bytes": path.stat().st_size,
        "sha256": _file_sha256(path),
        "corpus_size": len(nodes),
        "edges": len(links),
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _artifact_counter(settings: Settings) -> int:
    from .artifacts.schema import connect_artifact_db

    with connect_artifact_db(settings.artifact_db, read_only=True) as connection:
        row = connection.execute(
            "SELECT value FROM artifact_metadata WHERE key = 'change_counter'"
        ).fetchone()
    if row is None:
        raise RuntimeError("The artifact database has no change counter.")
    return int(row[0])


def _publish_file_no_overwrite(source: Path, destination: Path) -> None:
    # A unique generation name must never replace a prior recovery point.
    os.link(source, destination)
    source.unlink()


def _record_failure(
    settings: Settings,
    health: dict[str, Any],
    *,
    layer: str,
    started_at: str,
    exc: BaseException,
) -> None:
    failure = {
        "at": _utc_now(),
        "started_at": started_at,
        "layer": layer,
        "error_type": type(exc).__name__,
        "error_sha256": hashlib.sha256(str(exc).encode("utf-8")).hexdigest(),
    }
    layers = dict(health.get("layers", {}))
    layer_health = dict(layers.get(layer, {}))
    layer_health["last_failure"] = failure
    layers[layer] = layer_health
    _publish_json(
        settings.generation_health_path,
        {**health, "last_failure": failure, "layers": layers},
    )
    append_event(
        settings,
        "generation",
        "generation_failed",
        failure,
    )


def _valid_generation_components(
    settings: Settings,
    manifest: dict[str, Any],
) -> bool:
    markdown = _state_child(settings, manifest.get("markdown_snapshot"))
    artifact = _state_child(settings, manifest.get("artifact_snapshot"))
    graph = _state_child(settings, manifest.get("graph_snapshot"))
    if not markdown or not artifact or not graph:
        return False
    if not markdown.is_file() or not artifact.is_file() or not graph.is_file():
        return False
    try:
        _sqlite_metrics(markdown, "SELECT count(*) FROM chunks")
        _sqlite_metrics(artifact, "SELECT count(*) FROM bursts")
        graph_metrics = _graph_metrics(graph, markdown.name)
        layers = manifest.get("layers")
        expected_graph = (
            layers.get("graphify") if isinstance(layers, dict) else None
        )
        if not isinstance(expected_graph, dict):
            return False
        with sqlite3.connect(
            f"file:{artifact.resolve().as_posix()}?mode=ro",
            uri=True,
        ) as connection:
            row = connection.execute(
                "SELECT value FROM metadata "
                "WHERE key = 'artifact_change_counter'"
            ).fetchone()
        return bool(
            graph_metrics["path"] == graph.name
            and graph_metrics["bytes"] == int(expected_graph.get("bytes", -1))
            and graph_metrics["corpus_size"]
            == int(expected_graph.get("corpus_size", -1))
            and graph_metrics["edges"] == int(expected_graph.get("edges", -1))
            and graph_metrics["sha256"] == expected_graph.get("sha256")
            and row is not None
            and int(row[0]) == int(manifest["artifact_change_counter"])
        )
    except (KeyError, OSError, RuntimeError, sqlite3.DatabaseError, ValueError):
        return False


def _retire_old_generations(
    settings: Settings,
    *,
    legacy_keep: set[Path],
    legacy_candidates: set[Path] | None = None,
) -> dict[str, int]:
    current = load_current_generation(settings)
    if current is None:
        raise RuntimeError("Retention requires a valid current generation.")
    manifests: list[tuple[str, Path, dict[str, Any]]] = []
    for path in settings.state_dir.glob("generation-*.json"):
        payload = _read_json(path)
        if payload and payload.get("schema") == GENERATION_SCHEMA:
            manifests.append((str(payload.get("published_at") or ""), path, payload))
    manifests.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    current_id = str(current["generation_id"])
    current_entry = next(
        (
            item
            for item in manifests
            if str(item[2].get("generation_id")) == current_id
        ),
        None,
    )
    if current_entry is None or not _valid_generation_components(
        settings,
        current_entry[2],
    ):
        raise RuntimeError("The active generation failed retention validation.")
    retained = [current_entry]
    for item in manifests:
        if item[1] == current_entry[1]:
            continue
        if _valid_generation_components(settings, item[2]):
            retained.append(item)
        if len(retained) >= settings.generation_retention_count:
            break
    last_good_available = len(retained) >= min(
        settings.generation_retention_count,
        2,
    )
    retained_paths = {path.resolve() for _, path, _ in retained}
    leased_generation_ids = _active_generation_leases(settings)
    keep = {path.resolve() for path in legacy_keep if path.is_file()}
    for _, manifest_path, manifest in manifests:
        if (
            manifest_path.resolve() not in retained_paths
            and str(manifest.get("generation_id")) not in leased_generation_ids
        ):
            continue
        keep.add(manifest_path.resolve())
        for key in ("markdown_snapshot", "artifact_snapshot", "graph_snapshot"):
            component = _state_child(settings, manifest.get(key))
            if component is not None and component.is_file():
                keep.add(component.resolve())

    removed = 0
    removed_bytes = 0
    candidates: list[Path] = []
    for _, manifest_path, manifest in manifests:
        candidates.append(manifest_path)
        for key in ("markdown_snapshot", "artifact_snapshot", "graph_snapshot"):
            component = _state_child(settings, manifest.get(key))
            if component is not None:
                candidates.append(component)
    candidates.extend(legacy_candidates or set())
    for path in sorted(set(candidates), key=lambda item: item.name):
        if not path.is_file() or path.resolve() in keep:
            continue
        removed_bytes += path.stat().st_size
        # These files contain derived data only. Canonical Markdown, artifacts,
        # event revisions, and object bytes use different paths and are never pruned.
        path.unlink()
        removed += 1
    return {
        "removed_files": removed,
        "removed_bytes": removed_bytes,
        "verified_generations": len(retained),
        "last_good_available": last_good_available,
    }


def _cleanup_failed_generation(
    owned_paths: set[Path],
) -> None:
    for path in sorted(owned_paths, key=lambda item: len(item.parts), reverse=True):
        try:
            if path.is_dir():
                for child in path.iterdir():
                    if child.is_file():
                        child.unlink()
                path.rmdir()
            elif path.is_file():
                path.unlink()
        except OSError:
            pass


def refresh_generation(settings: Settings) -> dict[str, Any]:
    """Build and atomically publish one consistent retrieval generation."""
    from .artifacts.vector_index import (
        acknowledge_artifact_vector_changes,
        build_artifact_vector_index,
    )
    from .index import build_index, current_index_path
    from .provider_graph import build_provider_graph

    settings.state_dir.mkdir(parents=True, exist_ok=True)
    started_at = _utc_now()
    started = time.perf_counter()
    health = generation_health(settings)
    layer = "markdown"
    published = False
    owned_paths: set[Path] = set()
    with file_lock(
        settings.state_dir / "generation.lock",
        settings.index_lock_timeout_seconds,
    ) as lock_wait_ms:
        from .artifacts.vector_index import current_artifact_index_path

        # Capture rollback state only after this refresh owns the publication lock.
        # Otherwise a concurrent refresh can publish between the read and this lock.
        pointer_before = _read_json(settings.generation_pointer_path)

        legacy_candidates = {
            path
            for pattern in (
                "index-*.sqlite",
                "artifact-index-*.sqlite",
                "graph-*.json",
            )
            for path in settings.state_dir.glob(pattern)
            if path.is_file()
        }
        legacy_keep = {
            path
            for path in (
                current_index_path(settings),
                current_artifact_index_path(settings),
            )
            if path is not None
        }
        try:
            markdown_started = time.perf_counter()
            markdown = build_index(
                settings,
                force=False,
                publish_pointer=False,
            )
            if markdown.get("parse_errors"):
                raise RuntimeError("Markdown indexing reported parse errors.")
            markdown_path = Path(str(markdown["snapshot"]))
            if markdown_path != current_index_path(settings):
                owned_paths.add(markdown_path)
            markdown_metrics = _sqlite_metrics(
                markdown_path,
                "SELECT count(*) FROM chunks",
            )
            markdown_metrics["latency_ms"] = round(
                (time.perf_counter() - markdown_started) * 1000,
                3,
            )

            layer = "artifact-vector"
            if not settings.artifact_db.is_file():
                raise FileNotFoundError("Artifact database is not available.")
            artifact_started = time.perf_counter()
            artifact = build_artifact_vector_index(
                settings,
                force=False,
                publish_pointer=False,
            )
            artifact_path = Path(artifact.snapshot)
            if artifact_path != current_artifact_index_path(settings):
                owned_paths.add(artifact_path)
            artifact_metrics = _sqlite_metrics(
                artifact_path,
                "SELECT count(*) FROM bursts",
            )
            artifact_metrics.update(
                {
                    "latency_ms": round(
                        (time.perf_counter() - artifact_started) * 1000,
                        3,
                    ),
                    "change_counter": artifact.change_counter,
                    "embedded_bursts": artifact.embedded_bursts,
                    "embedded_updates": artifact.embedded_updates,
                    "reused_bursts": artifact.reused_bursts,
                    "removed_bursts": artifact.removed_bursts,
                    "ann_backend": artifact.ann_backend,
                }
            )

            layer = "graphify"
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            generation_id = f"{stamp}-{os.getpid()}"
            temporary_dir = settings.state_dir / f".generation-{generation_id}.partial"
            temporary_dir.mkdir()
            owned_paths.add(temporary_dir)
            graph_started = time.perf_counter()
            build_provider_graph(
                settings,
                temporary_dir,
                index_path=markdown_path,
            )
            graph_path = settings.state_dir / f"graph-{generation_id}.json"
            _publish_file_no_overwrite(temporary_dir / "graph.json", graph_path)
            owned_paths.add(graph_path)
            manifest_sidecar = temporary_dir / "manifest.json"
            if manifest_sidecar.exists():
                manifest_sidecar.unlink()
            temporary_dir.rmdir()
            graph_metrics = _graph_metrics(graph_path, markdown_path.name)
            graph_metrics["latency_ms"] = round(
                (time.perf_counter() - graph_started) * 1000,
                3,
            )

            layer = "publication"
            if _artifact_counter(settings) != artifact.change_counter:
                raise RuntimeError(
                    "The artifact database changed during generation publication."
                )
            manifest = {
                "schema": GENERATION_SCHEMA,
                "generation_id": generation_id,
                "published_at": _utc_now(),
                "markdown_snapshot": markdown_path.name,
                "artifact_snapshot": artifact_path.name,
                "artifact_change_counter": artifact.change_counter,
                "graph_snapshot": graph_path.name,
                "layers": {
                    "markdown": markdown_metrics,
                    "artifact_vector": artifact_metrics,
                    "graphify": graph_metrics,
                },
            }
            manifest_path = settings.state_dir / f"generation-{generation_id}.json"
            owned_paths.add(manifest_path)
            with file_lock(
                settings.state_dir / "generation-retention.lock",
                settings.index_lock_timeout_seconds,
            ):
                _publish_json_no_overwrite(manifest_path, manifest)
                _publish_json(
                    settings.generation_pointer_path,
                    {
                        "schema": GENERATION_POINTER_SCHEMA,
                        "generation_id": generation_id,
                        "manifest": manifest_path.name,
                        "published_at": manifest["published_at"],
                    },
                )
                published = True
                acknowledge_artifact_vector_changes(
                    settings,
                    artifact.change_counter,
                )
                retention = _retire_old_generations(
                    settings,
                    legacy_keep=legacy_keep,
                    legacy_candidates=legacy_candidates,
                )
            completed = {
                "generation_id": generation_id,
                "at": manifest["published_at"],
                "elapsed_ms": round(
                    (time.perf_counter() - started) * 1000,
                    3,
                ),
                "lock_wait_ms": lock_wait_ms,
                "layers": manifest["layers"],
                "retention": retention,
                "storage_bytes": sum(
                    path.stat().st_size
                    for path in settings.state_dir.iterdir()
                    if path.is_file()
                ),
            }
            previous_storage = int(
                health.get("last_success", {}).get("storage_bytes", 0)
            )
            completed["storage_growth_bytes"] = (
                int(completed["storage_bytes"]) - previous_storage
            )
            prior_layer_health = dict(health.get("layers", {}))
            next_layer_health: dict[str, Any] = {}
            for layer_name, metrics in manifest["layers"].items():
                previous_layer = dict(prior_layer_health.get(layer_name, {}))
                previous_success = dict(previous_layer.get("last_success", {}))
                layer_success = {
                    "at": manifest["published_at"],
                    **metrics,
                    "storage_growth_bytes": int(metrics.get("bytes", 0))
                    - int(previous_success.get("bytes", 0)),
                }
                next_layer_health[layer_name] = {
                    **previous_layer,
                    "last_success": layer_success,
                }
            _publish_json(
                settings.generation_health_path,
                {
                    **health,
                    "last_success": completed,
                    "layers": next_layer_health,
                },
            )
            append_event(
                settings,
                "generation",
                "generation_completed",
                completed,
            )
            return {
                **manifest,
                "retention": retention,
                "_index_result": markdown,
                "_artifact_result": artifact,
            }
        except BaseException as exc:
            if published:
                if pointer_before is None:
                    try:
                        settings.generation_pointer_path.unlink()
                    except FileNotFoundError:
                        pass
                else:
                    _publish_json(settings.generation_pointer_path, pointer_before)
                published = False
            _cleanup_failed_generation(owned_paths)
            _record_failure(
                settings,
                health,
                layer=layer,
                started_at=started_at,
                exc=exc,
            )
            raise
