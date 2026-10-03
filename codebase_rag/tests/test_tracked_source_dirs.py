"""Issue #2406: git-tracked source under a default-excluded name is indexed.

The default exclusions match a directory NAME at any depth, and several of
the names commonly hold first-party, committed source: Dart's `bin/main.dart`
entry point, a Go package `pkg/out`, a JS config module `src/env`. Their files
never reached the graph, with no notice naming them. A directory with one of
those ambiguous names is now kept when git tracks files in it; untracked or
ignored build output under the same names is still skipped.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from loguru import logger
from watchdog.events import FileModifiedEvent, FileSystemEventHandler

import realtime_updater
from codebase_rag import constants as cs
from codebase_rag.config import CGRIGNORE_FILENAME, load_ignore_patterns, settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.csharp_frontend import frontend as csharp_frontend
from codebase_rag.parsers.go_frontend import frontend as go_frontend
from codebase_rag.parsers.java_frontend import frontend as java_frontend
from codebase_rag.tests.conftest import git_env
from codebase_rag.utils.path_utils import frontend_ignored_dirs, is_eligible_rel_file
from evals.cgr_graph import _StatefulIngestor

TRACKED = {
    "bin/main.dart": 'import "package:defex/defex.dart";\nvoid main() { print(greet()); }\n',
    "lib/defex.dart": 'String greet() => "hi";\n',
    "pubspec.yaml": "name: defex\n",
    "src/app.js": "import { loadEnv } from './env/index.js';\nloadEnv();\n",
    "src/env/index.js": "export function loadEnv() { return 1; }\n",
    "pkg/out/out.go": "package out\nfunc Print(s string) string { return s }\n",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env=git_env(),
    )


@pytest.fixture
def defex(git_repo: Path) -> Path:
    for rel, text in TRACKED.items():
        (git_repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (git_repo / rel).write_text(text)
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "init")
    return git_repo


def _index(repo: Path) -> tuple[_StatefulIngestor, GraphUpdater]:
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    patterns = load_ignore_patterns(repo)
    updater = GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="defex",
        unignore_paths=patterns.unignore or None,
        exclude_paths=patterns.exclude or None,
    )
    updater.run(force=True)
    return store, updater


def _file_paths(store: _StatefulIngestor) -> set[str]:
    return {
        str(props.get(cs.KEY_PATH))
        for (label, _uid), props in store.nodes.items()
        if label == cs.NodeLabel.FILE
    }


def _indexed_paths(repo: Path) -> set[str]:
    return _file_paths(_index(repo)[0])


def test_tracked_source_under_ambiguous_names_is_indexed(defex: Path) -> None:
    indexed = _indexed_paths(defex)

    assert {"bin/main.dart", "src/env/index.js", "pkg/out/out.go"} <= indexed


def test_the_kept_directories_are_named_in_the_log(defex: Path) -> None:
    messages: list[str] = []
    sink = logger.add(messages.append, level="INFO", format="{message}")
    try:
        load_ignore_patterns(defex)
    finally:
        logger.remove(sink)

    kept = [m for m in messages if "bin" in m and "src/env" in m and "pkg/out" in m]
    assert kept, messages


def test_untracked_build_output_under_the_same_names_stays_excluded(
    defex: Path,
) -> None:
    # Negative: `out/` at the root and a nested `tools/bin/` hold only
    # untracked output; the rescue is anchored to the tracked directories.
    for rel in ("out/gen.js", "tools/bin/tool.js", "target/debug/x.js"):
        (defex / rel).parent.mkdir(parents=True, exist_ok=True)
        (defex / rel).write_text("export const X = 1;\n")

    indexed = _indexed_paths(defex)

    assert not {"out/gen.js", "tools/bin/tool.js", "target/debug/x.js"} & indexed
    assert "bin/main.dart" in indexed


def test_a_committed_vendor_directory_stays_excluded(defex: Path) -> None:
    # Negative: only names that commonly hold first-party source are
    # rescued; committed vendored or generated trees are still third-party.
    (defex / "vendor" / "lib.js").parent.mkdir(parents=True)
    (defex / "vendor" / "lib.js").write_text("export const V = 1;\n")
    _git(defex, "add", "-A")
    _git(defex, "commit", "-q", "-m", "vendor")

    assert "vendor/lib.js" not in _indexed_paths(defex)


def test_an_explicit_exclude_still_wins(defex: Path) -> None:
    # Negative: a user who excludes `bin` keeps it excluded.
    (defex / CGRIGNORE_FILENAME).write_text("bin\n")

    assert "bin/main.dart" not in _indexed_paths(defex)


def test_outside_git_the_defaults_apply_as_before(tmp_path: Path) -> None:
    # Negative: with no repository to ask, nothing is rescued.
    repo = tmp_path / "plain"
    for rel, text in TRACKED.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)

    assert load_ignore_patterns(repo).unignore == frozenset()
    assert "bin/main.dart" not in _indexed_paths(repo)


def test_an_untracked_file_beside_tracked_source_stays_excluded(defex: Path) -> None:
    # The rescue is for what git tracks, not for the whole directory: build
    # output written next to `bin/main.dart` is still output (review of
    # PR 2490).
    for rel in ("bin/generated.js", "src/env/local.js"):
        (defex / rel).write_text("export const X = 1;\n")

    indexed = _indexed_paths(defex)

    assert not {"bin/generated.js", "src/env/local.js"} & indexed
    assert {"bin/main.dart", "src/env/index.js"} <= indexed


def test_the_watcher_draws_the_same_line(defex: Path) -> None:
    # The watcher decides one path at a time with the same predicate.
    patterns = load_ignore_patterns(defex)

    def eligible(rel: str) -> bool:
        return is_eligible_rel_file(
            rel, patterns.exclude or None, patterns.unignore or None
        )

    assert eligible("bin/main.dart")
    assert not eligible("bin/generated.js")


def _commit(repo: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "more")


@pytest.mark.parametrize(
    "tracked", ["vendor/bin/lib.js", "node_modules/pkg/bin/cli.js", "bin/vendor/x.js"]
)
def test_a_tracked_file_under_another_default_exclusion_stays_excluded(
    defex: Path, tracked: str
) -> None:
    # Review of PR 2490: the rescue looked only for an ambiguous name, so a
    # committed `vendor/bin/lib.js` came in through `vendor`, which is
    # excluded whatever git tracks.
    _commit(defex, {tracked: "export const x = 1;\n"})

    indexed = _indexed_paths(defex)

    assert tracked not in indexed
    assert "bin/main.dart" in indexed


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows file names cannot hold * or ?"
)
@pytest.mark.parametrize("name", ["*.js", "?.js", "[a-z].js"])
def test_a_tracked_name_with_glob_characters_rescues_nothing_else(
    defex: Path, name: str
) -> None:
    # Review of PR 2490: the tracked path became a pattern as written, so a
    # tracked `bin/*.js` let every untracked `bin/<name>.js` in too.
    _commit(defex, {f"bin/{name}": "export const x = 1;\n"})
    (defex / "bin" / "q.js").write_text("export const leaked = 1;\n")

    indexed = _indexed_paths(defex)

    assert "bin/q.js" not in indexed
    assert "bin/main.dart" in indexed


GO_MOD = "module example.com/defex\n\ngo 1.22\n"
# `o.Hello()` reaches `Base.Hello` only through embedded-struct promotion: the
# go/types tool binds it exactly, the name trie by a heuristic guess.
GO_PROMOTED = (
    "package out\n\n"
    "type Base struct{}\n\n"
    'func (b Base) Hello() string { return "hi" }\n\n'
    "type Outer struct {\n\tBase\n}\n\n"
    "func Caller() string {\n\to := Outer{}\n\treturn o.Hello()\n}\n"
)


def _call_resolutions(store: _StatefulIngestor) -> dict[tuple[str, str], str]:
    return {
        (str(edge[1]), str(edge[4])): str(
            store.edge_props.get(edge, {}).get(cs.KEY_RESOLUTION)
        )
        for edge in store.keyed_edges
        if edge[2] == cs.RelationshipType.CALLS.value
    }


def test_go_calls_in_tracked_source_keep_the_compiler_answer(defex: Path) -> None:
    # The go/types tool filtered its positions by the default-excluded NAMES,
    # so a rescued `pkg/out/out.go` entered the graph while its calls lost the
    # compiler's binding (review of PR 2490).
    go = shutil.which("go")
    if go is None:
        pytest.skip("go toolchain not available")
    if go_frontend._build_tool(go) is None:
        pytest.skip("gotypes tool could not build in this environment")
    _commit(
        defex,
        {
            "go.mod": GO_MOD,
            "pkg/out/out.go": GO_PROMOTED,
            "pkg/util/util.go": GO_PROMOTED.replace("package out", "package util"),
        },
    )

    calls = _call_resolutions(_index(defex)[0])

    exact = cs.EdgeResolution.EXACT.value
    assert (
        calls[("defex.pkg.util.util.Caller", "defex.pkg.util.util.Base.Hello")] == exact
    ), "fixture must take the compiler's binding outside a default exclusion"
    assert calls[("defex.pkg.out.out.Caller", "defex.pkg.out.out.Base.Hello")] == exact


def test_javac_facts_cover_tracked_source(
    defex: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # javac compiled only the sources outside the default-excluded names, so a
    # tracked `out/demo/Caller.java` was indexed with no compiler facts.
    if not java_frontend.java_frontend_available():
        pytest.skip("JDK not available")
    monkeypatch.setattr(settings, "JAVA_FRONTEND", cs.JavaFrontend.JAVAC)
    _commit(
        defex,
        {
            "out/demo/Base.java": (
                "package demo;\n\npublic class Base {\n"
                '  public String hello() { return "hi"; }\n}\n'
            ),
            "out/demo/Caller.java": (
                "package demo;\n\npublic class Caller {\n"
                "  public String run() {\n    return new Base().hello();\n  }\n}\n"
            ),
        },
    )

    _store, updater = _index(defex)

    sites = updater.factory.definition_processor.java_call_sites
    assert {key[0] for key in sites} == {"out/demo/Caller.java"}


def test_the_native_frontends_drop_only_what_the_walk_drops(defex: Path) -> None:
    patterns = load_ignore_patterns(defex)

    ignored = frontend_ignored_dirs(
        defex, patterns.exclude or None, patterns.unignore or None
    )

    assert ignored == cs.IGNORE_PATTERNS - {"bin", "env", "out"}


def test_without_a_rescue_the_native_frontends_drop_every_default(
    tmp_path: Path,
) -> None:
    # Negative: outside git nothing is rescued, so the tools see the full set.
    repo = tmp_path / "plain"
    for rel, text in TRACKED.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)

    assert frontend_ignored_dirs(repo) == cs.IGNORE_PATTERNS


def _tool_env_recorder(
    stdout: str, envs: list[dict[str, str]]
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def fake_run(
        command: list[str], **kwargs: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        envs.append(kwargs["env"])
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    return fake_run


def _run_go(
    repo: Path, ignored: frozenset[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "go.mod").write_text(GO_MOD)
    monkeypatch.setattr(go_frontend.shutil, "which", lambda _name: "/usr/bin/go")
    monkeypatch.setattr(go_frontend, "_build_tool", lambda _go: Path("/fake/gotypes"))
    go_frontend.run_go_frontend(repo, ignored_dirs=ignored)


def _run_java(
    repo: Path, ignored: frozenset[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(java_frontend.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(java_frontend, "_build_tool", lambda _javac: Path("/fake"))
    java_frontend.run_java_frontend(repo, ignored_dirs=ignored)


def _run_csharp(
    repo: Path, ignored: frozenset[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "App.csproj").write_text("<Project />\n")
    monkeypatch.setattr(
        csharp_frontend.shutil, "which", lambda _name: "/usr/bin/dotnet"
    )
    monkeypatch.setattr(csharp_frontend, "_build_tool", lambda _dotnet: Path("/f.dll"))
    monkeypatch.setattr(csharp_frontend, "_restore", lambda _dotnet, _project: None)
    csharp_frontend.run_csharp_frontend(repo, ignored_dirs=ignored)


@pytest.mark.parametrize(
    ("runner", "module"),
    [
        (_run_go, go_frontend),
        (_run_java, java_frontend),
        (_run_csharp, csharp_frontend),
    ],
    ids=["gotypes", "javac", "roslyn"],
)
def test_each_native_frontend_is_handed_the_walks_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: Callable[[Path, frozenset[str], pytest.MonkeyPatch], None],
    module: ModuleType,
) -> None:
    envs: list[dict[str, str]] = []
    monkeypatch.setattr(
        module.subprocess, "run", _tool_env_recorder('{"calls": []}', envs)
    )
    ignored = cs.IGNORE_PATTERNS - {"out"}

    runner(tmp_path, ignored, monkeypatch)

    assert [env["CGR_IGNORE_DIRS"] for env in envs] == [",".join(sorted(ignored))]


class _Observer:
    """Stands in for watchdog's observer and keeps the handler it is given."""

    def __init__(self) -> None:
        self.handlers: list[FileSystemEventHandler] = []

    def schedule(
        self, handler: FileSystemEventHandler, _path: str, recursive: bool
    ) -> None:
        self.handlers.append(handler)

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def join(self) -> None:
        pass


