"""Issue #2451: every repository walk treats a symlink the same way.

Indexing followed a FILE link wherever it pointed, so `pkg/linked.py ->
../../outside/private.py` and `NOTES.md -> ../outside/notes.txt` put content
from outside `--repo-path` into the shared graph, while a DIRECTORY link was
dropped without a word. An in-repo file link (`pkg/alias.py -> core.py`) was
indexed as a second module duplicating every definition of its target, and an
in-repo directory link left an empty Package whose absolute path was its
target's.

The policy now: a link is never followed, file or directory, wherever it
points. A target outside the repository is never read; a target inside it is
indexed once, under its own path, when the walk reaches it. Each skipped link
is logged at DEBUG and the run logs one INFO count.

The walk leaves out two more kinds of entry, each named in one WARNING per
run: a named pipe, socket or device, whose open blocked the run for good
(issue #3721), and a file or directory whose name is not valid UTF-8, which
the graph cannot store and whose write ended the run (issue #3722).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Generator
from pathlib import Path
from subprocess import CompletedProcess
from typing import NamedTuple
from unittest.mock import MagicMock, call

import pytest
from loguru import logger
from watchdog.events import FileCreatedEvent, FileDeletedEvent, FileModifiedEvent

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.checkout_state import state_file
from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.language_spec import has_other_language_sibling
from codebase_rag.main import detect_excludable_directories
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers import structure_processor
from codebase_rag.parsers.contracts import discover_contract_operations
from codebase_rag.parsers.csharp_frontend.frontend import (
    find_csharp_project,
    uncovered_csharp_projects,
)
from codebase_rag.parsers.go_frontend import (
    GoCallSite,
    GoSemanticFacts,
    run_go_frontend,
)
from codebase_rag.parsers.go_frontend import frontend as go_frontend
from codebase_rag.parsers.java_lombok import _overlay_from_root, overlay_identity
from codebase_rag.parsers.js_ts.module_paths import (
    _read_manifest as _read_package_manifest,
)
from codebase_rag.tests.conftest import (
    _MockIngestor,
    create_and_run_updater,
    force_mtime_after_cache,
    get_node_names,
    get_nodes,
    get_relationships,
)
from codebase_rag.tools.ast_grep_service import AstGrepService
from codebase_rag.utils import path_utils
from codebase_rag.utils.path_utils import (
    has_implementation_sibling,
    python_stub_has_implementation,
    walk_eligible_files,
)
from codebase_rag.watch import CodeChangeEventHandler
from evals.cgr_graph import _StatefulIngestor

SECRET = "sk-live-123"
# Every link in the `layout` fixture, by its path in the repository.
LINKS = ["NOTES.md", "app/vendored", "pkg/alias.py", "pkg/linkdir", "pkg/linked.py"]


def _link(link: Path, target: str, *, directory: bool = False) -> None:
    try:
        # `Path` spells a relative target with the host's separator: Windows
        # stores a `../x` target verbatim and never resolves it, so the link
        # dangled there and could not stand in for one a build before the
        # fix followed.
        link.symlink_to(Path(target), target_is_directory=directory)
    except OSError:  # Windows without symlink privileges.
        pytest.skip("symlinks need privileges on this host")


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def layout(tmp_path: Path) -> Path:
    """The issue's reproduction, plus an in-repo directory link."""
    outside = tmp_path / "outside"
    _write(outside / "private.py", "def outside_fn():\n    return 1\n")
    _write(outside / "notes.txt", f"# Private notes\n\nAPI key: {SECRET}\n")
    _write(outside / "lib" / "mod.py", "def lib_fn():\n    return 1\n")
    repo = tmp_path / "repo"
    _write(repo / "pkg" / "__init__.py", "")
    _write(
        repo / "pkg" / "core.py",
        "def helper():\n    a = 1\n    b = 2\n    return a + b\n",
    )
    _link(repo / "pkg" / "alias.py", "core.py")
    _link(repo / "pkg" / "linked.py", "../../outside/private.py")
    _link(repo / "NOTES.md", "../outside/notes.txt")
    _link(repo / "pkg" / "linkdir", "../../outside/lib", directory=True)
    (repo / "app").mkdir()
    _link(repo / "app" / "vendored", "../pkg", directory=True)
    return repo


def _walked(repo: Path, **kwargs: frozenset[str] | None) -> list[str]:
    return [rel for _dir, _name, rel in walk_eligible_files(repo, **kwargs)]


@pytest.fixture
def records() -> Generator[list[tuple[str, str]], None, None]:
    seen: list[tuple[str, str]] = []
    handler_id = logger.add(
        lambda message: seen.append(
            (message.record["level"].name, message.record["message"])
        ),
        level="DEBUG",
    )
    yield seen
    logger.remove(handler_id)


class TestWalk:
    def test_a_file_link_out_of_the_repository_is_not_walked(
        self, layout: Path
    ) -> None:
        walked = _walked(layout)
        assert "pkg/linked.py" not in walked
        assert "NOTES.md" not in walked

    def test_an_in_repo_file_link_is_not_walked_beside_its_target(
        self, layout: Path
    ) -> None:
        walked = _walked(layout)
        assert "pkg/alias.py" not in walked
        assert "pkg/core.py" in walked

    def test_every_skipped_link_is_reported_file_or_directory(
        self, layout: Path
    ) -> None:
        reported: list[str] = []
        list(walk_eligible_files(layout, on_symlink=reported.append))
        assert sorted(reported) == LINKS

    def test_a_link_an_ignore_rule_drops_is_not_reported(self, layout: Path) -> None:
        # Negative: the ignore rules decide first, so the count names only
        # links that would otherwise have been indexed.
        (layout / "node_modules").mkdir()
        _link(layout / "node_modules" / "dep.py", "../pkg/core.py")
        reported: list[str] = []
        walked = [
            rel
            for _d, _n, rel in walk_eligible_files(
                layout,
                exclude_paths=frozenset({"NOTES.md"}),
                on_symlink=reported.append,
            )
        ]
        assert "NOTES.md" not in reported
        assert not any(rel.startswith("node_modules/") for rel in reported)
        assert walked == ["pkg/__init__.py", "pkg/core.py"]

    def test_a_repository_reached_through_a_link_is_walked_in_full(
        self, layout: Path
    ) -> None:
        # Negative: only the entries inside the repository are judged, so a
        # `--repo-path` that is itself a link (or under one) loses nothing.
        entry = layout.parent / "repo-link"
        _link(entry, "repo", directory=True)
        assert _walked(entry) == _walked(layout)
        assert {"pkg/__init__.py", "pkg/core.py"} <= set(_walked(entry))


class TestIndex:
    def test_nothing_from_outside_the_repository_reaches_the_graph(
        self, layout: Path, mock_ingestor: MagicMock
    ) -> None:
        create_and_run_updater(layout, mock_ingestor)
        root = layout.resolve()
        for node_call in mock_ingestor.ensure_node_batch.call_args_list:
            properties = node_call.args[1]
            absolute = properties.get(cs.KEY_ABSOLUTE_PATH)
            if isinstance(absolute, str):
                assert Path(absolute).is_relative_to(root), properties
            assert SECRET not in json.dumps(properties, default=str)
        modules = get_node_names(mock_ingestor, cs.NodeLabel.MODULE)
        assert "repo.pkg.linked" not in modules
        assert "repo.NOTES_md" not in modules
        assert not get_node_names(mock_ingestor, cs.NodeLabel.SECTION)

    def test_an_in_repo_link_does_not_duplicate_its_target(
        self, layout: Path, mock_ingestor: MagicMock
    ) -> None:
        create_and_run_updater(layout, mock_ingestor)
        functions = get_node_names(mock_ingestor, cs.NodeLabel.FUNCTION)
        assert functions == {"repo.pkg.core.helper"}

    def test_a_directory_link_in_the_repository_gets_no_container(
        self, layout: Path, mock_ingestor: MagicMock
    ) -> None:
        create_and_run_updater(layout, mock_ingestor)
        containers = {
            node_call.args[1].get(cs.KEY_PATH)
            for node_call in mock_ingestor.ensure_node_batch.call_args_list
            if node_call.args[0] in (cs.NodeLabel.PACKAGE, cs.NodeLabel.FOLDER)
        }
        assert "app/vendored" not in containers
        assert "pkg/linkdir" not in containers
        assert "pkg" in containers

    def test_the_hash_cache_holds_no_link(
        self, layout: Path, mock_ingestor: MagicMock
    ) -> None:
        create_and_run_updater(layout, mock_ingestor)
        cache = json.loads(
            state_file(layout, cs.HASH_CACHE_FILENAME).read_text(encoding="utf-8")
        )
        assert sorted(cache) == ["pkg/__init__.py", "pkg/core.py"]

    def test_each_skipped_link_is_logged_with_one_summary(
        self,
        layout: Path,
        mock_ingestor: MagicMock,
        records: list[tuple[str, str]],
    ) -> None:
        create_and_run_updater(layout, mock_ingestor)
        debug = [text for level, text in records if level == "DEBUG"]
        for link in LINKS:
            assert any(link in text and "symlink" in text for text in debug), link
        outside = (layout.parent / "outside").resolve()
        assert any(
            "pkg/linked.py" in text and str(outside / "private.py") in text
            for text in debug
        )
        summaries = [
            text
            for level, text in records
            if level == "INFO" and "symlink" in text.lower()
        ]
        assert len(summaries) == 1
        assert str(len(LINKS)) in summaries[0]

    def test_a_link_named_as_the_single_file_target_indexes_its_target(
        self, layout: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative: an explicit file target is resolved as before, so it is
        # indexed under the target's own path, never as a second module.
        (layout / ".git").mkdir()
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=layout / "pkg" / "alias.py",
            parsers=parsers,
            queries=queries,
        )
        updater.run()
        functions = get_node_names(mock_ingestor, cs.NodeLabel.FUNCTION)
        assert functions == {"repo.pkg.core.helper"}


def _stateful_updater(root: Path, store: _StatefulIngestor) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store, repo_path=root, parsers=parsers, queries=queries
    )


