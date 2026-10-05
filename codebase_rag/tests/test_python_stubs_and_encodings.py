"""Issue #2445: `.pyi` stubs and PEP 263 source encodings.

Two gaps in how Python source reaches the grammar:

* A `.pyi` stub got a File node and nothing else. For a compiled extension
  (`fastmath/_core.pyi` describing a `.so`) the stub is the only source of its
  definitions, so `from ._core import add` in `fastmath/__init__.py` resolved
  to nothing and every `fastmath.add` call stayed unresolved.
* A PEP 263 declaration (`# -*- coding: latin-1 -*-`) was ignored and the
  bytes were read as UTF-8, so `def café():` was indexed as `caf`.

What each group pins:

* The stub tests are the regression for the first gap. A stub with no
  implementation beside it IS the module, under the qualified name the `.py`
  would have had.
* The sibling tests are the negative side: a stub beside its `.py` (or its
  package directory) must not become a second Module, in either walk order,
  and must not add stub-only definitions to the implementation.
* The encoding tests are the regression for the second gap, including the
  call pass and the snippet reader, which slice the same source by position.
* The encoding negatives hold PEP 263's own limits: an undeclared UTF-8 file
  is untouched, an unknown or non-text codec degrades to the old reading with
  a warning instead of failing the run, a declaration on line 3 or after a
  code line is not a declaration, and only Python sources are transcoded.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_node_names,
    get_nodes,
    get_relationships,
)
from codebase_rag.types_defs import PropertyDict, ResultRow
from codebase_rag.utils.source_encoding import grammar_bytes
from codebase_rag.utils.source_extraction import extract_source_lines
from evals.cgr_graph import _StatefulIngestor

_LATIN1_LEGACY = (
    b"# -*- coding: latin-1 -*-\n"
    b"def caf\xe9():\n"
    b"    return 1\n"
    b"\n"
    b"def order():\n"
    b"    return caf\xe9()\n"
)

_STUB_CORE = (
    b"def add(a: int, b: int) -> int: ...\n"
    b"def mul(a: int, b: int) -> int: ...\n"
    b"class Vector:\n"
    b"    def norm(self) -> float: ...\n"
)


@pytest.fixture
def project(temp_repo: Path) -> Path:
    root = temp_repo / "proj"
    root.mkdir()
    return root


@pytest.fixture
def warnings_logged() -> Iterator[list[str]]:
    captured: list[str] = []
    sink = logger.add(
        lambda m: captured.append(m.record["message"]),
        level="WARNING",
        format="{message}",
    )
    yield captured
    logger.remove(sink)


def _write(root: Path, files: dict[str, bytes]) -> None:
    for rel, payload in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


def _index(root: Path, ingestor: MagicMock, files: dict[str, bytes]) -> None:
    _write(root, files)
    create_and_run_updater(root, ingestor)


def _definitions(ingestor: MagicMock) -> set[str]:
    return (
        get_node_names(ingestor, cs.NodeLabel.FUNCTION.value)
        | get_node_names(ingestor, cs.NodeLabel.METHOD.value)
        | get_node_names(ingestor, cs.NodeLabel.CLASS.value)
    )


def _module_paths(ingestor: MagicMock) -> dict[str, set[str]]:
    """{module qn: every path any write recorded for it}."""
    found: dict[str, set[str]] = {}
    for call in get_nodes(ingestor, cs.NodeLabel.MODULE.value):
        props = call[0][1]
        path = props.get(cs.KEY_PATH)
        entry = found.setdefault(props[cs.KEY_QUALIFIED_NAME], set())
        if isinstance(path, str):
            entry.add(path)
    return found


def _file_paths(ingestor: MagicMock) -> set[str]:
    return {
        call[0][1][cs.KEY_PATH] for call in get_nodes(ingestor, cs.NodeLabel.FILE.value)
    }


def _edges(ingestor: MagicMock, rel: cs.RelationshipType) -> set[tuple[str, str]]:
    return {
        (call.args[0][2], call.args[2][2])
        for call in get_relationships(ingestor, rel.value)
    }


def _function_props(ingestor: MagicMock, qn: str) -> PropertyDict:
    for call in get_nodes(ingestor, cs.NodeLabel.FUNCTION.value):
        if call[0][1][cs.KEY_QUALIFIED_NAME] == qn:
            return call[0][1]
    raise AssertionError(f"no Function node {qn}")


# --- `.pyi` stubs --------------------------------------------------------


def _fastmath(extra: dict[str, bytes] | None = None) -> dict[str, bytes]:
    files = {
        "fastmath/_core.pyi": _STUB_CORE,
        "fastmath/__init__.py": b"from ._core import add, mul\n",
        "main.py": (b"import fastmath\n\ndef run():\n    return fastmath.add(1, 2)\n"),
    }
    files.update(extra or {})
    return files


def test_a_stub_without_implementation_defines_its_module(
    project: Path, mock_ingestor: MagicMock
) -> None:
    _index(project, mock_ingestor, _fastmath())
    definitions = _definitions(mock_ingestor)
    assert {
        "proj.fastmath._core.add",
        "proj.fastmath._core.mul",
        "proj.fastmath._core.Vector",
        "proj.fastmath._core.Vector.norm",
    } <= definitions
    modules = _module_paths(mock_ingestor)
    assert modules.get("proj.fastmath._core") == {"fastmath/_core.pyi"}


def test_the_package_import_resolves_into_the_stub(
    project: Path, mock_ingestor: MagicMock
) -> None:
    _index(project, mock_ingestor, _fastmath())
    assert ("proj.fastmath", "proj.fastmath._core") in _edges(
        mock_ingestor, cs.RelationshipType.IMPORTS
    )


def test_a_call_through_the_reexport_reaches_the_stub_function(
    project: Path, mock_ingestor: MagicMock
) -> None:
    _index(project, mock_ingestor, _fastmath())
    assert ("proj.main.run", "proj.fastmath._core.add") in _edges(
        mock_ingestor, cs.RelationshipType.CALLS
    )


def test_a_package_init_stub_names_its_package(
    project: Path, mock_ingestor: MagicMock
) -> None:
    # A stub-only package: `__init__.pyi` names the package, exactly as
    # `__init__.py` does, never `proj.typed.__init__`.
    _index(project, mock_ingestor, {"typed/__init__.pyi": b"def f() -> int: ...\n"})
    assert "proj.typed.f" in _definitions(mock_ingestor)
    assert not any("__init__" in qn for qn in _module_paths(mock_ingestor))


def test_a_stub_beside_its_implementation_adds_no_second_module(
    project: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: the `.py` stays the module. Without the yield the stub takes a
    # disambiguated `proj.pkg.x.pyi` and a second copy of every definition.
    _index(
        project,
        mock_ingestor,
        {
            "pkg/__init__.py": b"",
            "pkg/x.py": b"def add(a, b):\n    return a + b\n",
            "pkg/x.pyi": (
                b"def add(a: int, b: int) -> int: ...\n"
                b"def only_in_stub() -> None: ...\n"
            ),
        },
    )
    modules = _module_paths(mock_ingestor)
    assert {qn for qn in modules if qn.startswith("proj.pkg.x")} == {"proj.pkg.x"}
    assert modules["proj.pkg.x"] == {"pkg/x.py"}
    definitions = _definitions(mock_ingestor)
    assert "proj.pkg.x.add" in definitions
    assert not {qn for qn in definitions if "only_in_stub" in qn or ".pyi" in qn}
    # The stub is still a file of the repository.
    assert "pkg/x.pyi" in _file_paths(mock_ingestor)


def test_an_init_stub_beside_init_py_adds_no_second_module(
    project: Path, mock_ingestor: MagicMock
) -> None:
    _index(
        project,
        mock_ingestor,
        {
            "pkg/__init__.py": b"def real():\n    return 1\n",
            "pkg/__init__.pyi": b"def real() -> int: ...\ndef ghost() -> int: ...\n",
        },
    )
    modules = _module_paths(mock_ingestor)
    assert {qn for qn in modules if qn.startswith("proj.pkg")} == {"proj.pkg"}
    assert modules["proj.pkg"] == {"pkg/__init__.py"}
    assert "proj.pkg.ghost" not in _definitions(mock_ingestor)


def test_a_stub_beside_its_package_directory_adds_no_second_module(
    project: Path, mock_ingestor: MagicMock
) -> None:
    # `x/__init__.py` is what `import x` loads, so it owns `proj.x`; a stray
    # `x.pyi` next to the directory must not claim or suffix that name.
    _index(
        project,
        mock_ingestor,
        {
            "x/__init__.py": b"def real():\n    return 1\n",
            "x.pyi": b"def ghost() -> int: ...\n",
        },
    )
    modules = _module_paths(mock_ingestor)
    assert {qn for qn in modules if qn.startswith("proj.x")} == {"proj.x"}
    assert modules["proj.x"] == {"x/__init__.py"}
    assert "proj.x.ghost" not in _definitions(mock_ingestor)


def test_an_excluded_implementation_leaves_the_stub_as_the_module(
    project: Path, mock_ingestor: MagicMock
) -> None:
    # The yield is to an implementation that will be INDEXED; one the user
    # excluded is not a node, so the stub is all the module has.
    _write(
        project,
        {
            "pkg/__init__.py": b"",
            "pkg/x.py": b"def add(a, b):\n    return a + b\n",
            "pkg/x.pyi": b"def add(a: int, b: int) -> int: ...\n",
        },
    )
    create_and_run_updater(
        project, mock_ingestor, exclude_paths=frozenset({"pkg/x.py"})
    )
    assert _module_paths(mock_ingestor)["proj.pkg.x"] == {"pkg/x.pyi"}
    assert "proj.pkg.x.add" in _definitions(mock_ingestor)


# --- PEP 263 source encodings ---------------------------------------------


def test_a_latin1_declaration_keeps_the_real_name(
    project: Path, mock_ingestor: MagicMock, warnings_logged: list[str]
) -> None:
    _index(project, mock_ingestor, {"legacy.py": _LATIN1_LEGACY})
    definitions = _definitions(mock_ingestor)
    assert "proj.legacy.café" in definitions
    assert "proj.legacy.caf" not in definitions
    assert not [w for w in warnings_logged if "legacy.py" in w]


def test_the_call_pass_reads_the_same_names(
    project: Path, mock_ingestor: MagicMock
) -> None:
    _index(project, mock_ingestor, {"legacy.py": _LATIN1_LEGACY})
    assert ("proj.legacy.order", "proj.legacy.café") in _edges(
        mock_ingestor, cs.RelationshipType.CALLS
    )


def test_lines_and_snippets_stay_on_the_original_file(
    project: Path, mock_ingestor: MagicMock
) -> None:
    # Re-encoding changes byte offsets within a line but never the line
    # count, so the recorded span still addresses the file on disk, and the
    # snippet reader honours the same declaration when it slices it.
    _index(project, mock_ingestor, {"legacy.py": _LATIN1_LEGACY})
    props = _function_props(mock_ingestor, "proj.legacy.café")
    assert (props[cs.KEY_START_LINE], props[cs.KEY_END_LINE]) == (2, 3)
    snippet = extract_source_lines(project / "legacy.py", 2, 3)
    assert snippet == "def café():\n    return 1"


@pytest.mark.parametrize(
    "header",
    [
        pytest.param(b"#!/usr/bin/env python\n# -*- coding: latin-1 -*-\n", id="L2"),
        pytest.param(b"# vim: set fileencoding=iso-8859-1 :\n", id="vim"),
        pytest.param(b"# coding=cp1252\n", id="equals"),
    ],
)
def test_declaration_spellings_pep_263_accepts(
    project: Path, mock_ingestor: MagicMock, header: bytes
) -> None:
    _index(
        project, mock_ingestor, {"legacy.py": header + b"def caf\xe9():\n    pass\n"}
    )
    assert "proj.legacy.café" in _definitions(mock_ingestor)


def test_a_stub_honours_its_declaration_too(
    project: Path, mock_ingestor: MagicMock
) -> None:
    _index(
        project,
        mock_ingestor,
        {"ext.pyi": b"# -*- coding: latin-1 -*-\ndef caf\xe9() -> int: ...\n"},
    )
    assert "proj.ext.café" in _definitions(mock_ingestor)


def test_an_undeclared_utf8_file_is_unchanged(
    project: Path, mock_ingestor: MagicMock, warnings_logged: list[str]
) -> None:
    # Negative: no declaration means UTF-8, exactly as before.
    source = "def café():\n    return 1\n".encode()
    _index(project, mock_ingestor, {"modern.py": source})
    assert "proj.modern.café" in _definitions(mock_ingestor)
    props = _function_props(mock_ingestor, "proj.modern.café")
    assert (props[cs.KEY_START_LINE], props[cs.KEY_END_LINE]) == (1, 2)
    assert not [w for w in warnings_logged if "modern.py" in w]


def test_a_utf8_bom_is_read_as_utf8(project: Path, mock_ingestor: MagicMock) -> None:
    # Negative: a BOM already means UTF-8; the file indexes as before and its
    # snippet does not carry the BOM into the text.
    source = b"\xef\xbb\xbf" + "def café():\n    return 1\n".encode()
    _index(project, mock_ingestor, {"bom.py": source})
    assert "proj.bom.café" in _definitions(mock_ingestor)
    assert extract_source_lines(project / "bom.py", 1, 2) == "def café():\n    return 1"


def test_a_bom_outranks_a_conflicting_declaration(
    project: Path, mock_ingestor: MagicMock, warnings_logged: list[str]
) -> None:
    # CPython refuses a BOM beside a non-UTF-8 cookie; the bytes after a BOM
    # are UTF-8 whatever the comment says, so they are read that way.
    source = (
        b"\xef\xbb\xbf# -*- coding: latin-1 -*-\n"
        + "def café():\n    return 1\n".encode()
    )
    _index(project, mock_ingestor, {"bom.py": source})
    assert "proj.bom.café" in _definitions(mock_ingestor)
    assert [w for w in warnings_logged if "bom.py" in w and "latin-1" in w]


@pytest.mark.parametrize(
    "codec",
    [
        pytest.param(b"no-such-codec", id="unknown"),
        pytest.param(b"base64", id="not-a-text-encoding"),
        pytest.param(b"utf-16", id="not-ascii-compatible"),
        pytest.param(b"ascii", id="does-not-decode"),
    ],
)
def test_an_unusable_declaration_falls_back_with_a_warning(
    project: Path, mock_ingestor: MagicMock, warnings_logged: list[str], codec: bytes
) -> None:
    # Negative: the run completes, the file keeps its old UTF-8 reading (the
    # ASCII sibling is indexed as always), and the warning names the file and
    # the codec so the reader knows why `café` was not recovered.
    source = (
        b"# -*- coding: " + codec + b" -*-\n"
        b"def caf\xe9():\n    return 1\n"
        b"def plain():\n    return 2\n"
    )
    _index(project, mock_ingestor, {"legacy.py": source})
    definitions = _definitions(mock_ingestor)
    assert "proj.legacy.plain" in definitions
    assert "proj.legacy.café" not in definitions
    assert [w for w in warnings_logged if "legacy.py" in w and codec.decode() in w], (
        warnings_logged
    )


@pytest.mark.parametrize(
    "header",
    [
        pytest.param(b"#!/usr/bin/env python\n#\n# -*- coding: latin-1 -*-\n", id="L3"),
        pytest.param(b"import os\n# -*- coding: latin-1 -*-\n", id="after-code"),
        pytest.param(b'x = "# coding: latin-1"\n', id="inside-a-string"),
    ],
)
def test_a_declaration_pep_263_does_not_recognise_is_ignored(
    project: Path, mock_ingestor: MagicMock, header: bytes
) -> None:
    # Negative: PEP 263 reads only line 1, or line 2 under a comment-or-blank
    # line 1. Anywhere else the text is an ordinary comment and the file is
    # read as UTF-8, exactly as CPython reads it.
    _index(
        project, mock_ingestor, {"legacy.py": header + b"def caf\xe9():\n    pass\n"}
    )
    assert "proj.legacy.café" not in _definitions(mock_ingestor)


def test_non_python_sources_are_not_transcoded(
    project: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: the declaration is a Python rule. A PHP file whose first line
    # happens to read like a cookie is parsed from its bytes as before.
    source = b"# -*- coding: latin-1 -*-\n<?php\nfunction caf\xe9() { return 1; }\n"
    _index(project, mock_ingestor, {"legacy.php": source})
    assert not {qn for qn in _definitions(mock_ingestor) if "café" in qn}


class TestTranscoderContract:
    """The helper's own guarantees, below the indexer."""

    def test_undeclared_source_is_returned_as_is(self) -> None:
        source = "def café():\n    pass\n".encode()
        assert grammar_bytes(source, cs.SupportedLanguage.PYTHON, Path("m.py")) is (
            source
        )

    def test_other_languages_are_returned_as_is(self) -> None:
        for language in (cs.SupportedLanguage.PHP, cs.SupportedLanguage.JS, None):
            assert grammar_bytes(_LATIN1_LEGACY, language, Path("m")) is _LATIN1_LEGACY

    def test_line_count_is_preserved(self) -> None:
        out = grammar_bytes(_LATIN1_LEGACY, cs.SupportedLanguage.PYTHON, Path("m.py"))
        assert out.count(b"\n") == _LATIN1_LEGACY.count(b"\n")
        assert out.decode(cs.ENCODING_UTF8).splitlines()[1] == "def café():"


