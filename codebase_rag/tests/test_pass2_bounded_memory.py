"""Pass 2 memory is bounded by the AST cache, not by the repository's size.

Two structures held EVERY file's tree-sitter tree for the whole pass,
outside `BoundedASTCache`: the up-front pre-parse dict, read with `.get` and
never emptied, and the definition processor's per-file captures cache, whose
`Node` lists pin their whole tree after the AST cache evicts it. A 20k-file
repository (microsoft/vscode) was OOM-killed at 11.5 GB in Pass 2
(issue #2926).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from tree_sitter import Parser, Tree

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.ast_cache import BoundedASTCache
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import LanguageQueries

_Parsers = tuple[
    Mapping[cs.SupportedLanguage, Parser],
    Mapping[cs.SupportedLanguage, LanguageQueries],
]

_FILES = 6
_BOUND = 2


def _source(i: int) -> str:
    # Each module calls the previous one's function, so Pass 3 has cross-file
    # work to redo for every evicted file.
    prev = (
        f"from mod{i - 1} import fn{i - 1}\n\n"
        if i
        else "def base():\n    return 0\n\n"
    )
    body = f"fn{i - 1}()" if i else "base()"
    return f"{prev}def fn{i}():\n    return {body}\n\n\nclass C{i}:\n    def m(self):\n        return fn{i}()\n"


def _write_repo(root: Path) -> Path:
    root.mkdir()
    for i in range(_FILES):
        (root / f"mod{i}.py").write_text(_source(i), encoding="utf-8")
    return root


@pytest.fixture
def python_parsers() -> _Parsers:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.PYTHON not in parsers:
        pytest.skip("python grammar unavailable")
    return parsers, queries


def _updater(
    root: Path,
    parsers: Mapping[cs.SupportedLanguage, Parser],
    queries: Mapping[cs.SupportedLanguage, LanguageQueries],
) -> GraphUpdater:
    return GraphUpdater(
        ingestor=MagicMock(), repo_path=root, parsers=parsers, queries=queries
    )


def test_captures_never_outlive_the_ast_cache(
    tmp_path: Path,
    python_parsers: _Parsers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gu.settings, "CACHE_MAX_ENTRIES", _BOUND)
    updater = _updater(_write_repo(tmp_path / "repo"), *python_parsers)
    seen: list[tuple[int, set[Path]]] = []
    original = updater._process_function_calls

    def snapshot(only: Collection[Path] | None = None) -> None:
        captures = set(updater.factory._func_class_captures_cache)
        seen.append((len(captures), captures - set(updater.ast_cache.cache)))
        original(only)

    monkeypatch.setattr(updater, "_process_function_calls", snapshot)
    updater.run(force=True)

    assert seen, "Pass 3 never ran"
    count, orphaned = seen[0]
    assert count <= _BOUND, count
    assert orphaned == set(), orphaned


def test_pre_parse_retains_at_most_the_cache_bound(
    tmp_path: Path,
    python_parsers: _Parsers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gu.settings, "CACHE_MAX_ENTRIES", _BOUND)
    root = _write_repo(tmp_path / "repo")
    updater = _updater(root, *python_parsers)
    entries = [
        (root / f"mod{i}.py", f"mod{i}.py", True, _source(i).encode())
        for i in range(_FILES)
    ]
    retained = updater._pre_parse_changed_files(entries)
    assert len(retained) == _BOUND, sorted(retained)


def test_files_beyond_the_bound_are_still_parsed_before_any_delete(
    tmp_path: Path,
    python_parsers: _Parsers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Negative: retaining fewer trees must not skip the up-front parse that
    # keeps a parse failure from leaving an emptied graph behind.
    monkeypatch.setattr(gu.settings, "CACHE_MAX_ENTRIES", _BOUND)
    root = _write_repo(tmp_path / "repo")
    updater = _updater(root, *python_parsers)
    entries = [
        (root / f"mod{i}.py", f"mod{i}.py", True, _source(i).encode())
        for i in range(_FILES)
    ]
    real_parse = gu.parse_with_preproc_recovery
    parsed: list[int] = []

    def counting_parse(
        parser: Parser, source_bytes: bytes, language: cs.SupportedLanguage
    ) -> Tree:
        parsed.append(1)
        if len(parsed) == _FILES:
            raise RuntimeError("parse died")
        return real_parse(parser, source_bytes, language)

    with (
        patch.object(gu, "parse_with_preproc_recovery", side_effect=counting_parse),
        pytest.raises(RuntimeError, match="parse died"),
    ):
        updater._pre_parse_changed_files(entries)


def test_eviction_notifies_the_owner() -> None:
    evicted: list[Path] = []
    cache = BoundedASTCache(max_entries=1, max_memory_mb=1, on_evict=evicted.append)
    first, second = Path("a.py"), Path("b.py")
    node = MagicMock(end_byte=1)
    cache[first] = (node, cs.SupportedLanguage.PYTHON)
    cache[second] = (node, cs.SupportedLanguage.PYTHON)
    assert evicted == [first]
    # An explicit delete is the owner's own doing, not an eviction.
    del cache[second]
    assert evicted == [first]


def test_a_bounded_run_builds_the_same_graph(
    tmp_path: Path,
    python_parsers: _Parsers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Negative: evicting captures with their tree only moves work to a
    # recompute; every node and edge is the same as an unbounded run's.
    def rows(bound: int, name: str) -> set[str]:
        monkeypatch.setattr(gu.settings, "CACHE_MAX_ENTRIES", bound)
        ingestor = MagicMock()
        GraphUpdater(
            ingestor=ingestor,
            repo_path=_write_repo(tmp_path / name),
            parsers=python_parsers[0],
            queries=python_parsers[1],
            project_name="repo",
        ).run(force=True)
        calls = [
            *ingestor.ensure_node_batch.call_args_list,
            *ingestor.ensure_relationship_batch.call_args_list,
        ]
        return {repr(c).replace(name, "<root>") for c in calls}

    assert rows(_BOUND, "small") == rows(1000, "large")