def _function_qns(store: _StatefulIngestor) -> set[str]:
    return {
        str(uid) for label, uid in store.nodes if label == cs.NodeLabel.FUNCTION.value
    }


def _file_paths(store: _StatefulIngestor) -> set[str]:
    return {
        str(props[cs.KEY_PATH])
        for (label, _uid), props in store.nodes.items()
        if label == cs.NodeLabel.FILE.value
    }


def _remove_indexed_file(
    tmp_path: Path, *, replace_with_link: bool
) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    """Index `core.py` and `old.py`, then delete `old.py` or replace it with
    a link to `core.py`."""
    repo = tmp_path / "repo"
    _write(repo / "core.py", "def helper():\n    return 1\n")
    _write(repo / "old.py", "def old_fn():\n    return 2\n")
    store = _StatefulIngestor()
    updater = _stateful_updater(repo, store)
    updater.run(force=True)
    assert _file_paths(store) == {"core.py", "old.py"}
    (repo / "old.py").unlink()
    if replace_with_link:
        _link(repo / "old.py", "core.py")
    return repo, store, updater


class TestIncrementalSync:
    def test_a_link_in_an_unchanged_directory_is_dropped_by_the_next_sync(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No link at the root, whose mtime the cache write itself moves: only
        # the cached files' own check can see that `sub/linked.py` is a link.
        _write(tmp_path / "outside" / "x.py", "def outside_fn():\n    return 1\n")
        repo = tmp_path / "repo"
        _write(repo / "main.py", "def main():\n    return 0\n")
        _write(repo / "sub" / "own.py", "def own():\n    return 1\n")
        _link(repo / "sub" / "linked.py", "../../outside/x.py")
        store = _StatefulIngestor()
        with monkeypatch.context() as patched:
            for module in (path_utils, structure_processor):
                patched.setattr(
                    module, "is_symlink_entry", lambda _path: False, raising=False
                )
            _stateful_updater(repo, store).run(force=True)
        assert "repo.sub.linked.outside_fn" in _function_qns(store)

        _stateful_updater(repo, store).run()

        assert "repo.sub.linked.outside_fn" not in _function_qns(store)
        assert "repo.sub.own.own" in _function_qns(store)
        cache = json.loads(
            state_file(repo, cs.HASH_CACHE_FILENAME).read_text(encoding="utf-8")
        )
        assert "sub/linked.py" not in cache

    def test_a_sync_after_a_file_became_a_link_keeps_the_targets_file(
        self, tmp_path: Path
    ) -> None:
        # The replaced file's own File node goes; resolving the link named
        # the target's, which the sync deleted instead (#2451 review).
        repo, store, _updater = _remove_indexed_file(tmp_path, replace_with_link=True)
        _stateful_updater(repo, store).run()
        assert _file_paths(store) == {"core.py"}

    def test_a_sync_after_a_file_was_deleted_keeps_the_others_file(
        self, tmp_path: Path
    ) -> None:
        # Negative: a plain deletion still drops exactly its own File node.
        repo, store, _updater = _remove_indexed_file(tmp_path, replace_with_link=False)
        _stateful_updater(repo, store).run()
        assert _file_paths(store) == {"core.py"}

    def test_a_repository_with_links_stays_in_sync(self, layout: Path) -> None:
        # Negative: the in-sync listing skips the links the walk skips, or
        # every later sync would see them as new files and re-run.
        store = _StatefulIngestor()
        _stateful_updater(layout, store).run(force=True)
        assert _stateful_updater(layout, store)._is_already_in_sync() is True

    def test_an_older_index_loses_its_link_nodes_on_the_next_sync(
        self, layout: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A graph built before the fix holds the links' modules; the next
        # ordinary sync removes them, leaving the target's.
        store = _StatefulIngestor()
        with monkeypatch.context() as patched:
            for module in (path_utils, structure_processor):
                patched.setattr(
                    module, "is_symlink_entry", lambda _path: False, raising=False
                )
            _stateful_updater(layout, store).run(force=True)
        assert "repo.pkg.alias.helper" in _function_qns(store)
        assert (cs.NodeLabel.PACKAGE.value, "repo.app.vendored") in store.nodes

        upgraded = _stateful_updater(layout, store)
        assert upgraded._is_already_in_sync() is False
        upgraded.run()

        assert _function_qns(store) == {"repo.pkg.core.helper"}
        packages = {
            uid for label, uid in store.nodes if label == cs.NodeLabel.PACKAGE.value
        }
        # The link's Package shared its target's absolute path, so removing
        # it by that path took the real Package with it.
        assert packages == {"repo.pkg"}


# A deadline for a run that must not block, not a performance assertion.
_RUN_DEADLINE_SECONDS = 60
_PIPE_RELEASES = 20

needs_fifo = pytest.mark.skipif(sys.platform == "win32", reason="named pipes are POSIX")


def _run_within_deadline(updater: GraphUpdater, pipe: Path, **kwargs: bool) -> None:
    """Run `updater` on a thread, failing the test if it is still blocked on `pipe`."""
    _call_within_deadline(lambda: updater.run(**kwargs), pipe)


def _call_within_deadline(work: Callable[[], object], pipe: Path) -> None:
    """Call `work` on a thread, failing the test if it is still blocked on `pipe`.

    Opening a named pipe for reading waits for a writer, so a run that reads
    one never returns. Past the deadline the pipe is opened for writing, which
    releases each blocked open in turn, so the failing run ends with its test.
    """
    raised: list[BaseException] = []

    def _run() -> None:
        try:
            work()
        except BaseException as exc:
            raised.append(exc)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(_RUN_DEADLINE_SECONDS)
    if worker.is_alive():
        for _ in range(_PIPE_RELEASES):
            with contextlib.suppress(OSError):
                os.close(os.open(pipe, os.O_WRONLY | os.O_NONBLOCK))
            worker.join(1)
            if not worker.is_alive():
                break
        pytest.fail(f"the run was still blocked on the named pipe {pipe.name}")
    if raised:
        raise raised[0]


def _pipe_repo(root: Path) -> Path:
    """A repository holding one module and two named pipes, one with a suffix."""
    repo = root / "repo"
    _write(repo / "a.py", "def a():\n    return 1\n")
    os.mkfifo(repo / "pipe.py")
    (repo / "sub").mkdir()
    os.mkfifo(repo / "sub" / "mypipe")
    return repo


def _manifest_repo(root: Path) -> Path:
    """A repository with a Python, a JavaScript, a Go, a Rust and a Dart module
    and no manifest. The Rust and Dart ones import a module of their own, which
    is what makes the resolver look its manifest up."""
    repo = root / "repo"
    _write(repo / "a.py", "def a():\n    return 1\n")
    _write(repo / "b.js", "export function b() {\n  return 1;\n}\n")
    _write(repo / "c.go", "package main\n\nfunc c() int {\n\treturn 1\n}\n")
    _write(
        repo / "src" / "main.rs",
        "mod d;\nuse crate::d::d;\n\nfn main() {\n    d();\n}\n",
    )
    _write(repo / "src" / "d.rs", "pub fn d() {}\n")
    _write(
        repo / "lib" / "e.dart",
        "import 'package:repo/f.dart';\n\nvoid e() {\n  f();\n}\n",
    )
    _write(repo / "lib" / "f.dart", "void f() {}\n")
    return repo


# The manifests a resolver looks up by name rather than finds by the walk.
_LOOKED_UP_MANIFESTS = [
    "go.mod",
    "package.json",
    "pyproject.toml",
    cs.PKG_CARGO_TOML,
    cs.DEP_FILE_PUBSPEC,
]


def _needs_go_type_checker() -> None:
    """Skip unless the go/types helper builds here, as the gotypes tests do."""
    go = shutil.which("go")
    if go is None:
        pytest.skip("go toolchain not available")
    if go_frontend._build_tool(go) is None:
        pytest.skip("gotypes tool could not build in this environment")


def _go_module_repo(root: Path) -> Path:
    """A Go module whose subpackage calls a function of its root package."""
    repo = root / "repo"
    _write(repo / "go.mod", "module example.com/m\n\ngo 1.21\n")
    _write(repo / "m.go", "package m\n\nfunc F() int {\n\treturn 1\n}\n")
    _write(
        repo / "sub" / "sub.go",
        'package sub\n\nimport "example.com/m"\n\nfunc G() int {\n\treturn m.F()\n}\n',
    )
    return repo


# `sub/sub.go`'s call of `m.F`, keyed as the type checker keys it, and `F`.
_GO_CALL = ("sub/sub.go", 6, 10, "F")
_GO_CALLEE = GoCallSite("F", "m.go", 3, 5)


def _go_facts_within_deadline(repo: Path, pipe: Path) -> GoSemanticFacts:
    """The type checker's facts for `repo`, failing the test if it blocks on `pipe`."""
    facts: list[GoSemanticFacts] = []
    _call_within_deadline(lambda: facts.append(run_go_frontend(repo)), pipe)
    return facts[0]


def _replacing_module(folder: Path, target: str) -> None:
    """A Go module at `folder` that calls `example.com/lib`, which its
    `replace` points at `target`, spelled as a go.mod spells it."""
    _write(
        folder / "go.mod",
        "module example.com/app\n\ngo 1.21\n\n"
        "require example.com/lib v0.0.0\n\n"
        f"replace example.com/lib => {target}\n",
    )
    _write(
        folder / "main.go",
        'package main\n\nimport "example.com/lib"\n\nfunc main() {\n\tlib.F()\n}\n',
    )


def _lib_with_pipe(folder: Path) -> Path:
    """Module `example.com/lib` at `folder`, holding a named pipe `zz.go`."""
    _write(folder / "go.mod", "module example.com/lib\n\ngo 1.21\n")
    _write(folder / "lib.go", "package lib\n\nfunc F() {}\n")
    pipe = folder / "zz.go"
    os.mkfifo(pipe)
    return pipe


# Where a `replace` sends Go: a folder outside the repository, named by a
# relative or an absolute path, or reached through a link outside the
# repository or inside it (issue #3744).
_REPLACE_LAYOUTS = ["outside", "absolute", "outside-link", "inside-link"]


def _replace_layout(root: Path, layout: str) -> tuple[Path, Path, Path, str]:
    """The repository, the pipe, the spelling Go opens the pipe by, and the
    module that holds the `replace`, for one of `_REPLACE_LAYOUTS`."""
    repo = root / "repo"
    if layout == "inside-link":
        pipe = _lib_with_pipe(repo / "lib")
        _link(repo / "alias", "lib", directory=True)
        _replacing_module(repo / "app", "../alias")
        return repo, pipe, repo / "alias" / "zz.go", "app"
    pipe = _lib_with_pipe(root / "lib")
    if layout == "outside-link":
        _link(root / "alias", "lib", directory=True)
        _replacing_module(repo, "../alias")
        return repo, pipe, root / "alias" / "zz.go", "."
    _replacing_module(repo, str(root / "lib") if layout == "absolute" else "../lib")
    return repo, pipe, pipe, "."


# Where a go.work in the folder above the repository sends Go: to `lib`
# outside it by the workspace's `replace` or `use`, or on to `dep` by the
# `replace` in the go.mod of `lib`, a module the workspace uses.
_WORKSPACE_LAYOUTS = ["replace", "use", "use-replace"]


def _workspace_layout(root: Path, layout: str) -> tuple[Path, Path]:
    """The repository and the pipe Go reaches, for one of `_WORKSPACE_LAYOUTS`."""
    repo = root / "repo"
    _write(
        repo / "main.go",
        'package main\n\nimport "example.com/lib"\n\nfunc main() {\n\tlib.F()\n}\n',
    )
    require = ""
    if layout == "replace":
        pipe = _lib_with_pipe(root / "lib")
        uses = "use ./repo\n\nreplace example.com/lib => ./lib\n"
        require = "require example.com/lib v0.0.0\n"
    elif layout == "use":
        pipe = _lib_with_pipe(root / "lib")
        uses = "use (\n\t./repo\n\t./lib\n)\n"
    else:
        _write(
            root / "lib" / "go.mod",
            "module example.com/lib\n\ngo 1.21\n\n"
            "require example.com/dep v0.0.0\n\n"
            "replace example.com/dep => ../dep\n",
        )
        _write(
            root / "lib" / "lib.go",
            'package lib\n\nimport "example.com/dep"\n\nfunc F() {\n\tdep.G()\n}\n',
        )
        _write(root / "dep" / "go.mod", "module example.com/dep\n\ngo 1.21\n")
        _write(root / "dep" / "dep.go", "package dep\n\nfunc G() {}\n")
        pipe = root / "dep" / "zz.go"
        os.mkfifo(pipe)
        uses = "use (\n\t./repo\n\t./lib\n)\n"
    _write(root / "go.work", f"go 1.21\n\n{uses}")
    _write(repo / "go.mod", f"module example.com/app\n\ngo 1.21\n\n{require}")
    return repo, pipe


_FAKE_GO_HELPER = "gotypes"


class _HandedRun(NamedTuple):
    facts: GoSemanticFacts
    # What each helper run's overlay leaves out.
    overlays: list[dict[str, str]]
    # The arguments of each `go` command the run started.
    commands: list[list[str]]


def _overlays_handed(repo: Path, monkeypatch: pytest.MonkeyPatch) -> _HandedRun:
    """A run over `repo` with only the helper faked: `go` itself runs."""
    real_run = subprocess.run
    run = _HandedRun(GoSemanticFacts({}, set(), []), [], [])

    def _run(
        cmd: list[str], env: dict[str, str], **kwargs: bool | int | str
    ) -> CompletedProcess[str]:
        if cmd[0] != _FAKE_GO_HELPER:
            run.commands.append(cmd[1:])
            return real_run(cmd, env=env, **kwargs)
        overlay = env.get(go_frontend._OVERLAY_ENV)
        run.overlays.append(
            {}
            if overlay is None
            else json.loads(Path(overlay).read_text(encoding="utf-8"))["Replace"]
        )
        return CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(go_frontend, "_build_tool", lambda _go: Path(_FAKE_GO_HELPER))
    monkeypatch.setattr(go_frontend.subprocess, "run", _run)
    return run._replace(facts=run_go_frontend(repo))


def _needs_go() -> None:
    if shutil.which("go") is None:
        pytest.skip("go toolchain not available")


def _emitted_paths(mock_ingestor: MagicMock) -> set[str]:
    return {
        str(node_call.args[1][cs.KEY_PATH])
        for node_call in mock_ingestor.ensure_node_batch.call_args_list
        if cs.KEY_PATH in node_call.args[1]
    }


@needs_fifo
class TestSpecialFiles:
    def test_a_named_pipe_is_not_walked_and_is_reported(self, tmp_path: Path) -> None:
        repo = _pipe_repo(tmp_path)
        special: list[str] = []
        walked = [
            rel
            for _dir, _name, rel in walk_eligible_files(repo, on_special=special.append)
        ]
        assert walked == ["a.py"]
        assert sorted(special) == ["pipe.py", "sub/mypipe"]

    def test_a_run_over_named_pipes_completes_without_them(
        self,
        tmp_path: Path,
        mock_ingestor: MagicMock,
        records: list[tuple[str, str]],
    ) -> None:
        repo = _pipe_repo(tmp_path)
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor, repo_path=repo, parsers=parsers, queries=queries
        )

        _run_within_deadline(updater, repo / "pipe.py")

        paths = _emitted_paths(mock_ingestor)
        assert "a.py" in paths
        assert not {"pipe.py", "sub/mypipe"} & paths
        cache = json.loads(
            state_file(repo, cs.HASH_CACHE_FILENAME).read_text(encoding="utf-8")
        )
        assert sorted(cache) == ["a.py"]
        warnings = [
            text
            for level, text in records
            if level == "WARNING" and "pipe" in text.lower()
        ]
        assert len(warnings) == 1
        assert "pipe.py" in warnings[0]
        assert "sub/mypipe" in warnings[0]

    @pytest.mark.parametrize("manifest", _LOOKED_UP_MANIFESTS)
    def test_a_pipe_named_as_a_manifest_is_never_read(
        self, tmp_path: Path, mock_ingestor: MagicMock, manifest: str
    ) -> None:
        # The import resolvers look these up by name, listing directories on
        # their own rather than through the walk, and read what they find.
        repo = _manifest_repo(tmp_path)
        os.mkfifo(repo / manifest)
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor, repo_path=repo, parsers=parsers, queries=queries
        )

        _run_within_deadline(updater, repo / manifest)

        paths = _emitted_paths(mock_ingestor)
        assert "a.py" in paths
        assert manifest not in paths

    @pytest.mark.parametrize("manifest", _LOOKED_UP_MANIFESTS)
    def test_a_manifest_linked_to_a_pipe_is_never_read(
        self, tmp_path: Path, mock_ingestor: MagicMock, manifest: str
    ) -> None:
        # Those resolvers follow a linked manifest to what it names, so a
        # link to a pipe blocked their read as the pipe itself did.
        repo = _manifest_repo(tmp_path)
        pipe = tmp_path / "pipe"
        os.mkfifo(pipe)
        (repo / manifest).symlink_to(pipe)
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor, repo_path=repo, parsers=parsers, queries=queries
        )

        _run_within_deadline(updater, pipe)

        paths = _emitted_paths(mock_ingestor)
        assert "a.py" in paths
        assert manifest not in paths

    @pytest.mark.parametrize("contract", ["api.json", "svc.proto"])
    def test_a_pipe_named_as_a_contract_is_never_read(
        self, tmp_path: Path, mock_ingestor: MagicMock, contract: str
    ) -> None:
        # With the io group on, contract discovery lists the repository on
        # its own and reads every spec and proto file it finds there.
        repo = tmp_path / "repo"
        _write(repo / "a.py", "def a():\n    return 1\n")
        os.mkfifo(repo / contract)
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=repo,
            parsers=parsers,
            queries=queries,
            capture=resolve_capture([cs.CaptureGroup.IO.value]),
        )

        _run_within_deadline(updater, repo / contract)

        paths = _emitted_paths(mock_ingestor)
        assert "a.py" in paths
        assert contract not in paths

    def test_a_package_manifest_that_became_a_pipe_is_never_read(
        self, tmp_path: Path
    ) -> None:
        # The watcher keeps the packages it found at start-up and reads a
        # package's manifest again when a later import names it; by then the
        # manifest can have been replaced by a pipe.
        package = tmp_path / "pkg"
        package.mkdir()
        os.mkfifo(package / cs.DEP_FILE_PACKAGE_JSON)
        read: list[object] = []

        _call_within_deadline(
            lambda: read.append(_read_package_manifest(package)),
            package / cs.DEP_FILE_PACKAGE_JSON,
        )

        assert read == [{}]

    def test_a_pipe_in_a_go_module_root_is_never_read(
        self, tmp_path: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An import of exactly a module path has the resolver read each `.go`
        # file it lists in that module's root, to tell a library from a
        # `package main` stub. That is cgr's own reader, so the go toolchain's
        # type checker, which loads the package on its own, is kept out: the
        # tests below cover it (issue #3736).
        monkeypatch.setattr(settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
        repo = tmp_path / "repo"
        _write(repo / "go.mod", "module example.com/m\n\ngo 1.21\n")
        _write(
            repo / "cmd" / "main.go",
            'package main\n\nimport "example.com/m"\n\nfunc main() {\n\tm.F()\n}\n',
        )
        os.mkfifo(repo / "zz.go")
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor, repo_path=repo, parsers=parsers, queries=queries
        )

        _run_within_deadline(updater, repo / "zz.go")

        paths = _emitted_paths(mock_ingestor)
        assert "cmd/main.go" in paths
        assert "zz.go" not in paths

    @pytest.mark.usefixtures("one_go_helper_per_session")
    @pytest.mark.parametrize("pipe", ["zz.go", "sub/zz.go", "build/zz.go"])
    def test_the_go_type_checker_never_opens_a_pipe(
        self, tmp_path: Path, pipe: str
    ) -> None:
        # The type checker loads each package from its folder and opens every
        # `.go` entry there itself (issue #3736). `build` is a folder cgr
        # ignores and Go loads all the same.
        _needs_go_type_checker()
        repo = _go_module_repo(tmp_path)
        (repo / pipe).parent.mkdir(exist_ok=True)
        os.mkfifo(repo / pipe)

        facts = _go_facts_within_deadline(repo, repo / pipe)

        assert facts.degraded_modules == ()
        assert facts.call_sites[_GO_CALL] == _GO_CALLEE

    @pytest.mark.usefixtures("one_go_helper_per_session")
    def test_the_go_type_checker_never_opens_a_link_to_a_pipe(
        self, tmp_path: Path
    ) -> None:
        _needs_go_type_checker()
        repo = _go_module_repo(tmp_path)
        pipe = tmp_path / "pipe"
        os.mkfifo(pipe)
        _link(repo / "zz.go", str(pipe))

        facts = _go_facts_within_deadline(repo, pipe)

        assert facts.degraded_modules == ()
        assert facts.call_sites[_GO_CALL] == _GO_CALLEE

    @pytest.mark.usefixtures("one_go_helper_per_session")
    def test_the_go_type_checker_never_opens_a_pipe_in_a_sibling_module(
        self, tmp_path: Path
    ) -> None:
        # A `replace` has the run for `app` load `lib`'s packages from their
        # folder in the repository as well.
        _needs_go_type_checker()
        repo = tmp_path / "repo"
        _write(repo / "lib" / "go.mod", "module example.com/lib\n\ngo 1.21\n")
        _write(repo / "lib" / "lib.go", "package lib\n\nfunc F() {}\n")
        _write(
            repo / "app" / "go.mod",
            "module example.com/app\n\ngo 1.21\n\n"
            "require example.com/lib v0.0.0\n\n"
            "replace example.com/lib => ../lib\n",
        )
        _write(
            repo / "app" / "main.go",
            'package main\n\nimport "example.com/lib"\n\nfunc main() {\n\tlib.F()\n}\n',
        )
        os.mkfifo(repo / "lib" / "zz.go")

        facts = _go_facts_within_deadline(repo, repo / "lib" / "zz.go")

        assert facts.degraded_modules == ()
        assert facts.analysed_modules == ("app", "lib")

    def test_the_go_type_checker_is_told_to_leave_out_each_pipe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # What the helper is handed, so a host without Go checks it too: each
        # pipe and link to one in the repository, and no other entry.
        repo = _go_module_repo(tmp_path)
        os.mkfifo(repo / "zz.go")
        (repo / "build").mkdir()
        os.mkfifo(repo / "build" / "zz.go")
        pipe = tmp_path / "pipe"
        os.mkfifo(pipe)
        _link(repo / "yy.go", str(pipe))
        _link(repo / "alias.go", "m.go")
        _link(repo / "linkdir", "sub", directory=True)
        _link(repo / "dangling.go", "missing.go")
        handed: list[dict[str, dict[str, str]]] = []

        def _run(
            cmd: list[str], env: dict[str, str], **_kwargs: bool | int | str
        ) -> CompletedProcess[str]:
            overlay = Path(env[go_frontend._OVERLAY_ENV])
            handed.append(json.loads(overlay.read_text(encoding="utf-8")))
            return CompletedProcess(cmd, 0, stdout="{}", stderr="")

        monkeypatch.setattr(go_frontend.shutil, "which", lambda _name: "go")
        monkeypatch.setattr(go_frontend, "_build_tool", lambda _go: Path("gotypes"))
        monkeypatch.setattr(go_frontend.subprocess, "run", _run)

        run_go_frontend(repo)

        assert handed == [
            {
                "Replace": {
                    str(repo / "build" / "zz.go"): "",
                    str(repo / "yy.go"): "",
                    str(repo / "zz.go"): "",
                }
            }
        ]

    @pytest.mark.usefixtures("one_go_helper_per_session")
    @pytest.mark.parametrize("layout", _REPLACE_LAYOUTS)
    def test_the_go_type_checker_never_opens_a_pipe_a_replace_reaches(
        self, tmp_path: Path, layout: str
    ) -> None:
        # A local `replace` has Go load the target's packages from the folder
        # it names, by that spelling, which the repository's own listing
        # never reaches outside it or through a link (issue #3744).
        _needs_go_type_checker()
        repo, pipe, _opened, module = _replace_layout(tmp_path, layout)

        facts = _go_facts_within_deadline(repo, pipe)

        assert facts.degraded_modules == ()
        assert module in facts.analysed_modules

    @pytest.mark.parametrize("layout", _REPLACE_LAYOUTS)
    def test_the_go_type_checker_is_told_to_leave_out_a_pipe_a_replace_reaches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
    ) -> None:
        # The overlay matches by spelling, so the pipe is left out as the
        # `replace` path joined with its place in the target; one inside the
        # repository keeps its own spelling as well.
        _needs_go()
        repo, pipe, opened, _module = _replace_layout(tmp_path, layout)
        expected = {str(opened)} | ({str(pipe)} if pipe.is_relative_to(repo) else set())

        handed = _overlays_handed(repo, monkeypatch).overlays

        assert handed
        assert all(overlay == dict.fromkeys(expected, "") for overlay in handed)

    @pytest.mark.parametrize("target", ["example.com/other v1.0.0", "../missing"])
    def test_a_replace_by_module_path_or_to_no_folder_adds_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
    ) -> None:
        _needs_go()
        repo = tmp_path / "repo"
        _replacing_module(repo, target)
        os.mkfifo(repo / "zz.go")

        run = _overlays_handed(repo, monkeypatch)

        assert run.overlays == [{str(repo / "zz.go"): ""}]
        assert run.facts.analysed_modules == (".",)

    @pytest.mark.usefixtures("one_go_helper_per_session")
    @pytest.mark.parametrize("layout", _WORKSPACE_LAYOUTS)
    def test_the_go_type_checker_never_opens_a_pipe_a_workspace_reaches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
    ) -> None:
        # Go finds a go.work in any folder above the module, applies its
        # `replace` and loads its `use` modules, wherever they are.
        _needs_go_type_checker()
        monkeypatch.delenv("GOWORK", raising=False)
        repo, pipe = _workspace_layout(tmp_path, layout)

        facts = _go_facts_within_deadline(repo, pipe)

        assert facts.degraded_modules == ()
        assert facts.analysed_modules == (".",)

    @pytest.mark.parametrize("layout", _WORKSPACE_LAYOUTS)
    def test_the_go_type_checker_is_told_to_leave_out_a_pipe_a_workspace_reaches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
    ) -> None:
        _needs_go()
        monkeypatch.delenv("GOWORK", raising=False)
        repo, pipe = _workspace_layout(tmp_path, layout)

        run = _overlays_handed(repo, monkeypatch)

        assert run.overlays == [{str(pipe): ""}]

    def test_a_workspace_turned_off_adds_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _needs_go()
        monkeypatch.setenv("GOWORK", "off")
        repo, _pipe = _workspace_layout(tmp_path, "replace")

        run = _overlays_handed(repo, monkeypatch)

        assert run.overlays == [{}]
        assert run.commands == []

    def test_without_a_workspace_or_a_replace_no_go_command_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _needs_go()
        monkeypatch.delenv("GOWORK", raising=False)
        repo = _go_module_repo(tmp_path)
        os.mkfifo(repo / "zz.go")

        run = _overlays_handed(repo, monkeypatch)

        assert run.overlays == [{str(repo / "zz.go"): ""}]
        assert run.commands == []

    def test_the_listing_of_a_replace_target_is_bounded(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        records: list[tuple[str, str]],
    ) -> None:
        # A `replace` can name any folder, `/` included.
        _needs_go()
        repo, _pipe, _opened, _module = _replace_layout(tmp_path, "outside")
        monkeypatch.setattr(cs, "GO_REPLACE_TARGET_MAX_ENTRIES", 1)

        run = _overlays_handed(repo, monkeypatch)

        assert run.facts.analysed_modules == (".",)
        assert len(run.overlays) == 1
        assert len(run.overlays[0]) <= 1
        warnings = [text for level, text in records if level == "WARNING"]
        assert any(str(tmp_path / "lib") in text for text in warnings)

    def test_a_pipe_among_the_rust_sources_is_never_read(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        # A `super::` import has the resolver sweep every `.rs` file of the
        # repository for `#[path]` attributes, by a walk of its own.
        repo = tmp_path / "repo"
        _write(repo / cs.PKG_CARGO_TOML, '[package]\nname = "c"\n')
        _write(repo / "src" / "lib.rs", "mod a;\nmod b;\n")
        _write(repo / "src" / "a.rs", "use super::b::f;\n\npub fn g() {\n    f();\n}\n")
        _write(repo / "src" / "b.rs", "pub fn f() {}\n")
        os.mkfifo(repo / "src" / "zz.rs")
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor, repo_path=repo, parsers=parsers, queries=queries
        )

        _run_within_deadline(updater, repo / "src" / "zz.rs")

        paths = _emitted_paths(mock_ingestor)
        assert {"src/a.rs", "src/b.rs"} <= paths
        assert "src/zz.rs" not in paths

    @pytest.mark.parametrize("squatter", ["pipe", "directory"])
    def test_a_non_file_named_like_a_module_leaves_its_mod_rs_in_charge(
        self, tmp_path: Path, squatter: str
    ) -> None:
        # `src/engine.rs` and `src/engine/mod.rs` can both back module
        # `engine`, and the resolver tries the sibling `.rs` first. A pipe or
        # a folder of that name is not an indexed file, so `mod.rs` and its
        # `#[path]` redirect must place the module exactly as they do with
        # nothing there.
        def imports(with_squatter: bool) -> set[tuple[object, object]]:
            repo = tmp_path / (squatter if with_squatter else "plain") / "repo"
            _write(repo / cs.PKG_CARGO_TOML, '[package]\nname = "c"\n')
            _write(
                repo / "src" / "lib.rs",
                "mod engine;\nuse crate::engine::inner::run;\n\npub fn go() {\n    run();\n}\n",
            )
            _write(
                repo / "src" / "engine" / "mod.rs",
                '#[path = "impl_inner.rs"]\npub mod inner;\n',
            )
            _write(repo / "src" / "engine" / "impl_inner.rs", "pub fn run() {}\n")
            ingestor = _MockIngestor()
            parsers, queries = load_parsers()
            updater = GraphUpdater(
                ingestor=ingestor, repo_path=repo, parsers=parsers, queries=queries
            )
            if not with_squatter:
                updater.run()
            elif squatter == "pipe":
                os.mkfifo(repo / "src" / "engine.rs")
                _run_within_deadline(updater, repo / "src" / "engine.rs")
            else:
                (repo / "src" / "engine.rs").mkdir()
                updater.run()
            return {
                (rel.args[0][2], rel.args[2][2])
                for rel in get_relationships(ingestor, cs.RelationshipType.IMPORTS)
            }

        plain = imports(with_squatter=False)

        assert plain, "the fixture must resolve the import through mod.rs"
        assert imports(with_squatter=True) == plain

    def test_a_workspace_members_manifest_that_is_a_pipe_is_never_read(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        # A `use` of another crate has the resolver read the manifest of every
        # member the workspace's `members` globs match.
        repo = tmp_path / "repo"
        _write(repo / cs.PKG_CARGO_TOML, '[workspace]\nmembers = ["crates/*"]\n')
        _write(
            repo / "crates" / "app" / cs.PKG_CARGO_TOML,
            '[package]\nname = "app"\n\n[dependencies]\nother = { path = "../other" }\n',
        )
        _write(
            repo / "crates" / "app" / "src" / "main.rs",
            "use other::thing;\n\nfn main() {\n    thing();\n}\n",
        )
        _write(repo / "crates" / "other" / "src" / "lib.rs", "pub fn thing() {}\n")
        pipe = repo / "crates" / "other" / cs.PKG_CARGO_TOML
        os.mkfifo(pipe)
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor, repo_path=repo, parsers=parsers, queries=queries
        )

        _run_within_deadline(updater, pipe)

        paths = _emitted_paths(mock_ingestor)
        assert "crates/app/src/main.rs" in paths
        assert "crates/other/Cargo.toml" not in paths

    @pytest.mark.parametrize("pipe", ["src/lib.rs", "src/a.rs"])
    def test_a_rust_entry_or_module_file_that_is_a_pipe_is_never_read(
        self, tmp_path: Path, mock_ingestor: MagicMock, pipe: str
    ) -> None:
        # A `crate::` path has the resolver read the crate's entry files, and
        # the file of each module the path walks through, which it finds by
        # listing their directories.
        repo = tmp_path / "repo"
        files = {
            cs.PKG_CARGO_TOML: '[package]\nname = "c"\n',
            "src/main.rs": "mod a;\nuse crate::a::inner::x;\n\nfn main() {\n    x();\n}\n",
            "src/lib.rs": "pub mod a;\n",
            "src/a.rs": "pub mod inner;\n",
            "src/a/inner.rs": "pub fn x() {}\n",
        }
        for rel, text in files.items():
            if rel != pipe:
                _write(repo / rel, text)
        os.mkfifo(repo / pipe)
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor, repo_path=repo, parsers=parsers, queries=queries
        )

        _run_within_deadline(updater, repo / pipe)

        paths = _emitted_paths(mock_ingestor)
        assert "src/main.rs" in paths
        assert pipe not in paths

    def test_a_pipe_named_as_a_csharp_project_is_never_a_candidate(
        self, tmp_path: Path
    ) -> None:
        # Where dotnet is installed, the Roslyn pre-pass picks a solution or
        # project by `rglob` and reads the solution to learn its members.
        repo = tmp_path / "repo"
        _write(repo / "src" / "App.csproj", "<Project />\n")
        os.mkfifo(repo / "App.sln")
        os.mkfifo(repo / "bench.csproj")
        found: list[object] = []

        def _discover() -> None:
            project = find_csharp_project(repo)
            assert project is not None
            found.extend([project, uncovered_csharp_projects(repo, project)])

        _call_within_deadline(_discover, repo / "App.sln")

        assert found == [repo / "src" / "App.csproj", []]

    def test_a_cached_file_replaced_by_a_pipe_is_dropped_by_the_next_sync(
        self, tmp_path: Path
    ) -> None:
        # The in-sync check hashes a cached file whose change time moved past
        # the cache, and a pipe made where the file was has just been made.
        repo = tmp_path / "repo"
        _write(repo / "a.py", "def a():\n    return 1\n")
        _write(repo / "b.py", "def b():\n    return 2\n")
        store = _StatefulIngestor()
        _stateful_updater(repo, store).run(force=True)
        assert _file_paths(store) == {"a.py", "b.py"}
        (repo / "b.py").unlink()
        os.mkfifo(repo / "b.py")

        _run_within_deadline(_stateful_updater(repo, store), repo / "b.py")

        assert _file_paths(store) == {"a.py"}
        assert _function_qns(store) == {"repo.a.a"}

    def test_a_repository_with_a_pipe_stays_in_sync(self, tmp_path: Path) -> None:
        # The in-sync listing leaves out the pipes the walk leaves out, or a
        # directory whose mtime moved would hold a "new file" on every sync.
        repo = _pipe_repo(tmp_path)
        store = _StatefulIngestor()
        _run_within_deadline(
            _stateful_updater(repo, store), repo / "pipe.py", force=True
        )
        stamp = repo.stat().st_mtime + 1
        os.utime(repo, (stamp, stamp))
        assert _stateful_updater(repo, store)._is_already_in_sync() is True

    @pytest.mark.parametrize("operation", ["search", "replace"])
    def test_structural_search_never_opens_a_pipe(
        self, tmp_path: Path, operation: str
    ) -> None:
        # It lists the repository on its own and read each source file it
        # found there (issue #3737).
        repo = tmp_path / "repo"
        _write(repo / "core.py", "API_TOKEN = 1\n")
        os.mkfifo(repo / "zz.py")
        service = AstGrepService(project_root=str(repo))
        files: list[list[str]] = []

        def _run() -> None:
            if operation == "search":
                found = service.search(pattern="API_TOKEN", language="python")
                files.append([match["file"] for match in found])
            else:
                changed = service.replace(
                    pattern="API_TOKEN", rewrite="API_KEY", language="python"
                )
                files.append([change["file"] for change in changed])

        _call_within_deadline(_run, repo / "zz.py")

        assert files == [["core.py"]]