class TestStubDefinitionsInDeadCode:
    """A stub's definitions are declarations of code that lives elsewhere."""

    @staticmethod
    def _dead(path: str) -> set[str]:
        row: ResultRow = {
            "label": cs.NodeLabel.FUNCTION.value,
            "qualified_name": "proj.fastmath._core.mul",
            "name": "mul",
            "path": path,
            "start_line": 1,
            "end_line": 1,
            "decorators": [],
            "is_exported": False,
            "overrides_external": False,
        }

        class _Store:
            def fetch_all(
                self, query: str, params: dict[str, str] | None = None
            ) -> list[ResultRow]:
                return [row] if query == cq.CYPHER_DEAD_CODE_NODES else []

        found = collect_dead_code(
            _Store(),
            "proj",
            default_dead_code_config(include_tests=True, include_classes=False),
        )
        return {r["qualified_name"] for r in found}

    def test_an_uncalled_stub_function_is_not_reported_dead(self) -> None:
        # Nothing in the repository calls `mul`, and nothing can: its body is
        # the compiled extension's.
        assert "proj.fastmath._core.mul" not in self._dead("fastmath/_core.pyi")

    def test_an_uncalled_source_function_still_is(self) -> None:
        # Negative: the same definition in a `.py` is ordinary dead code.
        assert "proj.fastmath._core.mul" in self._dead("fastmath/_core.py")


