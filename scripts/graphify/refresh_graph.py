#!/usr/bin/env python3
"""Refresh the AI Memory Graphify corpus and global graph on any platform.

Publication is staged, then swapped in, so a failure at any point can roll the
live corpus and global graph back to the previous known-good snapshot.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common import (  # noqa: E402
    ScriptError,
    WINDOWS,
    graphify_python,
    graphify_state_root,
    info,
    load_environment,
    mcp_url,
    memory_sources,
    repository_root,
    run_main,
)


@contextlib.contextmanager
def exclusive_lock(lock_path: Path):
    """Hold an advisory lock so two refreshes cannot publish at once.

    This replaces the Windows-only named mutex of the original script with the
    locking primitive each platform provides.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        if WINDOWS:
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ScriptError(
                    "An AI-Memory Graphify refresh is already running."
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ScriptError(
                    "An AI-Memory Graphify refresh is already running."
                ) from exc
        try:
            yield
        finally:
            if WINDOWS:
                import msvcrt

                handle.seek(0)
                with contextlib.suppress(OSError):
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


class Refresh:
    def __init__(self, run_id: str, seed_corpus_out: Path | None) -> None:
        self.root = repository_root()
        self.services = Path(__file__).resolve().parent
        self.run_id = run_id
        self.seed_corpus_out = seed_corpus_out

        state_root = graphify_state_root()
        self.stage_root = state_root / "staging" / "ai-memory" / run_id
        self.backup_root = state_root / "backups" / "ai-memory" / run_id
        self.failed_root = self.backup_root / "failed-publication"
        self.log_root = state_root / "logs" / "ai-memory-refresh"
        self.state_path = self.backup_root / "refresh-state.json"

        self.live_corpus_out = state_root / "corpora" / "ai-memory" / "graphify-out"
        self.live_global_graph = state_root / "global-graph.json"
        self.live_global_manifest = state_root / "global-manifest.json"

        self.corpus_backup = self.backup_root / "corpus-graphify-out"
        self.global_graph_backup = self.backup_root / "global-graph.json"
        self.global_manifest_backup = self.backup_root / "global-manifest.json"

        self.stage_corpus_out = self.stage_root / "corpus" / "graphify-out"
        self.stage_sources_root = self.stage_corpus_out / "sources"
        self.stage_global_root = self.stage_root / "global"

        self.tool_python = graphify_python(self.root)
        self.state: dict[str, object] = {}
        self.log_handle = None

    # -- helpers ---------------------------------------------------------

    def write_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self.state, indent=2), encoding="utf-8", newline="\n"
        )

    @staticmethod
    def move_recoverably(source: Path, destination: Path) -> None:
        if not source.exists():
            return
        if destination.exists():
            raise ScriptError(f"Recovery destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))

    def log(self, message: str) -> None:
        """Write one line to the console and the run transcript."""
        info(message)
        if self.log_handle is not None:
            self.log_handle.write(f"{message}\n")
            self.log_handle.flush()

    def _run(self, command: list[str], failure: str) -> None:
        """Run one step, mirroring its output to the console and transcript.

        The PowerShell implementation captured a transcript of every refresh,
        which is the only record of why a failed run rolled back, so the ported
        version has to tee the same output rather than just name a log file.
        """
        self.log(f"$ {' '.join(command)}")
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            stripped = line.rstrip("\n")
            print(stripped, flush=True)
            if self.log_handle is not None:
                # Flushed per line so a long step, such as extraction, can be
                # followed in the transcript while it is still running instead
                # of appearing only once the step finishes.
                self.log_handle.write(f"{stripped}\n")
                self.log_handle.flush()
        if process.wait() != 0:
            raise ScriptError(failure)

    def run_tool(self, arguments: list[str], failure: str) -> None:
        self._run([str(self.tool_python), *arguments], failure)

    def run_script(self, script: str, arguments: list[str], failure: str) -> None:
        self._run([sys.executable, str(self.services / script), *arguments], failure)

    # -- phases ----------------------------------------------------------

    def stage_seed(self, sources) -> None:
        corpus_seed = self.live_corpus_out
        if self.seed_corpus_out is not None:
            corpus_seed = self.seed_corpus_out.expanduser().resolve()
            for required in ("graph.json", "manifest.json"):
                if not (corpus_seed / required).is_file():
                    raise ScriptError(
                        f"Seed corpus is missing {required} at {corpus_seed}"
                    )

        seed_sources = corpus_seed / "sources"
        if seed_sources.is_dir():
            for source in sources:
                seed = seed_sources / source.source_id / "graphify-out"
                if seed.is_dir():
                    destination = (
                        self.stage_sources_root / source.source_id / "graphify-out"
                    )
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(seed, destination)
        elif len(sources) == 1 and corpus_seed.is_dir():
            # A one-source legacy corpus can seed the first named-source refresh.
            destination = (
                self.stage_sources_root / sources[0].source_id / "graphify-out"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(corpus_seed, destination)

    def extract_sources(self, sources) -> list[tuple[str, Path]]:
        graphs: list[tuple[str, Path]] = []
        for source in sources:
            source_root = self.stage_sources_root / source.source_id
            self.run_script(
                "extract_ai_memory.py",
                [
                    "--out-root",
                    str(source_root),
                    "--memory-root",
                    str(source.root),
                    "--source-id",
                    source.source_id,
                    "--skip-global",
                ],
                f"AI-Memory extraction failed for source '{source.source_id}'.",
            )
            graphs.append(
                (source.source_id, source_root / "graphify-out" / "graph.json")
            )
        return graphs

    def validate(self, arguments: list[str], failure: str) -> None:
        self.run_tool(
            [str(self.services / "validate-ai-memory-graph.py"), *arguments],
            failure,
        )

    def restore_last_known_good(self) -> None:
        self.failed_root.mkdir(parents=True, exist_ok=True)
        if self.global_graph_backup.exists():
            self.move_recoverably(
                self.live_global_graph, self.failed_root / "global-graph.failed.json"
            )
            self.move_recoverably(self.global_graph_backup, self.live_global_graph)
        if self.global_manifest_backup.exists():
            self.move_recoverably(
                self.live_global_manifest,
                self.failed_root / "global-manifest.failed.json",
            )
            self.move_recoverably(
                self.global_manifest_backup, self.live_global_manifest
            )
        if self.corpus_backup.exists():
            self.move_recoverably(
                self.live_corpus_out, self.failed_root / "corpus-graphify-out"
            )
            self.move_recoverably(self.corpus_backup, self.live_corpus_out)

        self.state["phase"] = "rolled-back"
        self.state["rolledBackAt"] = datetime.now(timezone.utc).isoformat()
        self.write_state()

    def run(self) -> None:
        for directory in (
            self.stage_sources_root,
            self.stage_global_root,
            self.backup_root,
            self.log_root,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        log_path = self.log_root / f"ai-memory-refresh-{self.run_id}.log"
        self.log_handle = log_path.open("a", encoding="utf-8", newline="\n")
        self.log(f"AI-Memory refresh {self.run_id} started.")

        # Checked after the transcript opens so a missing runtime is recorded
        # rather than failing silently before any log exists.
        if not self.tool_python.is_file():
            raise ScriptError(
                f"Pinned Graphify Python was not found at {self.tool_python}. "
                "Run scripts/setup.py."
            )

        prior_corpus_nodes = 0
        live_graph = self.live_corpus_out / "graph.json"
        if live_graph.is_file():
            payload = json.loads(live_graph.read_text(encoding="utf-8"))
            prior_corpus_nodes = len(payload.get("nodes", []))

        self.state = {
            "runId": self.run_id,
            "phase": "staging",
            "startedAt": datetime.now(timezone.utc).isoformat(),
            "priorCorpusNodes": prior_corpus_nodes,
            "liveCorpusOut": str(self.live_corpus_out),
            "liveGlobalGraph": str(self.live_global_graph),
            "liveGlobalManifest": str(self.live_global_manifest),
            "corpusBackup": str(self.corpus_backup),
            "globalGraphBackup": str(self.global_graph_backup),
            "globalManifestBackup": str(self.global_manifest_backup),
            "logPath": str(log_path),
        }
        self.write_state()

        sources = memory_sources()
        self.stage_seed(sources)
        source_graphs = self.extract_sources(sources)

        merge_arguments = [
            str(self.services / "merge-memory-source-graphs.py"),
            "--output-dir",
            str(self.stage_corpus_out),
        ]
        for source_id, graph in source_graphs:
            merge_arguments.extend(["--source", f"{source_id}={graph}"])
        self.run_tool(merge_arguments, "AI-Memory source graph merge failed.")

        self.validate(
            [
                "--corpus",
                str(self.stage_corpus_out / "graph.json"),
                "--manifest",
                str(self.stage_corpus_out / "manifest.json"),
                "--prior-corpus-nodes",
                str(prior_corpus_nodes),
            ],
            "Staged corpus validation failed.",
        )

        self.state["phase"] = "publishing-corpus"
        self.write_state()
        self.move_recoverably(self.live_corpus_out, self.corpus_backup)
        self.move_recoverably(self.stage_corpus_out, self.live_corpus_out)

        if self.live_global_graph.is_file():
            shutil.copy2(
                self.live_global_graph, self.stage_global_root / "global-graph.json"
            )
        if self.live_global_manifest.is_file():
            shutil.copy2(
                self.live_global_manifest,
                self.stage_global_root / "global-manifest.json",
            )

        self.run_tool(
            [
                str(self.services / "publish-ai-memory-global.py"),
                "--source",
                str(self.live_corpus_out / "graph.json"),
                "--stage-dir",
                str(self.stage_global_root),
            ],
            "Global graph staging failed.",
        )

        self.validate(
            [
                "--corpus",
                str(self.live_corpus_out / "graph.json"),
                "--manifest",
                str(self.live_corpus_out / "manifest.json"),
                "--global-graph",
                str(self.stage_global_root / "global-graph.json"),
                "--prior-corpus-nodes",
                str(prior_corpus_nodes),
            ],
            "Staged global graph validation failed.",
        )

        self.state["phase"] = "publishing-global"
        self.write_state()
        self.move_recoverably(self.live_global_graph, self.global_graph_backup)
        self.move_recoverably(self.live_global_manifest, self.global_manifest_backup)
        self.move_recoverably(
            self.stage_global_root / "global-graph.json", self.live_global_graph
        )
        self.move_recoverably(
            self.stage_global_root / "global-manifest.json", self.live_global_manifest
        )

        self.state["phase"] = "health-check"
        self.write_state()
        self.run_script(
            "start_global_mcp.py",
            [],
            "Graphify global MCP restart failed after publication.",
        )

        health_path = self.backup_root / "health-result.json"
        self.validate(
            [
                "--corpus",
                str(self.live_corpus_out / "graph.json"),
                "--manifest",
                str(self.live_corpus_out / "manifest.json"),
                "--global-graph",
                str(self.live_global_graph),
                "--prior-corpus-nodes",
                str(prior_corpus_nodes),
                "--mcp-url",
                mcp_url(),
                "--output",
                str(health_path),
            ],
            "Post-refresh health gate failed.",
        )

        self.run_tool(
            [str(self.services / "run-ai-memory-retrieval-eval.py")],
            "AI-Memory retrieval regression suite failed.",
        )

        self.state["phase"] = "complete"
        self.state["completedAt"] = datetime.now(timezone.utc).isoformat()
        self.state["healthPath"] = str(health_path)
        self.write_state()
        self.log(
            "AI-Memory graph refresh complete. "
            f"Recovery snapshot: {self.backup_root}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh the AI Memory Graphify corpus and global graph."
    )
    parser.add_argument("--seed-corpus-out", type=Path, default=None)
    args = parser.parse_args()

    load_environment(repository_root())
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    refresh = Refresh(run_id, args.seed_corpus_out)

    with exclusive_lock(graphify_state_root() / "ai-memory-refresh.lock"):
        try:
            refresh.run()
        except Exception as failure:
            # Logged before the state check so a failure that happens before
            # any state exists — a missing runtime, for example — still reaches
            # the transcript instead of leaving only a "started" line.
            refresh.log(f"FAILED: {failure}")
            if refresh.state:
                refresh.state["failure"] = str(failure)
                refresh.state["failedAt"] = datetime.now(timezone.utc).isoformat()
                refresh.write_state()
                refresh.restore_last_known_good()
                refresh.log("Rolled back to the last known good state.")
                try:
                    refresh.run_script(
                        "start_global_mcp.py",
                        [],
                        "MCP restart failed after rollback.",
                    )
                except ScriptError as restart_failure:
                    refresh.log(
                        "Rollback completed, but MCP restart failed: "
                        f"{restart_failure}"
                    )
            raise
        finally:
            if refresh.log_handle is not None:
                refresh.log_handle.close()
                refresh.log_handle = None


if __name__ == "__main__":
    run_main(main)