def _make_undecodable(parent: Path, raw_name: bytes, text: str | None = None) -> Path:
    """Create `parent/raw_name`, a directory when `text` is None, or skip.

    APFS and Windows refuse a name that is not valid UTF-8, so the case exists
    only where the filesystem stores a name's bytes as they are.
    """
    if sys.platform == "win32":
        # Windows paths are text, so such a name cannot even be spelled.
        pytest.skip("this filesystem refuses a name that is not valid UTF-8")
    raw = os.path.join(os.fsencode(parent), raw_name)
    try:
        if text is None:
            os.mkdir(raw)
        else:
            with open(raw, "wb") as handle:
                handle.write(text.encode(cs.ENCODING_UTF8))
    except (OSError, UnicodeError):
        pytest.skip("this filesystem refuses a name that is not valid UTF-8")
    return Path(os.fsdecode(raw))


def _undecodable_repo(root: Path) -> Path:
    """One good module, and undecodable names at the root, in a package and
    on a directory and a package."""
    repo = root / "repo"
    _write(repo / "a.py", "def a():\n    return 1\n")
    _write(repo / "pkg" / "__init__.py", "")
    _make_undecodable(repo, b"notes\xff.txt", "notes\n")
    _make_undecodable(repo / "pkg", b"bad\xff.py", "def g():\n    return 2\n")
    folder = _make_undecodable(repo, b"dir\xff")
    _write(folder / "c.py", "def c():\n    return 3\n")
    package = _make_undecodable(repo, b"pkg\xff")
    _write(package / "__init__.py", "")
    _write(package / "d.py", "def d():\n    return 4\n")
    return repo