def _interrupt(_seconds: float) -> None:
    raise KeyboardInterrupt


def test_the_standalone_watcher_indexes_and_follows_tracked_source(
    defex: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The watcher built its updater without the ignore patterns, so its
    # initial scan skipped the tracked files under `bin/`, `env/` and `out/`
    # and their later edits were never applied (review of PR 2490).
    (defex / "pkg" / "out" / "gen.go").write_text("package out\nfunc Gen() {}\n")
    observer = _Observer()
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    with monkeypatch.context() as patched:
        patched.setattr(realtime_updater, "Observer", lambda: observer)
        patched.setattr(
            realtime_updater, "time", SimpleNamespace(sleep=_interrupt, time=time.time)
        )
        realtime_updater._run_watcher_loop(
            store, defex, parsers, queries, 0, 0, "defex"
        )

    scanned = _file_paths(store)
    assert {"bin/main.dart", "src/env/index.js", "pkg/out/out.go"} <= scanned
    assert "pkg/out/gen.go" not in scanned

    out_go = defex / "pkg" / "out" / "out.go"
    out_go.write_text(out_go.read_text() + 'func Edited() string { return "e" }\n')
    (handler,) = observer.handlers
    handler.dispatch(FileModifiedEvent(str(out_go)))
    handler.dispatch(FileModifiedEvent(str(defex / "pkg" / "out" / "gen.go")))

    functions = {
        str(uid) for label, uid in store.nodes if label == cs.NodeLabel.FUNCTION
    }
    assert "defex.pkg.out.out.Edited" in functions
    assert "defex.pkg.out.gen.Gen" not in functions
