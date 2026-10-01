"""What an index run prints at the default level (issue #2398).

#2353 moved the CLI to INFO, but every discovered function, method, class,
file, package, folder and dependency was still logged at INFO, so a sync of
this repository printed ~25,000 lines, 98% of them one per symbol or file,
and the pass headers and warnings scrolled past unread. Read-only commands
such as `cgr status` and `cgr stats` also printed the ingestor's connect and
flush lifecycle between the lines of their own report.

At INFO a run now shows the passes, the counts and the warnings, and the
output no longer grows with the repository. The per-item lines are still
there at DEBUG.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger
from typer.testing import CliRunner

from codebase_rag import logs as ls
from codebase_rag.cli import app
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.stack.constants import StackState
from codebase_rag.stack.manager import StackStatus
from codebase_rag.tests.conftest import create_and_run_updater
from codebase_rag.types_defs import BatchWrapper, PropertyValue

# Every name the fixtures define carries this marker, so "does an INFO line
# name one of them" is a substring check that cannot miss a name.
_MARK = "quokka"

_INFO = "INFO"
_DEBUG = "DEBUG"


class _Line(NamedTuple):
    level: str
    text: str


class _Repo(NamedTuple):
    root: Path
    symbols: list[str]
    files: list[str]
    packages: list[str]
    folders: list[str]
    dependencies: list[str]


@contextmanager
def _log_lines(level: str) -> Iterator[list[_Line]]:
    lines: list[_Line] = []
    sink = logger.add(
        lambda m: lines.append(_Line(m.record["level"].name, m.record["message"])),
        level=level,
        format="{message}",
    )
    try:
        yield lines
    finally:
        logger.remove(sink)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _python_repo(
    root: Path,
    *,
    packages: int,
    modules: int,
    classes: int,
    methods: int,
    functions: int,
    dependencies: int,
) -> _Repo:
    symbols: list[str] = []
    files: list[str] = []
    package_names: list[str] = []
    folder_names: list[str] = []
    for p in range(packages):
        package = f"{_MARK}_pkg_{p}"
        folder = f"{_MARK}_scripts_{p}"
        package_names.append(package)
        folder_names.append(folder)
        _write(root, f"{package}/__init__.py", "")
        files.append(f"{package}/__init__.py")
        for m in range(modules):
            body: list[str] = []
            for c in range(classes):
                cls = f"Quokka{p}x{m}x{c}"
                symbols.append(cls)
                body.append(f"class {cls}:")
                for k in range(methods):
                    method = f"{_MARK}_method_{p}_{m}_{c}_{k}"
                    symbols.append(method)
                    body.append(f"    def {method}(self):\n        return {k}")
                body.append("")
            for f in range(functions):
                func = f"{_MARK}_fn_{p}_{m}_{f}"
                symbols.append(func)
                body.append(f"def {func}():\n    return {f}\n")
            rel = f"{package}/{_MARK}_mod_{m}.py"
            _write(root, rel, "\n".join(body))
            files.append(rel)
        tool = f"{folder}/{_MARK}_tool_{p}.py"
        func = f"{_MARK}_tool_main_{p}"
        symbols.append(func)
        _write(root, tool, f"def {func}():\n    return 0\n")
        files.append(tool)
    dependency_names = [f"{_MARK}dep{d}" for d in range(dependencies)]
    _write(root, "requirements.txt", "".join(f"{d}>=1.0\n" for d in dependency_names))
    files.append("requirements.txt")
    return _Repo(
        root=root,
        symbols=symbols,
        files=files,
        packages=package_names,
        folders=folder_names,
        dependencies=dependency_names,
    )


def _polyglot_repo(root: Path) -> _Repo:
    """A Python repo plus one file per class-like construct in JS, TS, Rust, C++."""
    repo = _python_repo(
        root, packages=2, modules=2, classes=2, methods=2, functions=2, dependencies=2
    )
    extra_files = {
        "web/quokka_api.js": (
            "function QuokkaClient(base) { this.base = base; }\n"
            "QuokkaClient.prototype.quokkaGet = function (p) { return p; };\n"
            "const quokkaUtils = {\n"
            "  quokkaJoin(a, b) { return a + b; },\n"
            "};\n"
            "exports.quokkaExported = function () { return quokkaUtils; };\n"
        ),
        "web/quokka_types.ts": (
            "export interface QuokkaItem { id: number }\n"
            "export enum QuokkaStatus { Open, Closed }\n"
            "export type QuokkaId = number | string;\n"
            "export class QuokkaStore {\n"
            "  quokkaAdd(i: QuokkaItem): void {}\n"
            "}\n"
        ),
        "native/quokka_lib.rs": (
            "pub struct QuokkaPoint { x: i32 }\n"
            "pub enum QuokkaShape { Circle }\n"
            "pub trait QuokkaArea { fn quokka_area(&self) -> f64; }\n"
            "impl QuokkaPoint { pub fn quokka_norm(&self) -> i32 { self.x } }\n"
            "mod quokka_inline { pub fn quokka_inner() {} }\n"
        ),
        "native/quokka_geo.cpp": (
            "struct QuokkaVec { float x; };\n"
            "union QuokkaBits { int i; float f; };\n"
            "class QuokkaShapeCpp { public: int quokka_sides() { return 0; } };\n"
        ),
    }
    for rel, text in extra_files.items():
        _write(root, rel, text)
    _write(
        root,
        "web/package.json",
        '{"name": "quokka-web", "dependencies": {"quokkareact": "^18.0.0"}}\n',
    )
    return repo._replace(
        files=[*repo.files, *extra_files],
        dependencies=[*repo.dependencies, "quokkareact"],
    )


def _index(repo: Path, level: str) -> list[_Line]:
    with _log_lines(level) as lines:
        create_and_run_updater(repo, MagicMock())
    return lines


def _default_level(lines: list[_Line]) -> list[_Line]:
    # INFO and SUCCESS: the informational lines. A WARNING that names the file
    # it is about is exactly what the default level is for.
    return [line for line in lines if line.level in {_INFO, "SUCCESS"}]


def test_the_default_level_names_no_symbol_file_folder_or_dependency(
    tmp_path: Path,
) -> None:
    _polyglot_repo(tmp_path / "repo")

    lines = _index(tmp_path / "repo", _INFO)

    naming = [line.text for line in _default_level(lines) if _MARK in line.text.lower()]
    assert not naming, f"{len(naming)} INFO lines name one item, e.g. {naming[:5]}"


def test_the_default_output_does_not_grow_with_the_repository(tmp_path: Path) -> None:
    shape = {"packages": 1, "modules": 1, "classes": 1, "methods": 1, "functions": 1}
    # The first run in a process also logs each grammar it loads; take that
    # here so the two measured runs differ only in the size of the repo.
    _python_repo(tmp_path / "warm" / "repo", **shape, dependencies=1)
    _index(tmp_path / "warm" / "repo", _INFO)
    small = _python_repo(tmp_path / "small" / "repo", **shape, dependencies=1)
    large = _python_repo(
        tmp_path / "large" / "repo",
        packages=4,
        modules=4,
        classes=3,
        methods=4,
        functions=4,
        dependencies=6,
    )

    small_lines = _index(small.root, _INFO)
    large_lines = _index(large.root, _INFO)

    assert len(large.symbols) > 20 * len(small.symbols)
    assert len(large_lines) == len(small_lines), (
        f"{len(small_lines)} lines for {len(small.symbols)} symbols, "
        f"{len(large_lines)} for {len(large.symbols)}"
    )


def test_pass_one_says_what_it_found(tmp_path: Path) -> None:
    repo = _python_repo(
        tmp_path / "repo",
        packages=3,
        modules=1,
        classes=1,
        methods=1,
        functions=1,
        dependencies=1,
    )

    lines = _index(repo.root, _INFO)

    summary = ls.STRUCTURE_IDENTIFIED.format(
        packages=len(repo.packages), folders=len(repo.folders)
    )
    assert _Line(_INFO, summary) in lines, [line.text for line in lines]


# Negative tests: what the default level must still show, and what DEBUG must
# still carry.


def test_the_default_level_still_shows_every_pass_and_its_counts(
    tmp_path: Path,
) -> None:
    repo = _python_repo(
        tmp_path / "repo",
        packages=2,
        modules=2,
        classes=2,
        methods=2,
        functions=2,
        dependencies=2,
    )

    texts = [line.text for line in _default_level(_index(repo.root, _INFO))]

    expected = [
        ls.PASS_1_STRUCTURE,
        ls.PASS_2_FILES,
        ls.INCREMENTAL_CHANGED.format(count=len(repo.files)),
        # The registry this counts holds the classes too.
        ls.FOUND_FUNCTIONS.format(count=len(repo.symbols)),
        ls.PASS_3_CALLS,
        ls.CLASS_PASS_4,
        ls.ANALYSIS_COMPLETE,
    ]
    missing = [text for text in expected if text not in texts]
    assert not missing, texts
    # In the order a run makes them, so a user can follow the run.
    positions = [texts.index(text) for text in expected]
    assert positions == sorted(positions), texts


def test_debug_still_lists_every_symbol_file_folder_and_dependency(
    tmp_path: Path,
) -> None:
    repo = _polyglot_repo(tmp_path / "repo")

    debug = [line.text for line in _index(repo.root, _DEBUG)]

    def logged(name: str) -> bool:
        return any(name in text for text in debug)

    detail = [
        *repo.symbols,
        *repo.files,
        *repo.packages,
        *repo.folders,
        *repo.dependencies,
        "QuokkaClient.quokkaGet",
        "quokkaJoin",
        "quokkaExported",
        "QuokkaItem",
        "QuokkaStatus",
        "QuokkaId",
        "QuokkaStore",
        "QuokkaPoint",
        "QuokkaShape",
        "QuokkaArea",
        "quokka_inline",
        "QuokkaVec",
        "QuokkaBits",
        "QuokkaShapeCpp",
    ]
    missing = [name for name in detail if not logged(name)]
    assert not missing, missing


# The ingestor itself: connecting, flushing and disconnecting.


class _FakeCursor:
    def __init__(
        self, columns: tuple[str, ...], rows: list[tuple[object, ...]]
    ) -> None:
        self.description = [SimpleNamespace(name=c) for c in columns] or None
        self._rows = rows

    def execute(
        self,
        query: str,
        params: BatchWrapper | dict[str, PropertyValue] | None = None,
    ) -> None:
        return None

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows

    def close(self) -> None:
        return None


class _FakeConnection:
    """A Memgraph connection whose every query succeeds with `rows`."""

    def __init__(
        self,
        columns: tuple[str, ...] = (),
        rows: list[tuple[object, ...]] | None = None,
    ) -> None:
        self.autocommit = False
        self._columns = columns
        self._rows = rows or []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._columns, self._rows)

    def close(self) -> None:
        return None


@contextmanager
def _memgraph(connection: _FakeConnection) -> Iterator[MagicMock]:
    with patch("codebase_rag.services.graph_service.mgclient") as mgclient:
        mgclient.connect.return_value = connection
        yield mgclient


def test_a_read_only_session_logs_nothing_at_the_default_level() -> None:
    with _memgraph(_FakeConnection()), _log_lines(_INFO) as lines:
        with MemgraphIngestor(host="localhost", port=7687) as ingestor:
            ingestor.fetch_all("MATCH (n) RETURN n")

    assert lines == []


@pytest.mark.parametrize("command", ["status", "stats"])
def test_a_read_only_command_prints_only_its_report(command: str) -> None:
    status = StackStatus(
        state=StackState.RUNNING,
        memgraph_reachable=True,
        qdrant_reachable=True,
        compose_file=Path("/tmp/cgr/docker-compose.yaml"),
        memgraph_endpoint="localhost:7687",
        qdrant_endpoint="localhost:6333",
    )
    # `status` lists the projects the graph holds.
    connection = (
        _FakeConnection(columns=("name", "last_synced_at"), rows=[("alpha", None)])
        if command == "status"
        else _FakeConnection()
    )
    with (
        _memgraph(connection),
        patch("codebase_rag.cli.StackManager") as manager,
        _log_lines(_INFO) as lines,
    ):
        manager.return_value.status.return_value = status
        result = CliRunner().invoke(app, [command])

    assert result.exit_code == 0, result.output
    assert lines == []
    # Negative: the report itself is untouched.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    report = {"status": ["stack:", "syncs:", "alpha"], "stats": ["Total Nodes"]}
    assert all(word in plain for word in report[command]), plain


def test_a_write_session_still_reports_what_it_flushed() -> None:
    connection = _FakeConnection(columns=("created",), rows=[(1,)])
    with _memgraph(connection), _log_lines(_INFO) as lines:
        with MemgraphIngestor(host="localhost", port=7687) as ingestor:
            ingestor.ensure_node_batch("Module", {"qualified_name": "m"})
            ingestor.ensure_node_batch("Class", {"qualified_name": "m.C"})
            ingestor.ensure_relationship_batch(
                ("Module", "qualified_name", "m"),
                "DEFINES",
                ("Class", "qualified_name", "m.C"),
            )

    assert _Line(_INFO, ls.MG_NODES_FLUSHED.format(flushed=2, total=2)) in lines
    assert (
        _Line(_INFO, ls.MG_RELS_FLUSHED.format(total=1, success=1, failed=0)) in lines
    )


def test_debug_still_traces_the_connection_and_flush_lifecycle() -> None:
    with _memgraph(_FakeConnection()), _log_lines(_DEBUG) as lines:
        with MemgraphIngestor(host="db.example", port=7699) as ingestor:
            ingestor.fetch_all("MATCH (n) RETURN n")

    debug = [line.text for line in lines]
    for text in (
        ls.MG_CONNECTING.format(host="db.example", port=7699),
        ls.MG_CONNECTED,
        ls.MG_FLUSH_START,
        ls.MG_FLUSH_COMPLETE,
        ls.MG_DISCONNECTED,
    ):
        assert text in debug, debug


def test_a_failed_connection_still_names_the_server_at_the_default_level() -> None:
    # "Connecting to Memgraph at host:port" was the only place a failed
    # connect named its target; mgclient's own error does not.
    with (
        patch("codebase_rag.services.graph_service.mgclient") as mgclient,
        _log_lines(_INFO) as lines,
    ):
        mgclient.connect.side_effect = ConnectionRefusedError("connection refused")
        with pytest.raises(ConnectionRefusedError):
            with MemgraphIngestor(host="db.example", port=7699):
                pass

    assert any("db.example:7699" in line.text for line in lines), lines