# As the warning spells them: each byte that is not UTF-8 as `\xff`.
_UNDECODABLE = ["dir\\xff", "notes\\xff.txt", "pkg/bad\\xff.py", "pkg\\xff"]


def _unstorable(mock_ingestor: MagicMock) -> list[str]:
    """Every node or relationship the run emitted that holds a lone surrogate."""
    emitted = [
        *mock_ingestor.ensure_node_batch.call_args_list,
        *mock_ingestor.ensure_relationship_batch.call_args_list,
    ]
    unstorable: list[str] = []
    for emitted_call in emitted:
        text = json.dumps(emitted_call.args, default=str, ensure_ascii=False)
        try:
            text.encode(cs.ENCODING_UTF8)
        except UnicodeEncodeError:
            unstorable.append(text)
    return unstorable


class TestUndecodableNames:
    def test_a_name_that_is_not_utf8_is_not_walked_and_is_reported(
        self, tmp_path: Path
    ) -> None:
        repo = _undecodable_repo(tmp_path)
        undecodable: list[str] = []
        walked = [
            rel
            for _dir, _name, rel in walk_eligible_files(
                repo, on_undecodable=undecodable.append
            )
        ]
        assert walked == ["a.py", "pkg/__init__.py"]
        readable = sorted(path_utils.readable_name(rel) for rel in undecodable)
        assert readable == _UNDECODABLE

    def test_a_run_completes_and_no_node_holds_the_name(
        self,
        tmp_path: Path,
        mock_ingestor: MagicMock,
        records: list[tuple[str, str]],
    ) -> None:
        repo = _undecodable_repo(tmp_path)
        create_and_run_updater(repo, mock_ingestor)

        assert _unstorable(mock_ingestor) == []
        assert _emitted_paths(mock_ingestor) >= {"a.py", "pkg/__init__.py", "pkg"}
        assert get_node_names(mock_ingestor, cs.NodeLabel.FUNCTION) == {"repo.a.a"}
        cache = json.loads(
            state_file(repo, cs.HASH_CACHE_FILENAME).read_text(encoding="utf-8")
        )
        assert sorted(cache) == ["a.py", "pkg/__init__.py"]
        warnings = [
            text for level, text in records if level == "WARNING" and "UTF-8" in text
        ]
        assert len(warnings) == 1
        for name in _UNDECODABLE:
            assert name in warnings[0]

    def test_an_edit_in_a_go_package_reparses_no_sibling_with_such_a_name(
        self,
        tmp_path: Path,
        mock_ingestor: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The watcher drops an event for such a name, but an edit to a sibling
        # has the re-ingest list the package's folder for every `.go` file.
        monkeypatch.setattr(settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)
        repo = tmp_path / "repo"
        _write(repo / "go.mod", "module example.com/m\n\ngo 1.21\n")
        _write(repo / "a.go", "package main\n\nfunc a() int {\n\treturn 1\n}\n")
        _make_undecodable(
            repo, b"bad\xff.go", "package main\n\nfunc b() int {\n\treturn 2\n}\n"
        )
        updater = create_and_run_updater(repo, mock_ingestor)
        mock_ingestor.reset_mock()

        edited = _write(
            repo / "a.go", "package main\n\nfunc a() int {\n\treturn 3\n}\n"
        )
        CodeChangeEventHandler(updater, debounce_seconds=0).dispatch(
            FileModifiedEvent(str(edited))
        )

        assert "a.go" in _emitted_paths(mock_ingestor)
        assert _unstorable(mock_ingestor) == []

    def test_a_package_flip_reparses_no_file_with_such_a_name(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        # A folder that becomes a package has every file in it re-parsed onto
        # the new Package, found by listing the folder.
        repo = tmp_path / "repo"
        _write(repo / "pkg" / "core.py", "def helper():\n    return 1\n")
        _make_undecodable(repo / "pkg", b"bad\xff.py", "def g():\n    return 2\n")
        updater = create_and_run_updater(repo, mock_ingestor)
        mock_ingestor.reset_mock()

        updater.reingest((_write(repo / "pkg" / "__init__.py", ""),))

        assert "pkg/core.py" in _emitted_paths(mock_ingestor)
        assert _unstorable(mock_ingestor) == []

    def test_a_workspace_member_with_such_a_name_maps_no_crate(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        # The crate map is filled from the members the workspace globs match,
        # and a `use` of a crate it holds resolves into the member's folder.
        repo = tmp_path / "repo"
        _write(repo / cs.PKG_CARGO_TOML, '[workspace]\nmembers = ["crates/*"]\n')
        _write(repo / "crates" / "app" / cs.PKG_CARGO_TOML, '[package]\nname = "app"\n')
        _write(repo / "crates" / "app" / "src" / "lib.rs", "pub fn run() {}\n")
        member = _make_undecodable(repo / "crates", b"other\xff")
        _write(member / cs.PKG_CARGO_TOML, '[package]\nname = "other"\n')
        _write(member / "src" / "lib.rs", "pub fn thing() {}\n")
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=mock_ingestor, repo_path=repo, parsers=parsers, queries=queries
        )

        crates = updater.factory.import_processor._rust_workspace_crate_roots()

        assert crates == {"app": (("crates", "app"), ("crates", "app", "src"), "lib")}

    def test_a_delomboked_file_with_such_a_name_is_left_out(
        self, tmp_path: Path
    ) -> None:
        # The overlay keys each expansion by the file's path, and its identity
        # hashes every key as UTF-8.
        repo = tmp_path / "repo"
        source_root = repo / "src" / "main" / "java"
        expanded = tmp_path / "expanded"
        _write(source_root / "Good.java", "class Good {}\n")
        _write(expanded / "Good.java", "class Good { int x; }\n")
        _make_undecodable(source_root, b"Bad\xff.java", "class Bad {}\n")
        _make_undecodable(expanded, b"Bad\xff.java", "class Bad { int y; }\n")

        overlay = _overlay_from_root(repo, source_root, expanded)

        assert sorted(overlay) == ["src/main/java/Good.java"]
        assert overlay_identity(overlay)


@pytest.fixture
def watched(temp_repo: Path) -> Generator[Path, None, None]:
    outside = temp_repo.parent / f"{temp_repo.name}-outside.py"
    _write(outside, "def outside_fn():\n    return 1\n")
    _write(temp_repo / "core.py", "def helper():\n    return 1\n")
    _link(temp_repo / "alias.py", "core.py")
    _link(temp_repo / "linked.py", str(outside))
    yield temp_repo
    outside.unlink(missing_ok=True)


class TestWatcher:
    def test_the_watcher_ignores_a_link(
        self, mock_updater: MagicMock, watched: Path
    ) -> None:
        handler = CodeChangeEventHandler(mock_updater, debounce_seconds=0)
        assert not handler._is_relevant(str(watched / "alias.py"))
        assert not handler._is_relevant(str(watched / "linked.py"))
        assert handler._is_relevant(str(watched / "core.py"))

    def test_a_created_link_is_not_reingested(
        self, mock_updater: MagicMock, watched: Path
    ) -> None:
        handler = CodeChangeEventHandler(mock_updater, debounce_seconds=0)
        mock_updater.has_indexed.return_value = False
        handler.dispatch(FileCreatedEvent(str(watched / "linked.py")))
        handler.dispatch(FileCreatedEvent(str(watched / "alias.py")))
        mock_updater.reingest.assert_not_called()

    def test_an_indexed_file_replaced_by_a_link_is_removed_not_reingested(
        self, mock_updater: MagicMock, watched: Path
    ) -> None:
        # Both events arrive after the swap, so the path is a link by the
        # time each is handled; the graph still holds the file it replaced.
        handler = CodeChangeEventHandler(mock_updater, debounce_seconds=0)
        mock_updater.has_indexed.return_value = True
        path = watched / "alias.py"
        handler.dispatch(FileDeletedEvent(str(path)))
        handler.dispatch(FileCreatedEvent(str(path)))
        assert mock_updater.reingest.call_args_list == [call((), deleted=(path,))] * 2

    def test_the_watcher_and_the_walk_agree_on_links(
        self, mock_updater: MagicMock, watched: Path
    ) -> None:
        handler = CodeChangeEventHandler(mock_updater, debounce_seconds=0)
        names = ["alias.py", "core.py", "linked.py"]
        walked = set(_walked(watched))
        assert {n for n in names if handler._is_relevant(str(watched / n))} == walked

    def test_a_deleted_link_still_reaches_the_graph(
        self, mock_updater: MagicMock, watched: Path
    ) -> None:
        # Negative: once the link is gone there is nothing to follow, and a
        # graph built before the fix may still hold its module.
        handler = CodeChangeEventHandler(mock_updater, debounce_seconds=0)
        (watched / "alias.py").unlink()
        handler.dispatch(FileDeletedEvent(str(watched / "alias.py")))
        mock_updater.reingest.assert_called_once_with(
            (), deleted=(watched / "alias.py",)
        )


class TestSiblingPredicates:
    def test_a_linked_implementation_does_not_take_the_declarations_name(
        self, tmp_path: Path
    ) -> None:
        # The walk no longer indexes `foo.ts`, so `foo.d.ts` must not yield
        # the shared module name to it, or nothing would own that name.
        _write(tmp_path / "src" / "foo.ts", "export const a = 1;\n")
        declaration = _write(tmp_path / "types" / "foo.d.ts", "export const a: 1;\n")
        _link(tmp_path / "types" / "foo.ts", "../src/foo.ts")
        assert not has_implementation_sibling(declaration, tmp_path)

    def test_a_real_implementation_still_takes_the_declarations_name(
        self, tmp_path: Path
    ) -> None:
        # Negative.
        declaration = _write(tmp_path / "foo.d.ts", "export const a: 1;\n")
        _write(tmp_path / "foo.ts", "export const a = 1;\n")
        assert has_implementation_sibling(declaration, tmp_path)

    def test_a_linked_file_of_another_language_is_no_sibling(
        self, tmp_path: Path
    ) -> None:
        module = _write(tmp_path / "pkg" / "m.py", "X = 1\n")
        _write(tmp_path / "web" / "m.js", "export const x = 1;\n")
        _link(tmp_path / "pkg" / "m.js", "../web/m.js")
        assert not has_other_language_sibling(module, tmp_path)


_OPENAPI = {
    "openapi": "3.0.0",
    "paths": {"/things": {"get": {"operationId": "listThings"}}},
}


class TestWatcherReplacement:
    def test_replacing_an_indexed_file_with_a_link_drops_its_definitions(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        _write(repo / "core.py", "def helper():\n    return 1\n")
        _write(repo / "old.py", "def old_fn():\n    return 2\n")
        store = _StatefulIngestor()
        updater = _stateful_updater(repo, store)
        updater.run(force=True)
        assert "repo.old.old_fn" in _function_qns(store)

        (repo / "old.py").unlink()
        _link(repo / "old.py", "core.py")
        handler = CodeChangeEventHandler(updater, debounce_seconds=0)
        handler.dispatch(FileDeletedEvent(str(repo / "old.py")))
        handler.dispatch(FileCreatedEvent(str(repo / "old.py")))

        assert _function_qns(store) == {"repo.core.helper"}
        cache = json.loads(
            state_file(repo, cs.HASH_CACHE_FILENAME).read_text(encoding="utf-8")
        )
        assert sorted(cache) == ["core.py"]

    def test_reingesting_a_file_replaced_by_a_link_deletes_its_own_file(
        self, tmp_path: Path
    ) -> None:
        # The deletion keys on the replaced entry, as the File was indexed,
        # not on the link's target (#2451 review).
        repo, store, updater = _remove_indexed_file(tmp_path, replace_with_link=True)
        updater.reingest((), deleted=(repo / "old.py",))
        assert _file_paths(store) == {"core.py"}

    def test_reingesting_a_deleted_file_deletes_its_own_file(
        self, tmp_path: Path
    ) -> None:
        # Negative: with no link in the way the same File node goes.
        repo, store, updater = _remove_indexed_file(tmp_path, replace_with_link=False)
        updater.reingest((), deleted=(repo / "old.py",))
        assert _file_paths(store) == {"core.py"}


class TestOtherWalks:
    def test_a_linked_contract_is_read_once(self, tmp_path: Path) -> None:
        _write(tmp_path / "api" / "things.json", json.dumps(_OPENAPI))
        _link(tmp_path / "api" / "alias.json", "things.json")
        operations = discover_contract_operations(tmp_path)
        assert [op.contract for op in operations] == ["api/things"]

    def test_structural_search_reports_a_linked_file_once(self, tmp_path: Path) -> None:
        _write(tmp_path / "core.py", "API_TOKEN = 1\n")
        _link(tmp_path / "alias.py", "core.py")
        matches = AstGrepService(project_root=str(tmp_path)).search(
            pattern="API_TOKEN", language="python"
        )
        assert [match["file"] for match in matches] == ["core.py"]

    def test_interactive_setup_offers_no_directory_behind_a_link(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path / "shared" / "node_modules" / "dep" / "index.js", "x\n")
        repo = tmp_path / "repo"
        _write(repo / "node_modules" / "own" / "index.js", "x\n")
        _link(repo / "linked", "../shared", directory=True)
        # Negative half: a real directory is still offered.
        assert detect_excludable_directories(repo) == {"node_modules"}


def _package_repo(root: Path, name: str) -> Path:
    repo = root / name
    _write(repo / "pkg" / "__init__.py", "")
    _write(repo / "pkg" / "m.py", "X = 1\n")
    return repo


def _sync(repo: Path, store: _StatefulIngestor, project: str, force: bool) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=project,
    ).run(force=force)


def _packages(store: _StatefulIngestor) -> set[str]:
    return {
        str(uid) for label, uid in store.nodes if label == cs.NodeLabel.PACKAGE.value
    }


class TestPackagePruneScope:
    # The orphan prune now deletes a Package by qualified name (the key its
    # upsert MERGEs on), scoped to the project and repository it read the
    # row from, so it never reaches another project's or checkout's package.

    def test_pruning_one_project_leaves_another_projects_package(
        self, tmp_path: Path
    ) -> None:
        store = _StatefulIngestor()
        alpha = _package_repo(tmp_path, "alpha")
        beta = _package_repo(tmp_path, "beta")
        _sync(alpha, store, "alpha", force=True)
        _sync(beta, store, "beta", force=True)
        assert {"alpha.pkg", "beta.pkg"} <= _packages(store)

        # `pkg` is a Folder now, so alpha's Package is a stale orphan.
        (alpha / "pkg" / "__init__.py").unlink()
        _sync(alpha, store, "alpha", force=False)

        assert "alpha.pkg" not in _packages(store)
        assert "beta.pkg" in _packages(store)

    def test_a_shared_project_name_leaves_the_other_checkouts_package(
        self, tmp_path: Path
    ) -> None:
        # Two checkouts given one project name share every package qn, so the
        # upsert puts both on one node; the one that wrote it last owns it.
        store = _StatefulIngestor()
        mine = _package_repo(tmp_path, "mine")
        other = _package_repo(tmp_path, "other")
        _sync(mine, store, "shared", force=True)
        _sync(other, store, "shared", force=True)
        node = store.nodes[(cs.NodeLabel.PACKAGE.value, "shared.pkg")]
        assert node[cs.KEY_ABSOLUTE_PATH] == (other / "pkg").resolve().as_posix()

        (mine / "pkg" / "__init__.py").unlink()
        _sync(mine, store, "shared", force=False)

        assert "shared.pkg" in _packages(store)

    def test_the_package_delete_itself_is_scoped(self, tmp_path: Path) -> None:
        # The delete query refuses on its own, not only through the rows the
        # prune reads: another project's name or another checkout's path is
        # left alone, and the owner's own delete goes through.
        store = _StatefulIngestor()
        other = _package_repo(tmp_path, "other")
        _sync(other, store, "shared", force=True)
        mine = _package_repo(tmp_path, "mine")

        def updater(repo: Path, project: str) -> GraphUpdater:
            parsers, queries = load_parsers()
            return GraphUpdater(
                ingestor=store,
                repo_path=repo,
                parsers=parsers,
                queries=queries,
                project_name=project,
            )

        mine_pkg = (mine / "pkg").resolve().as_posix()
        other_pkg = (other / "pkg").resolve().as_posix()
        updater(mine, "shared")._delete_package(store, "shared.pkg", mine_pkg)
        updater(other, "elsewhere")._delete_package(store, "shared.pkg", other_pkg)
        assert "shared.pkg" in _packages(store)

        updater(other, "shared")._delete_package(store, "shared.pkg", other_pkg)
        assert "shared.pkg" not in _packages(store)

    def test_a_package_behind_an_outside_link_is_pruned(self, tmp_path: Path) -> None:
        # Its absolute path is the link's target, outside the repository, so
        # the prune passed over it before checking whether anything derives it.
        repo = _package_repo(tmp_path, "repo")
        target = tmp_path / "outside" / "lib"
        _write(target / "__init__.py", "")
        _link(repo / "pkg" / "linkdir", "../../outside/lib", directory=True)
        store = _StatefulIngestor()
        _sync(repo, store, "proj", force=True)
        package = cs.NodeLabel.PACKAGE.value
        store.ensure_node_batch(
            package,
            {
                cs.KEY_QUALIFIED_NAME: "proj.pkg.linkdir",
                cs.KEY_NAME: "linkdir",
                cs.KEY_PATH: "pkg/linkdir",
                cs.KEY_ABSOLUTE_PATH: target.resolve().as_posix(),
            },
        )
        store.ensure_relationship_batch(
            (package, cs.KEY_QUALIFIED_NAME, "proj.pkg"),
            cs.RelationshipType.CONTAINS_PACKAGE,
            (package, cs.KEY_QUALIFIED_NAME, "proj.pkg.linkdir"),
        )
        # Negative: an outside path with no link of this repository behind
        # it (the package another checkout under this name wrote) stays.
        store.ensure_node_batch(
            package,
            {
                cs.KEY_QUALIFIED_NAME: "proj.vendor",
                cs.KEY_NAME: "vendor",
                cs.KEY_PATH: "vendor",
                cs.KEY_ABSOLUTE_PATH: (tmp_path / "elsewhere" / "vendor").as_posix(),
            },
        )
        edited = _write(repo / "pkg" / "m.py", "X = 2\n")
        force_mtime_after_cache(repo, edited)

        _sync(repo, store, "proj", force=False)

        packages = _packages(store)
        assert "proj.pkg.linkdir" not in packages
        assert {"proj.pkg", "proj.vendor"} <= packages

    def test_a_link_to_the_other_checkouts_package_leaves_it(
        self, tmp_path: Path
    ) -> None:
        # `mine/pkg` links to `other/pkg`, which the other checkout under the
        # same name indexed: the node at `pkg` holds `other/pkg`, the shape
        # that checkout writes for its own directory, so it is not this
        # repository's link-derived leftover (#2451 review).
        store = _StatefulIngestor()
        other = _package_repo(tmp_path, "other")
        mine = tmp_path / "mine"
        main = _write(mine / "main.py", "X = 1\n")
        _link(mine / "pkg", "../other/pkg", directory=True)
        _sync(mine, store, "shared", force=True)
        _sync(other, store, "shared", force=True)
        node = store.nodes[(cs.NodeLabel.PACKAGE.value, "shared.pkg")]
        assert node[cs.KEY_ABSOLUTE_PATH] == (other / "pkg").resolve().as_posix()
        _write(mine / "main.py", "X = 2\n")
        force_mtime_after_cache(mine, main)

        _sync(mine, store, "shared", force=False)

        assert "shared.pkg" in _packages(store)

    def test_a_link_derived_package_beside_the_other_checkouts_is_pruned(
        self, tmp_path: Path
    ) -> None:
        # Negative: a node an older build derived through this repository's
        # link to the same target still goes, and the other checkout's stays.
        store = _StatefulIngestor()
        other = _package_repo(tmp_path, "other")
        mine = tmp_path / "mine"
        main = _write(mine / "main.py", "X = 1\n")
        _link(mine / "vendored", "../other/pkg", directory=True)
        _sync(mine, store, "shared", force=True)
        _sync(other, store, "shared", force=True)
        store.ensure_node_batch(
            cs.NodeLabel.PACKAGE.value,
            {
                cs.KEY_QUALIFIED_NAME: "shared.vendored",
                cs.KEY_NAME: "vendored",
                cs.KEY_PATH: "vendored",
                cs.KEY_ABSOLUTE_PATH: (other / "pkg").resolve().as_posix(),
            },
        )
        _write(mine / "main.py", "X = 2\n")
        force_mtime_after_cache(mine, main)

        _sync(mine, store, "shared", force=False)

        packages = _packages(store)
        assert "shared.vendored" not in packages
        assert "shared.pkg" in packages


_STUB = "def f() -> int: ...\n"
_IMPL = "def f():\n    return 1\n"


def _implementation(root: Path, rel: str, *, inside: bool) -> str:
    """Write the real implementation; return its path relative to `proj/`.

    Inside the repository it is walked under its own path, and only the
    symlink rule tells the stub not to yield to the link. Outside it,
    `should_skip_path`'s containment check already refused the link.
    """
    if inside:
        _write(root / "proj" / "lib" / rel, _IMPL)
        return f"lib/{rel}"
    _write(root / "vendor" / rel, _IMPL)
    return f"../vendor/{rel}"


def _linked_file(root: Path, *, inside: bool) -> Path:
    target = _implementation(root, "x.py", inside=inside)
    stub = _write(root / "proj" / "x.pyi", _STUB)
    _link(root / "proj" / "x.py", target)
    return stub


def _linked_package_dir(root: Path, *, inside: bool) -> Path:
    target = _implementation(root, "x/__init__.py", inside=inside)
    stub = _write(root / "proj" / "x.pyi", _STUB)
    _link(root / "proj" / "x", Path(target).parent.as_posix(), directory=True)
    return stub


def _linked_package_init(root: Path, *, inside: bool) -> Path:
    # A real `x/` the walk enters, whose `__init__.py` is the link.
    target = _implementation(root, "x.py", inside=inside)
    stub = _write(root / "proj" / "x.pyi", _STUB)
    (root / "proj" / "x").mkdir()
    _link(root / "proj" / "x" / "__init__.py", f"../{target}")
    return stub


def _linked_init_beside_init_stub(root: Path, *, inside: bool) -> Path:
    target = _implementation(root, "x.py", inside=inside)
    stub = _write(root / "proj" / "pkg" / "__init__.pyi", _STUB)
    _link(root / "proj" / "pkg" / "__init__.py", f"../{target}")
    return stub


# (layout builder, the module qn the stub must define)
_LINKED_IMPLEMENTATIONS = [
    pytest.param(_linked_file, "proj.x", id="x.py-is-a-link"),
    pytest.param(_linked_package_dir, "proj.x", id="x-dir-is-a-link"),
    pytest.param(_linked_package_init, "proj.x", id="x/__init__.py-is-a-link"),
    pytest.param(_linked_init_beside_init_stub, "proj.pkg", id="__init__.py-link"),
]
_TARGET_INSIDE = pytest.mark.parametrize(
    "inside", [True, False], ids=["target-inside", "target-outside"]
)


def _module_paths(ingestor: MagicMock) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for node_call in get_nodes(ingestor, cs.NodeLabel.MODULE.value):
        props = node_call[0][1]
        path = props.get(cs.KEY_PATH)
        entry = found.setdefault(props[cs.KEY_QUALIFIED_NAME], set())
        if isinstance(path, str):
            entry.add(path)
    return found


class TestStubBesideALinkedImplementation:
    # A `.pyi` stub yields its module to the `.py` (or package) beside it
    # (issue #2445) only when the walk will index that implementation. A
    # link, or a package behind a linked directory, is never walked, so a
    # stub yielding to one left NOTHING defining the module.

    @_TARGET_INSIDE
    @pytest.mark.parametrize(("build", "module"), _LINKED_IMPLEMENTATIONS)
    def test_the_stub_defines_the_module(
        self,
        tmp_path: Path,
        mock_ingestor: MagicMock,
        build: Callable[..., Path],
        module: str,
        inside: bool,
    ) -> None:
        stub = build(tmp_path, inside=inside)
        repo = tmp_path / "proj"
        create_and_run_updater(repo, mock_ingestor)
        assert _module_paths(mock_ingestor).get(module) == {
            stub.relative_to(repo).as_posix()
        }
        assert f"{module}.f" in get_node_names(mock_ingestor, cs.NodeLabel.FUNCTION)

    @_TARGET_INSIDE
    @pytest.mark.parametrize(("build", "module"), _LINKED_IMPLEMENTATIONS)
    def test_a_linked_implementation_does_not_count(
        self, tmp_path: Path, build: Callable[..., Path], module: str, inside: bool
    ) -> None:
        stub = build(tmp_path, inside=inside)
        assert not python_stub_has_implementation(stub, tmp_path / "proj")

    def test_a_real_implementation_still_counts(self, tmp_path: Path) -> None:
        # Negative: the #2445 yield is unchanged for a file that is walked.
        stub = _write(tmp_path / "x.pyi", _STUB)
        _write(tmp_path / "x.py", _IMPL)
        assert python_stub_has_implementation(stub, tmp_path)

    def test_a_real_package_still_counts(self, tmp_path: Path) -> None:
        # Negative: a package directory that is not a link still wins.
        stub = _write(tmp_path / "x.pyi", _STUB)
        _write(tmp_path / "x" / "__init__.py", _IMPL)
        assert python_stub_has_implementation(stub, tmp_path)

    def test_a_real_init_still_counts(self, tmp_path: Path) -> None:
        # Negative.
        stub = _write(tmp_path / "pkg" / "__init__.pyi", _STUB)
        _write(tmp_path / "pkg" / "__init__.py", _IMPL)
        assert python_stub_has_implementation(stub, tmp_path)

    def test_a_stub_with_no_implementation_does_not_yield(self, tmp_path: Path) -> None:
        # Negative.
        stub = _write(tmp_path / "x.pyi", _STUB)
        assert not python_stub_has_implementation(stub, tmp_path)

    def test_a_repository_reached_through_a_link_keeps_its_implementation(
        self, tmp_path: Path
    ) -> None:
        # Negative: only entries inside the repository are judged, so a
        # `--repo-path` that is itself a link still has a walked `x.py`.
        _write(tmp_path / "repo" / "x.py", _IMPL)
        _write(tmp_path / "repo" / "x.pyi", _STUB)
        entry = tmp_path / "repo-link"
        _link(entry, "repo", directory=True)
        assert python_stub_has_implementation(entry / "x.pyi", entry)

    def test_a_real_implementation_keeps_the_module_in_the_graph(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        # Negative, end to end: the `.py` owns the module, the stub adds none.
        repo = tmp_path / "proj"
        _write(repo / "x.py", _IMPL)
        _write(repo / "x.pyi", _STUB + "def only_in_stub() -> None: ...\n")
        create_and_run_updater(repo, mock_ingestor)
        assert _module_paths(mock_ingestor).get("proj.x") == {"x.py"}
        functions = get_node_names(mock_ingestor, cs.NodeLabel.FUNCTION)
        assert "proj.x.f" in functions
        assert "proj.x.only_in_stub" not in functions


def _stub_whose_implementation_becomes_a_link(
    tmp_path: Path,
) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    """Index a real `x.py` beside `x.pyi`, then replace it with a link."""
    repo = tmp_path / "proj"
    _write(repo / "lib" / "x.py", _IMPL)
    _write(repo / "x.py", "def g():\n    return 2\n")
    _write(repo / "x.pyi", _STUB)
    store = _StatefulIngestor()
    updater = _stateful_updater(repo, store)
    updater.run(force=True)
    assert _stored_module_path(store, "proj.x") == "x.py"
    (repo / "x.py").unlink()
    _link(repo / "x.py", "lib/x.py")
    return repo, store, updater


def _stored_module_path(store: _StatefulIngestor, qn: str) -> object:
    return store.nodes.get((cs.NodeLabel.MODULE.value, qn), {}).get(cs.KEY_PATH)


class TestImplementationBecomesALink:
    # The stub's answer flips when its `x.py` turns into a link, and the
    # update must hand `proj.x` to the stub rather than leave it unowned.

    def test_the_next_sync_hands_the_module_to_the_stub(self, tmp_path: Path) -> None:
        repo, store, _updater = _stub_whose_implementation_becomes_a_link(tmp_path)
        _stateful_updater(repo, store).run()
        assert _stored_module_path(store, "proj.x") == "x.pyi"
        assert _function_qns(store) == {"proj.x.f", "proj.lib.x.f"}

    def test_a_reingest_hands_the_module_to_the_stub(self, tmp_path: Path) -> None:
        repo, store, updater = _stub_whose_implementation_becomes_a_link(tmp_path)
        updater.reingest((), deleted=(repo / "x.py",))
        assert _stored_module_path(store, "proj.x") == "x.pyi"
        assert _function_qns(store) == {"proj.x.f", "proj.lib.x.f"}


class TestReingestSkipsLinks:
    # A scoped re-ingest re-parses files the call did not name: same-stem
    # survivors (issue #1569) and the files under a directory whose
    # package-ness flipped (issue #1798). Both listed the directory and kept
    # whatever `is_file()` accepted, which follows a link, so a re-ingest
    # indexed links the walk leaves out.

    def test_a_package_flip_reparses_no_link(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _write(repo / "pkg" / "core.py", "def helper():\n    return 1\n")
        _link(repo / "pkg" / "alias.py", "core.py")
        store = _StatefulIngestor()
        updater = _stateful_updater(repo, store)
        updater.run(force=True)
        assert _function_qns(store) == {"repo.pkg.core.helper"}

        updater.reingest((_write(repo / "pkg" / "__init__.py", ""),))

        assert _function_qns(store) == {"repo.pkg.core.helper"}
        assert "pkg/alias.py" not in _file_paths(store)
        cache = json.loads(
            state_file(repo, cs.HASH_CACHE_FILENAME).read_text(encoding="utf-8")
        )
        assert "pkg/alias.py" not in cache

    def test_a_same_stem_survivor_is_no_link(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _write(repo / "web" / "m.js", "export function shown() {\n  return 1;\n}\n")
        (repo / "pkg").mkdir()
        _link(repo / "pkg" / "m.js", "../web/m.js")
        store = _StatefulIngestor()
        updater = _stateful_updater(repo, store)
        updater.run(force=True)
        before = _function_qns(store)
        assert not any(qn.startswith("repo.pkg.") for qn in before)

        updater.reingest((_write(repo / "pkg" / "m.py", "def own():\n    return 1\n"),))

        assert _function_qns(store) == before | {"repo.pkg.m.own"}
        assert "pkg/m.js" not in _file_paths(store)

    def test_a_package_flip_still_reparses_a_real_sibling(self, tmp_path: Path) -> None:
        # Negative: the real files under a flipped directory are re-parsed
        # onto the new Package, as before.
        repo = tmp_path / "repo"
        _write(repo / "pkg" / "core.py", "def helper():\n    return 1\n")
        store = _StatefulIngestor()
        updater = _stateful_updater(repo, store)
        updater.run(force=True)

        updater.reingest((_write(repo / "pkg" / "__init__.py", ""),))

        assert "repo.pkg" in _packages(store)
        assert _function_qns(store) == {"repo.pkg.core.helper"}
        contains = {
            (edge[1], edge[4])
            for edge in store.keyed_edges
            if edge[2] == cs.RelationshipType.CONTAINS_MODULE.value
        }
        assert ("repo.pkg", "repo.pkg.core") in contains
