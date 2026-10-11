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
from unittest.mock import MagicMock

import pytest
from loguru import logger
from watchdog.events import (
    FileCreatedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEventHandler,
)

import realtime_updater
from codebase_rag import constants as cs
from codebase_rag.config import (
    CGRIGNORE_FILENAME,
    git_index_path,
    load_ignore_patterns,
    settings,
)
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.csharp_frontend import frontend as csharp_frontend
from codebase_rag.parsers.frontends.csharp import CSharpFrontend
from codebase_rag.parsers.frontends.go import GoFrontend
from codebase_rag.parsers.frontends.java import JavaJavacFrontend
from codebase_rag.parsers.go_frontend import frontend as go_frontend
from codebase_rag.parsers.java_frontend import frontend as java_frontend
from codebase_rag.tests.conftest import git_env
from codebase_rag.utils.path_utils import (
    frontend_ignored_dirs,
    is_eligible_rel_file,
    rescued_files,
)
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


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows file names cannot end in whitespace"
)
@pytest.mark.parametrize("suffix", [" ", "\t"])
def test_a_tracked_name_with_trailing_whitespace_rescues_nothing_else(
    defex: Path, suffix: str
) -> None:
    # gitwildmatch drops trailing whitespace, so a tracked `bin/q.js ` written
    # as a pattern would let the untracked `bin/q.js` beside it in.
    _commit(defex, {f"bin/q.js{suffix}": "export const x = 1;\n"})
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


JAVA_BASE = (
    "package demo;\n\npublic class Base {\n"
    '  public String hello() { return "hi"; }\n}\n'
)
JAVA_CALLER = (
    "package demo;\n\npublic class Caller {\n"
    "  public String run() {\n    return new Base().hello();\n  }\n}\n"
)


def test_javac_compiles_tracked_source_and_nothing_else_under_its_name(
    defex: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # javac compiled only the sources outside the default-excluded names, so a
    # tracked `out/demo/Caller.java` was indexed with no compiler facts. It is
    # now handed the tracked files themselves, not the name: an untracked
    # copy of `demo.Base` under another `out/` would otherwise compile too and
    # could take the binding from the class the graph holds (review of PR 2490).
    if not java_frontend.java_frontend_available():
        pytest.skip("JDK not available")
    monkeypatch.setattr(settings, "JAVA_FRONTEND", cs.JavaFrontend.JAVAC)
    _commit(
        defex, {"out/demo/Base.java": JAVA_BASE, "out/demo/Caller.java": JAVA_CALLER}
    )
    untracked = {
        "tools/out/demo/Base.java": JAVA_BASE.replace("hi", "copy"),
        "tools/out/demo/Copy.java": JAVA_CALLER.replace("Caller", "Copy"),
    }
    for rel, text in untracked.items():
        (defex / rel).parent.mkdir(parents=True, exist_ok=True)
        (defex / rel).write_text(text)

    _store, updater = _index(defex)

    sites = updater.factory.definition_processor.java_call_sites
    assert {key[0] for key in sites} == {"out/demo/Caller.java"}
    assert {site.target_file for site in sites.values()} == {"out/demo/Base.java"}


def test_the_rescued_files_are_what_the_walk_keeps_under_a_default_name(
    defex: Path,
) -> None:
    (defex / "out" / "gen.js").parent.mkdir()
    (defex / "out" / "gen.js").write_text("export const X = 1;\n")
    patterns = load_ignore_patterns(defex)

    rescued = rescued_files(defex, patterns.exclude or None, patterns.unignore or None)

    assert rescued == {"bin/main.dart", "src/env/index.js", "pkg/out/out.go"}
    assert frontend_ignored_dirs(rescued) == cs.IGNORE_PATTERNS - {"bin", "env", "out"}


def test_without_a_rescue_the_native_frontends_drop_every_default(
    tmp_path: Path,
) -> None:
    # Negative: outside git nothing is rescued, so the tools see the full set.
    repo = tmp_path / "plain"
    for rel, text in TRACKED.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)

    assert rescued_files(repo) == frozenset()
    assert frontend_ignored_dirs(frozenset()) == cs.IGNORE_PATTERNS


