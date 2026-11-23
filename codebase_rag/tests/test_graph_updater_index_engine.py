"""`GraphUpdater` reads `CGR_INDEX_ENGINE` once and routes every file in one place.

Phase 1, step 3 of the Rust rewrite (docs/reports/PHASE1_SEAMS.md, sections 1
and 5). The updater resolves its engine selection when it is made, into
`engines` and `rust_languages`; `_route` decides the tier and language every
file is indexed by; `replica()` builds a fresh updater over the same inputs
for the `both` mode's scratch runs. No language can run on Rust in this build,
so every selection must index exactly what today's indexer does.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType
from unittest.mock import call

import pytest

from codebase_rag import config, index_engine
from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.index_engine import parse_index_engine
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.ast_grep_tier import AstGrepTier, ast_grep_importable
from codebase_rag.parsers.cpp_frontend import cpp_frontend_available
from codebase_rag.parsers.definition_processor import DefinitionProcessor
from codebase_rag.parsers.document_tier import DocumentTier
from codebase_rag.parsers.document_tier import _load_parser as _load_markdown
from codebase_rag.tests.conftest import _MockIngestor, force_mtime_after_cache
from codebase_rag.types_defs import FileRoute, IndexEngineSelection

L = cs.SupportedLanguage
E = cs.IndexEngine
T = cs.FileTier

# Two files per language that define, import and call each other, so every
# pass of the indexer has something to do in each.
LANGUAGE_FIXTURES: dict[cs.SupportedLanguage, dict[str, str]] = {
    L.PYTHON: {
        "pkg/__init__.py": "",
        "pkg/util.py": (
            "class Greeter:\n    def greet(self, name):\n"
            "        return helper(name)\n\n\n"
            "def helper(name):\n    return name.upper()\n"
        ),
        "main.py": (
            "from pkg.util import Greeter, helper\n\n\n"
            "def run():\n    Greeter().greet('x')\n    return helper('y')\n"
        ),
    },
    L.JS: {
        "util.js": (
            "export function helper(x) { return x + 1; }\n"
            "export class Greeter { greet(n) { return helper(n); } }\n"
        ),
        "main.js": (
            'import { helper, Greeter } from "./util.js";\n'
            "export function run() { new Greeter().greet(1); return helper(2); }\n"
        ),
    },
    L.TS: {
        "util.ts": (
            "export function helper(x: number): number { return x + 1; }\n"
            "export class Greeter { greet(n: number): number { return helper(n); } }\n"
        ),
        "main.ts": (
            'import { helper, Greeter } from "./util";\n'
            "export function run(): number {\n"
            "  new Greeter().greet(1);\n  return helper(2);\n}\n"
        ),
    },
    L.TSX: {
        "util.tsx": "export function helper(x: number): number { return x + 1; }\n",
        "app.tsx": (
            'import { helper } from "./util";\n'
            "export function App() { return <div>{helper(1)}</div>; }\n"
        ),
    },
    L.RUST: {
        "src/lib.rs": "mod util;\n\npub fn run() -> i32 {\n    util::helper(1)\n}\n",
        "src/util.rs": (
            "pub struct Greeter;\n\nimpl Greeter {\n"
            "    pub fn greet(&self) -> i32 {\n        helper(2)\n    }\n}\n\n"
            "pub fn helper(x: i32) -> i32 {\n    x + 1\n}\n"
        ),
    },
    L.GO: {
        "util.go": (
            "package main\n\ntype Greeter struct{}\n\n"
            "func (g Greeter) Greet() int { return helper(1) }\n\n"
            "func helper(x int) int { return x + 1 }\n"
        ),
        "main.go": "package main\n\nfunc main() {\n\tGreeter{}.Greet()\n\thelper(2)\n}\n",
    },
    L.SCALA: {
        "Util.scala": (
            "package demo\n\nobject Util {\n  def helper(x: Int): Int = x + 1\n}\n\n"
            "class Greeter {\n  def greet(): Int = Util.helper(1)\n}\n"
        ),
        "Main.scala": (
            "package demo\n\nobject Main {\n"
            "  def run(): Int = new Greeter().greet() + Util.helper(2)\n}\n"
        ),
    },
    L.JAVA: {
        "src/demo/Util.java": (
            "package demo;\n\npublic class Util {\n"
            "    public static int helper(int x) { return x + 1; }\n}\n"
        ),
        "src/demo/Main.java": (
            "package demo;\n\npublic class Main {\n"
            "    public int run() { return Util.helper(2); }\n}\n"
        ),
    },
    L.C: {
        "util.c": "int helper(int x) { return x + 1; }\n",
        "main.c": "int helper(int x);\n\nint main(void) { return helper(2); }\n",
    },
    L.CPP: {
        "util.hpp": (
            "#pragma once\nnamespace demo {\nclass Greeter {\npublic:\n"
            "  int greet();\n};\nint helper(int x);\n}\n"
        ),
        "util.cpp": (
            '#include "util.hpp"\nnamespace demo {\n'
            "int helper(int x) { return x + 1; }\n"
            "int Greeter::greet() { return helper(1); }\n}\n"
        ),
        "main.cpp": (
            '#include "util.hpp"\n'
            "int main() { demo::Greeter g; return g.greet() + demo::helper(2); }\n"
        ),
    },
    L.PHP: {
        "src/Util.php": (
            "<?php\nnamespace Demo;\n\nclass Util {\n"
            "    public static function helper(int $x): int { return $x + 1; }\n}\n"
        ),
        "src/Main.php": (
            "<?php\nnamespace Demo;\n\nuse Demo\\Util;\n\n"
            "function run(): int { return Util::helper(2); }\n"
        ),
    },
    L.LUA: {
        "util.lua": "local M = {}\nfunction M.helper(x) return x + 1 end\nreturn M\n",
        "main.lua": (
            'local util = require("util")\n'
            "local function run() return util.helper(2) end\nreturn run\n"
        ),
    },
    L.CSHARP: {
        "Util.cs": (
            "namespace Demo {\n  public class Util {\n"
            "    public static int Helper(int x) { return x + 1; }\n  }\n}\n"
        ),
        "Program.cs": (
            "namespace Demo {\n  public class Program {\n"
            "    public static int Main() { return Util.Helper(2); }\n  }\n}\n"
        ),
    },
    L.DART: {
        "lib/util.dart": (
            "int helper(int x) => x + 1;\n\nclass Greeter {\n"
            "  int greet() => helper(1);\n}\n"
        ),
        "lib/main.dart": "import 'util.dart';\n\nint run() => Greeter().greet() + helper(2);\n",
    },
    L.SQL: {
        "schema.sql": "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);\n",
        "views.sql": "CREATE VIEW names AS SELECT name FROM users;\n",
    },
    L.JULIA: {
        "src/Util.jl": "module Util\nexport helper\nhelper(x) = x + 1\nend\n",
        "src/Main.jl": 'include("Util.jl")\nusing .Util\nrun() = helper(2)\n',
    },
}

# Nodes every file gets whether or not its language was parsed.
_UNPARSED_LABELS = frozenset(
    {cs.NodeLabel.PROJECT, cs.NodeLabel.FOLDER, cs.NodeLabel.PACKAGE, cs.NodeLabel.FILE}
)
_SYNC_TIME = "<sync time>"


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _family(language: cs.SupportedLanguage) -> list[str]:
    unit = next(unit for unit in cs.INDEX_ENGINE_DEFAULTS if language in unit)
    return sorted(unit)


def _assign(language: cs.SupportedLanguage, engine: cs.IndexEngine) -> str:
    # Names the language's whole family: a value that split one is refused.
    return ",".join(f"{member}={engine}" for member in _family(language))


def _selections(language: cs.SupportedLanguage) -> list[tuple[str, str | None]]:
    """Every form of the value, as (the setting, the parameter): None leaves
    the updater to read the setting."""
    return [
        ("rust", None),
        ("auto", "auto"),
        ("auto", "python"),
        ("auto", "rust"),
        ("auto", _assign(language, E.RUST)),
        ("auto", f"python,{_assign(language, E.RUST)}"),
        ("auto", f"rust,{_assign(language, E.PYTHON)}"),
        ("auto", _assign(language, E.AUTO)),
    ]


def _calls(ingestor: _MockIngestor) -> list:
    # The one call that differs between two runs of the same index: the time
    # the sync completed, which the reference graphs leave out too.
    return [
        call_
        if not (call_.args and isinstance(call_.args[-1], dict))
        or cs.KEY_LAST_SYNCED_AT not in call_.args[-1]
        else getattr(call, call_[0])(
            *call_.args[:-1],
            call_.args[-1] | {cs.KEY_LAST_SYNCED_AT: _SYNC_TIME},
            **call_.kwargs,
        )
        for call_ in ingestor.method_calls
    ]


@pytest.fixture
def parsers_and_queries() -> tuple:
    return load_parsers()


@pytest.fixture
def make_updater(
    parsers_and_queries: tuple, tmp_path: Path
) -> Callable[..., GraphUpdater]:
    parsers, queries = parsers_and_queries
    made = 0

    def make(
        repo: Path, ingestor: _MockIngestor | None = None, **kwargs
    ) -> GraphUpdater:
        nonlocal made
        made += 1
        kwargs.setdefault("state_dir", tmp_path / "state" / str(made))
        kwargs.setdefault("skip_embeddings", True)
        return GraphUpdater(
            ingestor=_MockIngestor() if ingestor is None else ingestor,
            repo_path=repo,
            parsers=parsers,
            queries=queries,
            **kwargs,
        )

    return make


@pytest.fixture
def go_on_rust(monkeypatch: pytest.MonkeyPatch) -> None:
    # A build that can index Go on Rust, as phase 3's will.
    monkeypatch.setattr(
        index_engine, "rust_engine_languages", lambda: frozenset({L.GO})
    )


@pytest.fixture
def setting(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    def set_to(value: str) -> None:
        monkeypatch.setattr(config.settings, "INDEX_ENGINE", value)

    return set_to


class TestTheSelection:
    def test_with_none_given_it_reads_the_setting(
        self,
        tmp_path: Path,
        make_updater: Callable[..., GraphUpdater],
        setting: Callable[[str], None],
        go_on_rust: None,
    ) -> None:
        setting("go=rust")

        updater = make_updater(tmp_path)

        assert updater.index_engine == parse_index_engine("go=rust")
        assert updater.engines[L.GO] == E.RUST
        assert updater.rust_languages == frozenset({L.GO})
        assert {updater.engines[language] for language in L if language != L.GO} == {
            E.PYTHON
        }

    def test_the_parameter_wins_over_the_setting(
        self,
        tmp_path: Path,
        make_updater: Callable[..., GraphUpdater],
        setting: Callable[[str], None],
        go_on_rust: None,
    ) -> None:
        setting("go=rust")
        on_python = make_updater(tmp_path, index_engine=parse_index_engine("python"))
        setting("python")
        on_rust = make_updater(tmp_path, index_engine=parse_index_engine("go=rust"))

        assert on_python.index_engine == parse_index_engine("python")
        assert on_python.engines[L.GO] == E.PYTHON
        assert on_python.rust_languages == frozenset()
        assert on_rust.engines[L.GO] == E.RUST
        assert on_rust.rust_languages == frozenset({L.GO})

    def test_a_given_selection_is_not_checked_against_the_setting(
        self,
        tmp_path: Path,
        make_updater: Callable[..., GraphUpdater],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Given a selection, the updater never asks for the setting's.
        def unread() -> IndexEngineSelection:
            raise AssertionError("the setting was read")

        monkeypatch.setattr(gu, "default_index_engine", unread)

        updater = make_updater(tmp_path, index_engine=parse_index_engine("rust"))

        assert updater.index_engine == parse_index_engine("rust")

    def test_the_setting_is_read_once_when_the_updater_is_made(
        self,
        tmp_path: Path,
        make_updater: Callable[..., GraphUpdater],
        setting: Callable[[str], None],
        monkeypatch: pytest.MonkeyPatch,
        go_on_rust: None,
    ) -> None:
        # The watcher, the MCP server and the tests keep one updater across
        # many runs: a run must never straddle two selections.
        reads: list[IndexEngineSelection] = []
        resolves: list[IndexEngineSelection] = []
        read = gu.default_index_engine
        resolve = gu.resolve_index_engines

        def counted_read() -> IndexEngineSelection:
            reads.append(selection := read())
            return selection

        def counted_resolve(
            selection: IndexEngineSelection,
        ) -> dict[cs.SupportedLanguage, cs.IndexEngine]:
            resolves.append(selection)
            return resolve(selection)

        monkeypatch.setattr(gu, "default_index_engine", counted_read)
        monkeypatch.setattr(gu, "resolve_index_engines", counted_resolve)
        _write(tmp_path / "repo", LANGUAGE_FIXTURES[L.PYTHON])
        setting("go=rust")
        updater = make_updater(tmp_path / "repo")
        setting("python")

        updater.run()
        updater.run(force=True)

        assert reads == [parse_index_engine("go=rust")]
        assert resolves == [parse_index_engine("go=rust")]
        assert updater.engines[L.GO] == E.RUST
        assert updater.rust_languages == frozenset({L.GO})

    @pytest.mark.parametrize(
        "spec", ["auto", "python", "rust", "go=rust", "rust,c=python,cpp=python"]
    )
    def test_every_language_runs_on_python_in_this_build(
        self, tmp_path: Path, make_updater: Callable[..., GraphUpdater], spec: str
    ) -> None:
        updater = make_updater(tmp_path, index_engine=parse_index_engine(spec))

        assert dict(updater.engines) == dict.fromkeys(L, E.PYTHON)
        assert updater.rust_languages == frozenset()

    def test_the_resolved_engines_cannot_be_changed(
        self, tmp_path: Path, make_updater: Callable[..., GraphUpdater]
    ) -> None:
        updater = make_updater(tmp_path)

        assert isinstance(updater.engines, MappingProxyType)
        with pytest.raises(TypeError):
            updater.engines[L.GO] = E.RUST
        assert isinstance(updater.rust_languages, frozenset)


_NOTEBOOK = json.dumps(
    {
        "cells": [
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": ["def summarize():\n", "    return 1\n"],
            }
        ],
        "metadata": {"language_info": {"name": "python"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
)
# One file per tier, with what `_process_single_file` did with each before
# `_route` existed: the first of tree-sitter, a manifest, a notebook, then the
# ast-grep and document tiers, else the File node alone.
TIER_FIXTURE = {
    "app.py": "def main():\n    return 1\n",
    # A stub whose implementation is indexed is left to its File node
    # (issue #2445); one with none is the module's only source.
    "typed.py": "def f():\n    return 1\n",
    "typed.pyi": "def f() -> int: ...\n",
    "compiled.pyi": "def g() -> int: ...\n",
    "requirements.txt": "requests==2.0\n",
    "package.json": '{"dependencies": {"left-pad": "1.0.0"}}\n',
    "App.csproj": '<Project Sdk="Microsoft.NET.Sdk"></Project>\n',
    "analysis.ipynb": _NOTEBOOK,
    "tool.rb": "def greet\n  puts 'hi'\nend\n",
    "README.md": "# Title\n\nSome text.\n",
    "notes.txt": "plain text\n",
}
EXPECTED_ROUTES = {
    "app.py": FileRoute(T.TREE_SITTER, L.PYTHON),
    "typed.py": FileRoute(T.TREE_SITTER, L.PYTHON),
    "typed.pyi": FileRoute(T.NONE),
    "compiled.pyi": FileRoute(T.TREE_SITTER, L.PYTHON),
    "requirements.txt": FileRoute(T.MANIFEST),
    "package.json": FileRoute(T.MANIFEST),
    "App.csproj": FileRoute(T.MANIFEST),
    "analysis.ipynb": FileRoute(T.NOTEBOOK),
    # The ast-grep and document tiers need their extras; without them the
    # file keeps its File node alone, as it always has.
    "tool.rb": FileRoute(T.AST_GREP if ast_grep_importable() else T.NONE),
    "README.md": FileRoute(T.DOCUMENT if _load_markdown() is not None else T.NONE),
    "notes.txt": FileRoute(T.NONE),
}


# What takes a file in each tier: the processors `_TierSpy` watches.
_Tier = GraphUpdater | DefinitionProcessor | AstGrepTier | DocumentTier


class _TierSpy:
    """Which tier actually took each file, seen from the tiers themselves,
    against what `_route` said when `_process_single_file` was handed it."""

    def __init__(self, updater: GraphUpdater, monkeypatch: pytest.MonkeyPatch):
        self.updater = updater
        self.routed: dict[str, FileRoute] = {}
        self.took: dict[str, list[cs.FileTier]] = {}
        dp = updater.factory.definition_processor
        self._spy(monkeypatch, dp, "process_file", T.TREE_SITTER)
        self._spy(monkeypatch, dp, "process_dependencies", T.MANIFEST)
        self._spy(monkeypatch, updater, "_process_notebook", T.NOTEBOOK)
        self._spy(monkeypatch, updater.ast_grep_tier, "process_file", T.AST_GREP)
        self._spy(monkeypatch, updater.document_tier, "process_file", T.DOCUMENT)
        process = updater._process_single_file

        def routed_then_processed(filepath: Path, *args, **kwargs) -> None:
            self.routed[self._key(filepath)] = updater._route(filepath)
            process(filepath, *args, **kwargs)

        monkeypatch.setattr(updater, "_process_single_file", routed_then_processed)

    def _key(self, filepath: Path) -> str:
        return filepath.relative_to(self.updater.repo_path).as_posix()

    def _spy(
        self,
        monkeypatch: pytest.MonkeyPatch,
        owner: _Tier,
        name: str,
        tier: cs.FileTier,
    ) -> None:
        # On the class: the ast-grep tier's instances take no new attributes.
        cls = type(owner)
        original = getattr(cls, name)

        def spied(this: _Tier, filepath: Path, *args, **kwargs):
            if this is owner:
                self.took.setdefault(self._key(filepath), []).append(tier)
            return original(this, filepath, *args, **kwargs)

        monkeypatch.setattr(cls, name, spied)

    def tier_that_took(self, key: str) -> cs.FileTier:
        # A notebook's cells go through the definition processor too, as the
        # Python they make; the notebook tier is the one that took the file.
        took = self.took.get(key, [])
        if took:
            return took[0]
        if key in self.updater._cpp_frontend_covered:
            return T.CPP_FRONTEND
        return T.NONE


def _check_routes(spy: _TierSpy, expected: dict[str, FileRoute]) -> None:
    assert spy.routed == expected
    for key, route in expected.items():
        assert spy.tier_that_took(key) == route.tier, key
        # The grammar every other reader of a file's tree asks for is the
        # route's language.
        path = spy.updater.repo_path / key
        assert spy.updater._tree_sitter_language(path) == route.language, key


@pytest.mark.parametrize("language", list(L))
def test_every_selection_gives_the_same_ingestor_calls(
    language: cs.SupportedLanguage,
    tmp_path: Path,
    make_updater: Callable[..., GraphUpdater],
    setting: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    fingerprint_inputs_once_per_session: None,
) -> None:
    repo = _write(tmp_path / "repo", LANGUAGE_FIXTURES[language])

    def index(setting_value: str, spec: str | None) -> list:
        setting(setting_value)
        ingestor = _MockIngestor()
        updater = make_updater(
            repo,
            ingestor,
            index_engine=None if spec is None else parse_index_engine(spec),
        )
        spy = _TierSpy(updater, monkeypatch)
        updater.run()
        # Every file of the fixture is its language's, parsed by tree-sitter
        # under every selection.
        _check_routes(
            spy,
            dict.fromkeys(
                LANGUAGE_FIXTURES[language], FileRoute(T.TREE_SITTER, language)
            ),
        )
        return _calls(ingestor)

    today = index("auto", None)
    parsed = {
        call_.args[0]
        for call_ in today
        if call_[0] == "ensure_node_batch" and call_.args[0] not in _UNPARSED_LABELS
    }
    assert parsed, f"the {language} fixture was not parsed"

    for setting_value, spec in _selections(language):
        assert index(setting_value, spec) == today, (setting_value, spec)


def test_route_names_the_tier_that_indexes_each_file(
    tmp_path: Path,
    make_updater: Callable[..., GraphUpdater],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater = make_updater(_write(tmp_path / "repo", TIER_FIXTURE))
    spy = _TierSpy(updater, monkeypatch)

    updater.run()

    _check_routes(spy, EXPECTED_ROUTES)


def test_route_agrees_with_the_tier_on_an_incremental_run(
    tmp_path: Path,
    make_updater: Callable[..., GraphUpdater],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _write(tmp_path / "repo", TIER_FIXTURE)
    # The checkout's own state directory, which `force_mtime_after_cache` reads.
    updater = make_updater(repo, state_dir=None)
    updater.run()
    edited = ("app.py", "requirements.txt", "analysis.ipynb", "notes.txt")
    for rel in edited:
        (repo / rel).write_text(TIER_FIXTURE[rel] + "\n", encoding="utf-8")
        force_mtime_after_cache(repo, repo / rel)
    spy = _TierSpy(updater, monkeypatch)

    updater.run()

    assert set(edited) <= set(spy.routed)
    _check_routes(spy, {rel: EXPECTED_ROUTES[rel] for rel in spy.routed})


def test_route_agrees_with_the_tier_on_a_reingest(
    tmp_path: Path,
    make_updater: Callable[..., GraphUpdater],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _write(tmp_path / "repo", TIER_FIXTURE)
    updater = make_updater(repo)
    updater.run()
    spy = _TierSpy(updater, monkeypatch)

    updater.reingest([repo / rel for rel in TIER_FIXTURE])

    _check_routes(spy, EXPECTED_ROUTES)


_WIDGET_H = (
    "#define WIDGET_API\nnamespace ui {\n"
    "struct WIDGET_API Widget {\n  void show();\n};\n}\n"
)
_WIDGET_CPP = '#include "widget.h"\nnamespace ui {\nvoid Widget::show() {}\n}\n'


@pytest.mark.skipif(not cpp_frontend_available(), reason="libclang not available")
def test_route_sends_what_libclang_emitted_to_the_cpp_frontend(
    tmp_path: Path,
    make_updater: Callable[..., GraphUpdater],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _write(
        tmp_path / "repo",
        {"widget.h": _WIDGET_H, "widget.cpp": _WIDGET_CPP, "app.py": "x = 1\n"},
    )
    (repo / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(repo),
                    "arguments": ["c++", "-std=c++17", str(repo / "widget.cpp")],
                    "file": str(repo / "widget.cpp"),
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gu.settings, "CPP_FRONTEND", cs.CppFrontend.LIBCLANG)
    updater = make_updater(repo)
    spy = _TierSpy(updater, monkeypatch)

    updater.run()

    assert "widget.cpp" in updater._cpp_frontend_covered
    # The frontend's files keep the grammar's language: the interface
    # fingerprint and the flow functions still read them with it.
    _check_routes(
        spy,
        {
            key: FileRoute(T.CPP_FRONTEND, L.CPP)
            if key in updater._cpp_frontend_covered
            else FileRoute(T.TREE_SITTER, L.CPP)
            for key in ("widget.h", "widget.cpp")
        }
        | {
            "app.py": FileRoute(T.TREE_SITTER, L.PYTHON),
            "compile_commands.json": FileRoute(T.NONE),
        },
    )


def _files_under(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class TestReplica:
    # How each of GraphUpdater's inputs is read back from an updater: a new
    # input fails `test_it_copies_every_input` until `replica()` handles it.
    READ_BACK = {
        "ingestor": "ingestor",
        "repo_path": "repo_path",
        "parsers": "parsers",
        "queries": "queries",
        "unignore_paths": "_configured_unignore_paths",
        "exclude_paths": "exclude_paths",
        "project_name": "project_name",
        "capture": "capture",
        "skip_embeddings": "skip_embeddings",
        "project_named": "project_named",
        "state_dir": "state_dir",
        "index_engine": "index_engine",
    }
    # What a replica takes from its caller instead: its own graph and sync
    # state, and no embeddings (docs/reports/PHASE1_SEAMS.md, section 2).
    REPLACED = frozenset({"ingestor", "state_dir", "skip_embeddings"})

    @pytest.fixture
    def original(
        self, tmp_path: Path, make_updater: Callable[..., GraphUpdater]
    ) -> GraphUpdater:
        # Every input away from its default, so a dropped one shows.
        return make_updater(
            _write(tmp_path / "repo", LANGUAGE_FIXTURES[L.PYTHON]),
            unignore_paths=frozenset({"vendor"}),
            exclude_paths=frozenset({"build"}),
            project_name="named",
            capture=resolve_capture(["io"]),
            skip_embeddings=False,
            project_named=False,
            state_dir=tmp_path / "original-state",
            index_engine=parse_index_engine("python,go=rust"),
        )

    def test_the_table_names_every_input(self) -> None:
        inputs = set(inspect.signature(GraphUpdater.__init__).parameters) - {"self"}

        assert inputs == set(self.READ_BACK)

    def test_it_copies_every_input(
        self, original: GraphUpdater, tmp_path: Path
    ) -> None:
        ingestor = _MockIngestor()

        replica = original.replica(ingestor, state_dir=tmp_path / "replica-state")

        for name, attribute in self.READ_BACK.items():
            if name not in self.REPLACED:
                assert getattr(replica, attribute) == getattr(original, attribute), name
        assert replica.parsers is original.parsers
        assert replica.queries is original.queries
        assert replica.ingestor is ingestor
        assert replica.state_dir == tmp_path / "replica-state"
        assert replica.skip_embeddings is True
        assert replica.engines == original.engines
        assert replica.rust_languages == original.rust_languages

    def test_it_runs_on_the_selection_it_is_given(
        self, original: GraphUpdater, tmp_path: Path, go_on_rust: None
    ) -> None:
        replica = original.replica(
            _MockIngestor(),
            state_dir=tmp_path / "replica-state",
            index_engine=parse_index_engine("python"),
        )

        assert replica.index_engine == parse_index_engine("python")
        assert replica.rust_languages == frozenset()
        assert original.index_engine == parse_index_engine("python,go=rust")

    def test_it_does_not_read_the_setting(
        self,
        original: GraphUpdater,
        tmp_path: Path,
        setting: Callable[[str], None],
    ) -> None:
        setting("rust")

        replica = original.replica(
            _MockIngestor(), state_dir=tmp_path / "replica-state"
        )

        assert replica.index_engine == original.index_engine

    def test_a_single_file_updater_replicates_to_the_same_file(
        self, tmp_path: Path, make_updater: Callable[..., GraphUpdater]
    ) -> None:
        repo = _write(tmp_path / "repo", LANGUAGE_FIXTURES[L.PYTHON])
        original = make_updater(repo / "main.py")

        replica = original.replica(
            _MockIngestor(), state_dir=tmp_path / "replica-state"
        )

        assert (
            replica._single_file
            == original._single_file
            == (repo / "main.py").resolve()
        )
        assert replica.repo_path == original.repo_path

    def test_it_indexes_what_its_original_does_and_keeps_its_own_state(
        self,
        tmp_path: Path,
        make_updater: Callable[..., GraphUpdater],
        fingerprint_inputs_once_per_session: None,
    ) -> None:
        repo = _write(tmp_path / "repo", LANGUAGE_FIXTURES[L.PYTHON])
        original_ingestor = _MockIngestor()
        original = make_updater(
            repo, original_ingestor, state_dir=tmp_path / "original-state"
        )
        # Both full builds, as the `both` mode's scratch runs are.
        original.run(force=True)
        replica_ingestor = _MockIngestor()
        replica = original.replica(
            replica_ingestor, state_dir=tmp_path / "replica-state"
        )
        state_before = _files_under(tmp_path / "original-state")

        replica.run(force=True)

        assert _calls(replica_ingestor) == _calls(original_ingestor)
        assert _files_under(tmp_path / "replica-state")
        assert _files_under(tmp_path / "original-state") == state_before
