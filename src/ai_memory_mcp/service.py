from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .index import MemoryIndex, build_index, current_index_path
from .models import ScopeFilter
from .retrieval import RetrievalEngine


class MemoryService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        self.engine = RetrievalEngine(self.settings)

    def search(
        self,
        query: str,
        *,
        root_scope: str | None = None,
        repository: str | None = None,
        project: str | None = None,
        ticket: str | None = None,
        status: str = "active",
        path_prefix: str | None = None,
        limit: int | None = None,
        explain: bool = True,
    ) -> dict[str, Any]:
        scope = ScopeFilter(
            root_scope=root_scope,
            repository=repository,
            project=project,
            ticket=ticket,
            status=status,
            path_prefix=path_prefix,
        )
        return self.engine.search(
            query, scope=scope, limit=limit, explain=explain
        ).to_dict()

    def get(self, identity: str) -> dict[str, Any]:
        return self.engine.get(identity)

    def neighbors(self, identity: str, depth: int = 1) -> dict[str, Any]:
        return self.engine.neighbors(identity, depth)

    def path(self, source: str, target: str) -> dict[str, Any]:
        return self.engine.path(source, target)

    def explain(self, query: str, identity: str | None = None) -> dict[str, Any]:
        packet = self.search(query, limit=5, explain=True)
        if identity:
            packet["results"] = [
                result
                for result in packet["results"]
                if identity in (result["memory_id"], result["path"], result["title"])
            ]
        return packet

    def refresh(self, mode: str = "index", force: bool = False) -> dict[str, Any]:
        if mode not in {"index", "full"}:
            raise ValueError("mode must be 'index' or 'full'")
        graphify: dict[str, Any] | None = None
        if mode == "full":
            runtime = self._graphify_runtime()
            if not runtime["consistent"]:
                return {
                    "ok": False,
                    "graphify": {
                        "ok": False,
                        "failure": "graphify-runtime-preflight",
                        "runtime": runtime,
                    },
                    "index": None,
                }
            script = self.settings.refresh_script
            if not script or not script.exists():
                raise FileNotFoundError(f"Graphify refresh script not found: {script}")
            started = time.perf_counter()
            environment = dict(os.environ)
            scripts = Path(str(runtime["scripts_dir"]))
            environment["PATH"] = f"{scripts}{os.pathsep}{environment.get('PATH', '')}"
            environment["AI_MEMORY_GRAPHIFY_PYTHON"] = str(runtime["python"])
            environment["AI_MEMORY_GRAPHIFY_MCP_EXE"] = str(runtime["mcp_executable"])
            powershell = shutil.which("pwsh") or shutil.which("powershell")
            if not powershell:
                raise FileNotFoundError("PowerShell is required for Graphify refresh")
            run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            refresh_log = (
                self.settings.state_dir / f"graphify-refresh-{run_stamp}.log"
            )
            self.settings.state_dir.mkdir(parents=True, exist_ok=True)
            # The restarted MCP outlives PowerShell. A real file avoids the
            # inherited-pipe EOF wait that would otherwise strand this caller.
            with refresh_log.open("w", encoding="utf-8") as handle:
                process = subprocess.run(
                    [
                        powershell,
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(script),
                    ],
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=1800,
                    check=False,
                    env=environment,
                )
            output_tail = refresh_log.read_text(
                encoding="utf-8", errors="replace"
            )[-4000:]
            graphify = {
                "ok": process.returncode == 0,
                "exit_code": process.returncode,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                "log": str(refresh_log),
                "output_tail": output_tail,
            }
            if process.returncode != 0:
                return {"ok": False, "graphify": graphify, "index": None}
        indexed = build_index(self.settings, force=force)
        self.engine = RetrievalEngine(self.settings)
        return {"ok": True, "graphify": graphify, "index": indexed}

    def health(self) -> dict[str, Any]:
        index_path = current_index_path(self.settings)
        index_meta: dict[str, Any] = {"available": False}
        if index_path:
            index = MemoryIndex(self.settings)
            index_meta = {
                "available": True,
                "path": str(index.path),
                **index.metadata(),
            }
        graph_health = self.engine.graph.health()
        graph_age_seconds = (
            max(0.0, time.time() - self.settings.graph_path.stat().st_mtime)
            if self.settings.graph_path.exists()
            else None
        )
        mcp_version = importlib.metadata.version("mcp")
        return {
            "ok": bool(index_meta["available"]),
            "canonical_memory_root": {
                "path": str(self.settings.memory_root),
                "available": self.settings.memory_root.is_dir(),
                "authority": "canonical-markdown",
            },
            "index": index_meta,
            "graphify": {
                **graph_health,
                "age_seconds": graph_age_seconds,
                "provider_role": "internal-graph-signal",
                "runtime": self._graphify_runtime(),
            },
            "runtime": {
                "python": platform.python_version(),
                "mcp": mcp_version,
                "mcp_supported": mcp_version.startswith("1."),
            },
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    def _graphify_runtime(self) -> dict[str, Any]:
        project_root = Path(__file__).resolve().parents[2]
        scripts = project_root / ".graphify-runtime" / "Scripts"
        python = scripts / "python.exe"
        executable = scripts / "graphify.exe"
        mcp_executable = scripts / "graphify-mcp.exe"
        expected = "0.9.26"
        package_version: str | None = None
        cli_version: str | None = None
        errors: list[str] = []
        if python.exists():
            site_packages = scripts.parent / "Lib" / "site-packages"
            package_version = next(
                (
                    distribution.version
                    for distribution in importlib.metadata.distributions(
                        path=[str(site_packages)]
                    )
                    if distribution.metadata.get("Name", "").casefold()
                    == "graphifyy"
                ),
                None,
            )
            if package_version is None:
                errors.append("pinned Python cannot resolve graphifyy metadata")
        else:
            errors.append("pinned Python is missing")
        if executable.exists():
            # The console shim and distribution share this isolated venv, so
            # metadata gives the CLI version without spawning another process.
            cli_version = package_version
        else:
            errors.append("pinned Graphify CLI is missing")
        if not mcp_executable.exists():
            errors.append("pinned Graphify MCP executable is missing")
        skill_version_path = (
            Path.home() / ".codex" / "skills" / "graphify" / ".graphify_version"
        )
        skill_version = (
            skill_version_path.read_text(encoding="utf-8").strip()
            if skill_version_path.exists()
            else None
        )
        consistent = (
            package_version == expected
            and cli_version == expected
            and mcp_executable.exists()
            and not errors
        )
        return {
            "consistent": consistent,
            "expected": expected,
            "package": package_version,
            "cli": cli_version,
            "skill": skill_version,
            "skill_matches_runtime": skill_version == expected,
            "python": str(python),
            "mcp_executable": str(mcp_executable),
            "scripts_dir": str(scripts),
            "errors": errors,
        }

    def feedback(
        self,
        query: str,
        memory_id: str,
        relevance: str,
        note: str = "",
    ) -> dict[str, Any]:
        if relevance not in {"relevant", "irrelevant", "partial"}:
            raise ValueError("relevance must be relevant, irrelevant, or partial")
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        event = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "query": query,
            "memory_id": memory_id,
            "relevance": relevance,
            "note": note,
        }
        with self.settings.feedback_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return {"recorded": True, **event}
