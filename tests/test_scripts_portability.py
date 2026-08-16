"""Cover the standalone maintenance scripts that run before installation."""

from __future__ import annotations

import importlib.util
import os
import socket
import sys
from pathlib import Path

import pytest


def _load(name: str, relative: str, project_root: Path):
    """Import a script module directly, mirroring how the scripts import it."""
    path = project_root / relative
    scripts_root = str(project_root / "scripts")
    graphify_root = str(project_root / "scripts" / "graphify")
    for entry in (scripts_root, graphify_root):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def common(project_root: Path):
    return _load("_common", "scripts/_common.py", project_root)


@pytest.fixture
def processes(project_root: Path):
    return _load("_processes", "scripts/graphify/_processes.py", project_root)


def test_every_entry_point_has_a_wrapper_for_each_shell(
    project_root: Path,
) -> None:
    """Each ported script must stay reachable from PowerShell and POSIX shells."""
    expected = {
        "scripts/setup": "setup",
        "scripts/install-clients": "install_clients",
        "scripts/install-codex": "install_codex",
        "scripts/graphify/extract-ai-memory": "extract_ai_memory",
        "scripts/graphify/refresh-ai-memory-graph": "refresh_graph",
        "scripts/graphify/start-graphify-global-mcp": "start_global_mcp",
        "scripts/graphify/stop-graphify-global-mcp": "stop_global_mcp",
        "scripts/graphify/install-graphify-global-mcp-startup": "install_autostart",
    }
    for stem, implementation in expected.items():
        directory = (project_root / stem).parent
        assert (project_root / f"{stem}.ps1").is_file(), stem
        assert (project_root / f"{stem}.sh").is_file(), stem
        assert (directory / f"{implementation}.py").is_file(), implementation


