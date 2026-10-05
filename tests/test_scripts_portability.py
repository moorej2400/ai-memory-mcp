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
    if scripts_root not in sys.path:
        sys.path.insert(0, scripts_root)
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
    return _load("_processes", "scripts/_processes.py", project_root)


def test_every_entry_point_has_a_wrapper_for_each_shell(
    project_root: Path,
) -> None:
    """Each ported script must stay reachable from PowerShell and POSIX shells."""
    expected = {
        "scripts/setup": "setup",
        "scripts/install-clients": "install_clients",
        "scripts/install-codex": "install_codex",
        "scripts/retire-graphify-memory": "retire_graphify_memory",
        "scripts/run-retrieval-eval": "run_retrieval_eval",
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


def test_setup_template_has_no_graphify_configuration(project_root: Path) -> None:
    setup = _load("setup_template", "scripts/setup.py", project_root)

    assert "GRAPHIFY" not in setup.ENV_TEMPLATE


def test_setup_installs_the_codebase_runtime_only_on_request(
    project_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    setup = _load("setup_flow", "scripts/setup.py", project_root)
    memory_root = tmp_path / "vault"
    memory_root.mkdir()
    repository = tmp_path / "repo"
    (repository / ".venv" / "bin").mkdir(parents=True)
    (repository / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    (repository / ".env").write_text("", encoding="utf-8")
    calls: list[list[str]] = []
    runtime: list[Path] = []
    monkeypatch.setattr(setup, "repository_root", lambda: repository)
    monkeypatch.setattr(setup, "_run", lambda command, failure: calls.append(command))
    monkeypatch.setattr(
        setup,
        "_install_graphify_codebase_runtime",
        lambda root, bootstrap: runtime.append(root),
    )

    for extra in ([], ["--skip-graphify-runtime"]):
        monkeypatch.setattr(
            "sys.argv", ["setup.py", "--memory-root", str(memory_root), *extra]
        )
        setup.main()
    assert runtime == []
    assert calls[-1] == [str(repository / ".venv" / "bin" / "ai-memory-sync")]

    monkeypatch.setattr(
        "sys.argv",
        ["setup.py", "--memory-root", str(memory_root), "--with-graphify-codebase"],
    )
    setup.main()
    assert runtime == [repository]


@pytest.fixture
def retire(project_root: Path):
    return _load(
        "retire_graphify_memory", "scripts/retire_graphify_memory.py", project_root
    )


def _legacy_installation(tmp_path: Path, retire, monkeypatch) -> tuple[Path, Path]:
    root = tmp_path / "vault"
    state = root / ".ai-memory" / "provider-state" / "graphify"
    (state / "corpora").mkdir(parents=True)
    (state / "global-graph.json").write_text("{}", encoding="utf-8")
    home = tmp_path / "home"
    repository = tmp_path / "repo"
    repository.mkdir()
    script = repository / "scripts" / "graphify" / "start_global_mcp.py"
    for launcher in retire.launcher_candidates(home):
        launcher.path.parent.mkdir(parents=True, exist_ok=True)
        launcher.path.write_text(f"python {script}\n", encoding="utf-8")
    (repository / ".env").write_text(
        'AI_MEMORY_WORK_DIR="x"\nGRAPHIFY_GLOBAL_MCP_URL="y"\n', encoding="utf-8"
    )
    monkeypatch.setenv("AI_MEMORY_WORK_DIR", str(root))
    monkeypatch.setenv("AI_MEMORY_GRAPHIFY_STATE_DIR", "")
    monkeypatch.setattr(retire, "repository_root", lambda: repository)
    monkeypatch.setattr(retire, "native_generation_available", lambda: True)
    monkeypatch.setattr(retire, "unregister", lambda launcher: None)
    return root, home


def test_retirement_dry_run_changes_nothing(
    retire, monkeypatch, tmp_path: Path, capsys
) -> None:
    root, home = _legacy_installation(tmp_path, retire, monkeypatch)
    monkeypatch.setattr(retire, "find_processes", lambda *fragments: [])

    retire.main([], home=home)

    output = capsys.readouterr().out
    assert "No change was made" in output
    assert "GRAPHIFY_GLOBAL_MCP_URL" in output
    assert (root / ".ai-memory" / "provider-state" / "graphify").is_dir()
    assert all(item.path.is_file() for item in retire.launcher_candidates(home))
    assert not (root / ".ai-memory" / "backups").exists()


def test_retirement_archives_the_listener_launcher_and_state(
    retire, monkeypatch, tmp_path: Path
) -> None:
    root, home = _legacy_installation(tmp_path, retire, monkeypatch)
    graph = root / ".ai-memory" / "provider-state" / "graphify" / "global-graph.json"
    searched: list[tuple[str, ...]] = []
    stopped: list[int] = []

    class Listener:
        pid = 4242

    def find(*fragments: str):
        searched.append(fragments)
        return [Listener()]

    monkeypatch.setattr(retire, "find_processes", find)
    monkeypatch.setattr(retire, "terminate_tree", stopped.append)

    retire.main(["--apply"], home=home)

    assert searched == [("graphify-mcp", str(graph))]
    assert stopped == [4242]
    assert not (root / ".ai-memory" / "provider-state" / "graphify").exists()
    archived = list((root / ".ai-memory" / "backups" / "graphify-retirement").glob("*"))
    assert len(archived) == 1
    assert (archived[0] / "provider-state" / "graphify" / "global-graph.json").is_file()
    for launcher in retire.launcher_candidates(home):
        assert not launcher.path.exists()
        assert (archived[0] / "launchers" / launcher.path.name).is_file()


def test_retirement_keeps_state_until_a_native_generation_exists(
    retire, monkeypatch, tmp_path: Path, capsys
) -> None:
    root, home = _legacy_installation(tmp_path, retire, monkeypatch)
    monkeypatch.setattr(retire, "find_processes", lambda *fragments: [])
    monkeypatch.setattr(retire, "native_generation_available", lambda: False)

    retire.main(["--apply"], home=home)

    assert (root / ".ai-memory" / "provider-state" / "graphify").is_dir()
    assert "Run ai-memory-sync first" in capsys.readouterr().out


def test_retirement_ignores_an_unrelated_launcher(
    retire, monkeypatch, tmp_path: Path
) -> None:
    root, home = _legacy_installation(tmp_path, retire, monkeypatch)
    for launcher in retire.launcher_candidates(home):
        launcher.path.write_text("some other program\n", encoding="utf-8")

    assert retire.installed_launchers(home, tmp_path / "repo") == []


def test_retirement_keeps_the_launcher_of_another_checkout(
    retire, monkeypatch, tmp_path: Path, capsys
) -> None:
    root, home = _legacy_installation(tmp_path, retire, monkeypatch)
    other = tmp_path / "other-checkout" / "scripts" / "graphify" / "start_global_mcp.py"
    for launcher in retire.launcher_candidates(home):
        launcher.path.write_text(f"python {other}\n", encoding="utf-8")
    monkeypatch.setattr(retire, "find_processes", lambda *fragments: [])

    retire.main(["--apply"], home=home)

    assert "It starts another checkout" in capsys.readouterr().out
    assert all(item.path.is_file() for item in retire.launcher_candidates(home))


@pytest.mark.parametrize(
    ("render", "directory"),
    [
        (lambda path: path.replace("&", "&amp;"), "a&b"),
        (lambda path: path.replace("%", "%%"), "100%"),
        (lambda path: path.replace("\\", "\\\\").replace('"', '\\"'), 'quote"d'),
    ],
)
def test_retirement_recognizes_escaped_launcher_paths(
    retire, tmp_path: Path, render, directory: str
) -> None:
    repository = tmp_path / directory
    script = repository / "scripts" / "graphify" / "start_global_mcp.py"

    assert retire.launcher_owner(f"run {render(str(script))}", repository) == "this"
    assert retire.launcher_owner(f"run {render(str(script))}", tmp_path / "x") == "other"


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


def test_wait_for_port_gives_up_when_the_child_exits(processes) -> None:
    import subprocess

    child = subprocess.Popen([sys.executable, "-c", "raise SystemExit(1)"])
    try:
        # An unbound port with a dead child must fail fast rather than block.
        assert processes.wait_for_port("127.0.0.1", 9, 30, child) is False
    finally:
        child.wait()


def test_retrieval_eval_has_no_user_specific_default(
    project_root: Path, monkeypatch
) -> None:
    evaluation = _load(
        "run_retrieval_eval", "scripts/run_retrieval_eval.py", project_root
    )
    monkeypatch.delenv("AI_MEMORY_RETRIEVAL_EVAL_CASES", raising=False)
    monkeypatch.delenv("GRAPHIFY_MEMORY_RETRIEVAL_EVAL_CASES", raising=False)
    assert evaluation.retrieval_cases() == ()

    monkeypatch.setenv("GRAPHIFY_MEMORY_RETRIEVAL_EVAL_CASES", '[["legacy", "x"]]')
    assert evaluation.retrieval_cases() == (("legacy", "x"),)
    monkeypatch.setenv("AI_MEMORY_RETRIEVAL_EVAL_CASES", '[["current", "y"]]')
    assert evaluation.retrieval_cases() == (("current", "y"),)


@pytest.mark.parametrize("runtime_installed", [False, True])
def test_codex_installs_the_graphify_skill_only_when_usable(
    project_root: Path, monkeypatch, tmp_path: Path, runtime_installed: bool
) -> None:
    codex = _load("install_codex", "scripts/install_codex.py", project_root)
    repository = tmp_path / "repo"
    (repository / ".venv" / "bin").mkdir(parents=True)
    (repository / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    for name, relative in codex.SKILLS.items():
        source = repository / relative
        source.parent.mkdir(parents=True)
        source.write_text(f"---\nname: {name}\ndescription: Test.\n---\n", encoding="utf-8")
    (repository / "requirements-graphify.txt").write_text(
        "graphifyy==0.9.26\n", encoding="utf-8"
    )
    if runtime_installed:
        runtime = repository / ".graphify-runtime" / ("Scripts" if os.name == "nt" else "bin")
        runtime.mkdir(parents=True)
        (runtime / ("graphify.exe" if os.name == "nt" else "graphify")).write_text("")
    monkeypatch.setattr(codex.shutil, "which", lambda name: None)
    codex_home = tmp_path / "codex"
    monkeypatch.setattr(
        "sys.argv",
        ["install_codex.py", "--repository-root", str(repository), "--codex-home", str(codex_home)],
    )

    codex.main()

    assert (codex_home / "skills" / "ai-memory" / "SKILL.md").is_file()
    assert (codex_home / "skills" / "graphify" / "SKILL.md").is_file() is runtime_installed