class TestStubOwnershipAcrossIncrementalRuns:
    """The stub/implementation choice is a fact about the disk, not the run.

    Adding or deleting the `.py` puts the stub's stem in flux, so the stub
    re-parses and yields or takes the module the way a clean index would.
    The package `x/__init__.py` decides for `x.pyi` just as `x.py` does,
    though its stem is `x/__init__`, not `x`.
    """

    _STUB = b"def add(a: int, b: int) -> int: ...\ndef only_in_stub() -> None: ...\n"
    _IMPL = b"def add(a, b):\n    return a + b\n"

    @staticmethod
    def _updater(store: _StatefulIngestor, root: Path) -> GraphUpdater:
        parsers, queries = load_parsers()
        return GraphUpdater(
            ingestor=store,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name="proj",
        )

    def _sync(
        self, store: _StatefulIngestor, root: Path, changed: str, scoped: bool
    ) -> None:
        # A walk finds the change itself; `reingest` (the watcher, the MCP
        # tool) is named only the changed file and must find the stub.
        if scoped:
            self._updater(store, root).reingest([changed])
        else:
            self._updater(store, root).run(force=False)

    @staticmethod
    def _state(store: _StatefulIngestor) -> tuple[dict[str, str], set[str]]:
        modules = {
            str(uid): str(props.get(cs.KEY_PATH))
            for (label, uid), props in store.nodes.items()
            if label == cs.NodeLabel.MODULE.value
        }
        functions = {
            str(uid)
            for (label, uid) in store.nodes
            if label == cs.NodeLabel.FUNCTION.value
        }
        return modules, functions

    @pytest.mark.parametrize("scoped", [False, True], ids=["run", "reingest"])
    @pytest.mark.parametrize("impl", ["x.py", "x/__init__.py"])
    def test_an_added_implementation_takes_the_module_from_its_stub(
        self, project: Path, impl: str, scoped: bool
    ) -> None:
        store = _StatefulIngestor()
        (project / "x.pyi").write_bytes(self._STUB)
        self._updater(store, project).run(force=True)
        modules, functions = self._state(store)
        assert modules["proj.x"] == "x.pyi"
        assert "proj.x.only_in_stub" in functions

        _write(project, {impl: self._IMPL})
        self._sync(store, project, impl, scoped)
        modules, functions = self._state(store)
        assert {qn: p for qn, p in modules.items() if qn.startswith("proj.x")} == {
            "proj.x": impl
        }
        assert "proj.x.add" in functions
        assert "proj.x.only_in_stub" not in functions

    @pytest.mark.parametrize("scoped", [False, True], ids=["run", "reingest"])
    @pytest.mark.parametrize("impl", ["x.py", "x/__init__.py"])
    def test_a_deleted_implementation_hands_the_module_to_its_stub(
        self, project: Path, impl: str, scoped: bool
    ) -> None:
        store = _StatefulIngestor()
        _write(project, {"x.pyi": self._STUB, impl: self._IMPL})
        self._updater(store, project).run(force=True)
        assert self._state(store)[0]["proj.x"] == impl

        (project / impl).unlink()
        self._sync(store, project, impl, scoped)
        modules, functions = self._state(store)
        assert modules["proj.x"] == "x.pyi"
        assert {"proj.x.add", "proj.x.only_in_stub"} <= functions