class _ToolRuns:
    """Records what each compiler-tool launch was handed."""

    def __init__(self) -> None:
        self.envs: list[dict[str, str]] = []
        self.listings: list[str | None] = []

    def __call__(
        self, command: list[str], **kwargs: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        env = kwargs["env"]
        self.envs.append(env)
        listing = env.get(java_frontend.EXTRA_SOURCES_ENV)
        self.listings.append(Path(listing).read_text() if listing else None)
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")


RESCUED = frozenset({"out/Tracked.java", "out/tracked.go", "out/Tracked.cs"})


def _run_go(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "go.mod").write_text(GO_MOD)
    monkeypatch.setattr(go_frontend.shutil, "which", lambda _name: "/usr/bin/go")
    monkeypatch.setattr(go_frontend, "_build_tool", lambda _go: Path("/fake/gotypes"))
    GoFrontend().run(repo, (), rescued_files=RESCUED)


def _run_java(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(java_frontend.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(java_frontend, "_build_tool", lambda _javac: Path("/fake"))
    JavaJavacFrontend().run(repo, (), rescued_files=RESCUED)


def _run_csharp(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "App.csproj").write_text("<Project />\n")
    monkeypatch.setattr(
        csharp_frontend.shutil, "which", lambda _name: "/usr/bin/dotnet"
    )
    monkeypatch.setattr(csharp_frontend, "_build_tool", lambda _dotnet: Path("/f.dll"))
    monkeypatch.setattr(csharp_frontend, "_restore", lambda _dotnet, _project: None)
    CSharpFrontend().run(repo, (), rescued_files=RESCUED)


@pytest.mark.parametrize(
    ("runner", "module", "ignored", "listing"),
    [
        (_run_go, go_frontend, cs.IGNORE_PATTERNS - {"out"}, None),
        (_run_csharp, csharp_frontend, cs.IGNORE_PATTERNS - {"out"}, None),
        # javac takes the files themselves, so the name stays excluded and an
        # untracked source under another `out/` never compiles.
        (_run_java, java_frontend, cs.IGNORE_PATTERNS, "out/Tracked.java"),
    ],
    ids=["gotypes", "roslyn", "javac"],
)
def test_each_native_frontend_is_handed_the_rescued_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: Callable[[Path, pytest.MonkeyPatch], None],
    module: ModuleType,
    ignored: frozenset[str],
    listing: str | None,
) -> None:
    runs = _ToolRuns()
    monkeypatch.setattr(module.subprocess, "run", runs)

    runner(tmp_path, monkeypatch)

    assert [env["CGR_IGNORE_DIRS"] for env in runs.envs] == [",".join(sorted(ignored))]
    assert runs.listings == [listing]


def test_javac_without_a_rescue_gets_no_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: nothing rescued, nothing extra handed over.
    runs = _ToolRuns()
    monkeypatch.setattr(java_frontend.subprocess, "run", runs)
    monkeypatch.setattr(java_frontend.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(java_frontend, "_build_tool", lambda _javac: Path("/fake"))

    JavaJavacFrontend().run(tmp_path, ())

    assert runs.listings == [None]


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


def _start_watcher(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[_StatefulIngestor, FileSystemEventHandler]:
    """Run the real watcher start-up, then hand back its graph and handler."""
    observer = _Observer()
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    with monkeypatch.context() as patched:
        patched.setattr(realtime_updater, "Observer", lambda: observer)
        patched.setattr(
            realtime_updater, "time", SimpleNamespace(sleep=_interrupt, time=time.time)
        )
        realtime_updater._run_watcher_loop(store, repo, parsers, queries, 0, 0, "defex")
    (handler,) = set(observer.handlers)
    return store, handler


def _functions(store: _StatefulIngestor) -> set[str]:
    return {str(uid) for label, uid in store.nodes if label == cs.NodeLabel.FUNCTION}


def test_the_standalone_watcher_indexes_and_follows_tracked_source(
    defex: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The watcher built its updater without the ignore patterns, so its
    # initial scan skipped the tracked files under `bin/`, `env/` and `out/`
    # and their later edits were never applied (review of PR 2490).
    (defex / "pkg" / "out" / "gen.go").write_text("package out\nfunc Gen() {}\n")
    store, handler = _start_watcher(defex, monkeypatch)

    scanned = _file_paths(store)
    assert {"bin/main.dart", "src/env/index.js", "pkg/out/out.go"} <= scanned
    assert "pkg/out/gen.go" not in scanned

    out_go = defex / "pkg" / "out" / "out.go"
    out_go.write_text(out_go.read_text() + 'func Edited() string { return "e" }\n')
    handler.dispatch(FileModifiedEvent(str(out_go)))
    handler.dispatch(FileModifiedEvent(str(defex / "pkg" / "out" / "gen.go")))

    assert "defex.pkg.out.out.Edited" in _functions(store)
    assert "defex.pkg.out.gen.Gen" not in _functions(store)


def test_the_watcher_follows_a_tracked_file_renamed_under_out(
    defex: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The rescues were read once at start-up, so the renamed file's creation
    # was refused as untracked and it stayed out of the graph (review of
    # PR 2490). `git mv` rewrites the index, and the watcher re-reads its
    # rules when the index changes, even after the move event that preceded it.
    store, handler = _start_watcher(defex, monkeypatch)
    old, new = defex / "pkg" / "out" / "out.go", defex / "pkg" / "out" / "moved.go"
    _git(defex, "mv", "pkg/out/out.go", "pkg/out/moved.go")

    handler.dispatch(FileMovedEvent(str(old), str(new)))
    handler.dispatch(FileModifiedEvent(str(defex / ".git" / "index")))

    assert "defex.pkg.out.moved.Print" in _functions(store)
    assert "defex.pkg.out.out.Print" not in _functions(store)
    assert "pkg/out/moved.go" in _file_paths(store)


def test_the_watcher_indexes_a_file_once_git_tracks_it(
    defex: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, handler = _start_watcher(defex, monkeypatch)
    gen = defex / "pkg" / "out" / "gen.go"
    gen.write_text("package out\nfunc Gen() {}\n")
    handler.dispatch(FileCreatedEvent(str(gen)))
    # Negative: untracked output under `out/` stays out.
    assert "defex.pkg.out.gen.Gen" not in _functions(store)

    _git(defex, "add", "pkg/out/gen.go")
    handler.dispatch(FileModifiedEvent(str(defex / ".git" / "index")))

    assert "defex.pkg.out.gen.Gen" in _functions(store)


def test_the_watcher_applies_an_edited_ignore_file(
    defex: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An edit to `.cgrignore` left later events filtered by the old rules, so
    # a newly excluded file kept being re-ingested (review of PR 2490).
    store, handler = _start_watcher(defex, monkeypatch)
    cgrignore = defex / CGRIGNORE_FILENAME
    cgrignore.write_text("src/\n")
    handler.dispatch(FileCreatedEvent(str(cgrignore)))

    app = defex / "src" / "app.js"
    app.write_text(app.read_text() + "export function added() { return 2; }\n")
    handler.dispatch(FileModifiedEvent(str(app)))

    assert "src/app.js" not in _file_paths(store)
    assert not any(qn.endswith(".added") for qn in _functions(store))
    assert "bin/main.dart" in _file_paths(store)


def test_an_index_write_that_moves_no_rule_keeps_the_updater(
    defex: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: `git status` and friends rewrite the index without changing
    # what is tracked; that must not cost a re-sync.
    _store, handler = _start_watcher(defex, monkeypatch)
    before = handler.updater

    handler.dispatch(FileModifiedEvent(str(defex / ".git" / "index")))

    assert handler.updater is before


def test_a_linked_worktree_watches_its_index_outside_the_checkout(
    git_repo: Path, tmp_path: Path
) -> None:
    # A linked worktree keeps its index under the main repository's git
    # directory, which the recursive watch on the checkout never sees.
    _git(git_repo, "commit", "-q", "--allow-empty", "-m", "init")
    linked = tmp_path / "linked"
    _git(git_repo, "worktree", "add", "-q", str(linked))

    rules = realtime_updater.IgnoreRules(linked, _never_built)

    index = git_index_path(linked)
    assert index is not None
    assert not index.is_relative_to(linked)
    assert rules.outside_dirs() == {index.parent}
    assert index in rules.inputs


def _never_built(_patterns: object) -> GraphUpdater:
    raise AssertionError("no updater is built while only reading the rules")


def _fail_after_delete(*_args: object) -> None:
    raise RuntimeError("died after the delete")


def test_a_rules_change_restores_a_graph_a_failed_reingest_left_partial(
    defex: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A re-ingest that dies after deleting a file's nodes leaves a full
    # rebuild owed. A rules change ran an incremental sync instead, which
    # skipped the unchanged file whose nodes were gone and stamped the partial
    # graph as current (review of PR 2490).
    store, handler = _start_watcher(defex, monkeypatch)
    out_go = defex / "pkg" / "out" / "out.go"
    with monkeypatch.context() as patched:
        patched.setattr(handler.updater, "_reingest_reparse", _fail_after_delete)
        handler.dispatch(FileModifiedEvent(str(out_go)))
    assert "defex.pkg.out.out.Print" not in _functions(store), (
        "fixture must leave the graph partial"
    )

    (defex / "pkg" / "out" / "gen.go").write_text("package out\nfunc Gen() {}\n")
    _git(defex, "add", "pkg/out/gen.go")
    handler.dispatch(FileModifiedEvent(str(defex / ".git" / "index")))

    assert "defex.pkg.out.out.Print" in _functions(store)
    assert "defex.pkg.out.gen.Gen" in _functions(store)
    assert not handler._needs_full_rebuild


class _ChangedRules:
    """Ignore rules that always report a change and build the given updater."""

    def __init__(self, repo: Path, built: MagicMock) -> None:
        self.repo_path = repo
        self.inputs = frozenset({repo / ".git" / "index"})
        self._built = built

    def reload(self) -> bool:
        return True

    def build_updater(self) -> MagicMock:
        return self._built


def _updater_double(repo: Path) -> MagicMock:
    # A real updater always carries both sets, as None when unconfigured.
    return MagicMock(repo_path=repo, exclude_paths=None, unignore_paths=None)


def _rules_handler(
    tmp_path: Path, built: MagicMock, owed_rebuild: bool
) -> FileSystemEventHandler:
    handler = realtime_updater.CodeChangeEventHandler(
        _updater_double(tmp_path),
        debounce_seconds=0,
        ignore_rules=_ChangedRules(tmp_path, built),
    )
    handler._needs_full_rebuild = owed_rebuild
    handler.dispatch(FileModifiedEvent(str(tmp_path / ".git" / "index")))
    return handler


def test_a_rules_change_with_a_rebuild_owed_runs_it_on_the_new_updater(
    tmp_path: Path,
) -> None:
    built = _updater_double(tmp_path)

    handler = _rules_handler(tmp_path, built, owed_rebuild=True)

    built.run.assert_called_once_with(force=True)
    assert not handler._needs_full_rebuild


def test_a_rules_change_with_nothing_owed_stays_incremental(tmp_path: Path) -> None:
    # Negative: the full rebuild is only for a graph a failure left partial.
    built = _updater_double(tmp_path)

    handler = _rules_handler(tmp_path, built, owed_rebuild=False)

    built.run.assert_called_once_with()
    assert not handler._needs_full_rebuild


def test_a_failed_rebuild_on_a_rules_change_keeps_it_owed(tmp_path: Path) -> None:
    # Negative: the flag clears only once the rebuild succeeds, and the
    # failure does not escape the watchdog callback.
    built = _updater_double(tmp_path)
    built.run.side_effect = RuntimeError("rebuild died")

    handler = _rules_handler(tmp_path, built, owed_rebuild=True)

    built.run.assert_called_once_with(force=True)
    assert handler._needs_full_rebuild