def test_scripts_avoid_hardcoded_windows_environment_layout(
    project_root: Path,
) -> None:
    """The ported implementations must not reintroduce Windows-only paths."""
    forbidden = ("Scripts/python.exe", "Scripts\\python.exe", "Lib/site-packages")
    for path in sorted((project_root / "scripts").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert not any(token in text for token in forbidden), path


def test_memory_sources_resolve_federated_configuration(
    common, monkeypatch, tmp_path: Path
) -> None:
    core = tmp_path / "core"
    personal = tmp_path / "personal"
    core.mkdir()
    personal.mkdir()

    monkeypatch.setenv("AI_MEMORY_WORK_DIR", str(core))
    monkeypatch.setenv("AI_MEMORY_PRIMARY_SOURCE_ID", "core")
    monkeypatch.setenv(
        "AI_MEMORY_RETRIEVAL_SOURCES", f'{{"personal": "{personal.as_posix()}"}}'
    )
    monkeypatch.delenv("AI_MEMORY_PERSONAL_DIR", raising=False)

    sources = common.memory_sources()

    assert [source.source_id for source in sources] == ["core", "personal"]
    assert [source.writable for source in sources] == [True, False]
    assert sources[0].root == core.resolve()


def test_memory_sources_reject_a_duplicate_directory(
    common, monkeypatch, tmp_path: Path
) -> None:
    core = tmp_path / "core"
    core.mkdir()
    monkeypatch.setenv("AI_MEMORY_WORK_DIR", str(core))
    monkeypatch.setenv("AI_MEMORY_PRIMARY_SOURCE_ID", "core")
    monkeypatch.setenv(
        "AI_MEMORY_RETRIEVAL_SOURCES", f'{{"other": "{core.as_posix()}"}}'
    )

    with pytest.raises(common.ScriptError, match="Duplicate memory source"):
        common.memory_sources()


def test_memory_sources_require_configuration(common, monkeypatch) -> None:
    monkeypatch.delenv("AI_MEMORY_WORK_DIR", raising=False)
    monkeypatch.delenv("AI_MEMORY_DIR", raising=False)

    with pytest.raises(common.ScriptError, match="AI_MEMORY_WORK_DIR"):
        common.memory_sources()


def test_environment_file_does_not_override_explicit_values(
    common, monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(
        'AI_MEMORY_SCRIPT_FIRST="from-file"\n'
        'AI_MEMORY_SCRIPT_SECOND="from-file"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("AI_MEMORY_SCRIPT_FIRST", raising=False)
    monkeypatch.setenv("AI_MEMORY_SCRIPT_SECOND", "from-process")

    common.load_environment(tmp_path)

    assert os.environ["AI_MEMORY_SCRIPT_FIRST"] == "from-file"
    assert os.environ["AI_MEMORY_SCRIPT_SECOND"] == "from-process"


def test_setup_uses_one_configured_memory_root(project_root: Path) -> None:
    setup = _load("setup", "scripts/setup.py", project_root)

    assert "AI_MEMORY_WORK_DIR=" in setup.ENV_TEMPLATE
    assert "AI_MEMORY_MCP_STATE_DIR=" not in setup.ENV_TEMPLATE
    assert "AI_MEMORY_GRAPHIFY_STATE_DIR=" not in setup.ENV_TEMPLATE
    assert "AI_MEMORY_GRAPH_PATH=" not in setup.ENV_TEMPLATE
    assert "AI_MEMORY_ARTIFACT_DB=" not in setup.ENV_TEMPLATE
    assert "AI_MEMORY_ARTIFACT_OBJECTS_DIR=" not in setup.ENV_TEMPLATE
    assert "AI_MEMORY_ARTIFACT_BACKUP_DIR=" not in setup.ENV_TEMPLATE


def test_graphify_state_defaults_inside_memory_root(
    common, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "vault"
    monkeypatch.setenv("AI_MEMORY_WORK_DIR", str(root))
    monkeypatch.setenv("AI_MEMORY_GRAPHIFY_STATE_DIR", "")

    assert common.graphify_state_root() == (
        root / ".ai-memory" / "provider-state" / "graphify"
    )


def test_setup_uses_the_installed_artifact_initializer(
    project_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    setup = _load("setup_artifacts", "scripts/setup.py", project_root)
    application_python = tmp_path / "venv" / "bin" / "python"
    calls: list[tuple[list[str], str]] = []
    monkeypatch.setattr(
        setup,
        "_run",
        lambda command, failure: calls.append((command, failure)),
    )

    setup._initialize_artifact_store(application_python, tmp_path / "repo")

    assert calls == [
        (
            [
                str(tmp_path / "repo" / ".venv" / "bin" / "ai-memory-artifact"),
                "init",
            ],
            "Failed to initialize the artifact database.",
        )
    ]


def test_posix_wrappers_accept_a_python3_only_environment(
    project_root: Path,
) -> None:
    """Wrappers must support every venv layout the path resolver supports."""
    wrappers = [
        *sorted((project_root / "scripts").rglob("*.sh")),
        *sorted((project_root / "graphify-codebase" / "scripts").rglob("*.sh")),
    ]
    assert wrappers
    for wrapper in wrappers:
        text = wrapper.read_text(encoding="utf-8")
        assert '.venv/bin/python3' in text, wrapper


def test_desktop_entry_exec_quotes_reserved_characters(
    project_root: Path,
) -> None:
    autostart = _load(
        "install_autostart", "scripts/graphify/install_autostart.py", project_root
    )
    rendered = autostart._desktop_exec(
        ["/opt/my apps/python", "/srv/a b/start.py", "plain"]
    )

    # A path containing a space must survive as one argument.
    assert '"/opt/my apps/python"' in rendered
    assert '"/srv/a b/start.py"' in rendered
    assert rendered.endswith("plain")

    # Every character the specification reserves forces quoting, because a
    # directory name is free to contain any of them.
    for reserved in ("'", "#", "&", ";", "(", ")", "~", "|", "*", "?", "<", ">"):
        single = autostart._desktop_exec([f"/srv/a{reserved}b/start.py"])
        assert single.startswith('"') and single.endswith('"'), reserved

    # A literal percent must be doubled so it is not read as a field code.
    assert autostart._desktop_exec(["/srv/100%/x"]).count("%%") == 1

    # Inside quotes only these four take a backslash.
    escaped = autostart._desktop_exec(['/srv/a b/"q"$v`t\\z'])
    assert '\\"' in escaped and "\\$" in escaped
    assert "\\`" in escaped and "\\\\" in escaped


def test_standalone_scripts_use_filesystem_identity_for_paths(
    common, tmp_path: Path
) -> None:
    """`_common.path_key` must match the packaged resolver, not fold by OS."""
    target = tmp_path / "Memory"
    target.mkdir()
    other = tmp_path / "Other"
    other.mkdir()

    assert common.path_key(target).startswith("id:")
    assert common.path_key(target) != common.path_key(other)

    # Two routes to one directory share an identity.
    (tmp_path / "sub").mkdir()
    assert common.path_key(target) == common.path_key(
        tmp_path / "sub" / ".." / "Memory"
    )

    # Whether case collides is the volume's decision, not the OS name's.
    lowered = tmp_path / "memory"
    assert (common.path_key(target) == common.path_key(lowered)) == lowered.exists()


def test_systemd_is_rejected_without_a_working_user_manager(
    project_root: Path, monkeypatch
) -> None:
    """An installed systemctl with no running user manager must fall back."""
    autostart = _load(
        "install_autostart", "scripts/graphify/install_autostart.py", project_root
    )

    class Failed:
        returncode = 1
        stdout = ""
        stderr = "Failed to connect to bus"

    monkeypatch.setattr(autostart.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(autostart.subprocess, "run", lambda *a, **k: Failed())

    assert autostart._systemd_user_manager_available() is False


def test_graphify_codebase_prefers_path_like_main(
    project_root: Path, monkeypatch
) -> None:
    """`main` resolved Graphify from PATH; that ordering must be preserved."""
    module = _load(
        "invoke_graphify_codebase",
        "graphify-codebase/scripts/invoke_graphify_codebase.py",
        project_root,
    )
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/local/bin/graphify")

    assert module._graphify_executable() == "/usr/local/bin/graphify"


def test_find_processes_never_returns_this_process(processes) -> None:
    """A match on our own command line must not become a termination target."""
    assert processes.find_processes("pytest") == [] or all(
        process.pid != os.getpid()
        for process in processes.find_processes("pytest")
    )


def test_port_probe_detects_a_live_listener(processes) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        assert processes.port_is_serving("127.0.0.1", port) is True

    assert processes.port_is_serving("127.0.0.1", port) is False


def test_refresh_writes_a_transcript_including_the_failure(
    project_root: Path, tmp_path: Path
) -> None:
    """A failed refresh must leave a transcript explaining why it failed."""
    import subprocess

    environment = dict(os.environ)
    environment["AI_MEMORY_GRAPHIFY_STATE_DIR"] = str(tmp_path)
    environment["AI_MEMORY_GRAPHIFY_PYTHON"] = str(tmp_path / "absent" / "python")

    result = subprocess.run(
        [sys.executable, str(project_root / "scripts" / "graphify" / "refresh_graph.py")],
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
    )

    assert result.returncode != 0
    transcripts = list((tmp_path / "logs" / "ai-memory-refresh").glob("*.log"))
    assert transcripts, "refresh produced no transcript"
    body = transcripts[0].read_text(encoding="utf-8")
    assert "started." in body
    assert "FAILED:" in body


def test_refresh_accepts_progress_before_json(project_root: Path) -> None:
    refresh = _load(
        "refresh_graph_json",
        "scripts/graphify/refresh_graph.py",
        project_root,
    )

    assert refresh.json_summary(
        "Fetching files: 100%\n{\n  \"documents\": 12\n}\n"
    ) == {"documents": 12}


def test_graph_health_parser_requires_a_node_count(project_root: Path) -> None:
    validator = _load(
        "validate_graph_stats",
        "scripts/graphify/validate-ai-memory-graph.py",
        project_root,
    )

    assert validator.graph_stats_node_count("Nodes: 141\nEdges: 94\n") == 141
    with pytest.raises(ValueError, match="invalid graph statistics"):
        validator.graph_stats_node_count("Edges: 94\n")


def test_graph_retrieval_eval_has_no_user_specific_default(
    project_root: Path,
) -> None:
    source = (
        project_root / "scripts" / "graphify" / "run-ai-memory-retrieval-eval.py"
    ).read_text(encoding="utf-8")

    assert "DEFAULT_CASES" not in source
    assert "return ()" in source


def test_refresh_lock_prevents_a_concurrent_publication(
    project_root: Path, tmp_path: Path
) -> None:
    """Two refreshes must never publish at once.

    The lock is taken in a separate process because the Windows primitive does
    not conflict with itself inside one process.
    """
    import subprocess
    import textwrap

    lock = tmp_path / "refresh.lock"
    preamble = (
        "import sys, pathlib\n"
        f"sys.path.insert(0, {str(project_root / 'scripts')!r})\n"
        f"sys.path.insert(0, {str(project_root / 'scripts' / 'graphify')!r})\n"
        "from refresh_graph import exclusive_lock\n"
    )
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            preamble
            + textwrap.dedent(
                f"""
                import time
                with exclusive_lock(pathlib.Path({str(lock)!r})):
                    print("held", flush=True)
                    time.sleep(5)
                """
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"

        contender = subprocess.run(
            [
                sys.executable,
                "-c",
                preamble
                + textwrap.dedent(
                    f"""
                    from _common import ScriptError
                    try:
                        with exclusive_lock(pathlib.Path({str(lock)!r})):
                            print("acquired")
                    except ScriptError as error:
                        print(f"blocked: {{error}}")
                    """
                ),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert "blocked: An AI-Memory Graphify refresh is already running." in (
            contender.stdout
        ), contender.stdout + contender.stderr
    finally:
        holder.kill()
        holder.wait()


def test_wait_for_port_gives_up_when_the_child_exits(processes) -> None:
    import subprocess

    child = subprocess.Popen([sys.executable, "-c", "raise SystemExit(1)"])
    try:
        # An unbound port with a dead child must fail fast rather than block.
        assert processes.wait_for_port("127.0.0.1", 9, 30, child) is False
    finally:
        child.wait()
