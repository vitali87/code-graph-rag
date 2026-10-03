"""`positional_params` reaches the graph, for arity diagnosis (issue #227).

The extractor is unit-tested in `test_python_positional_parameters.py`; this
file asserts the property survives real ingestion and lands on the node the
diagnosis reads back. A correct extractor that no frontend calls would pass
those tests and leave the feature dead, which is the exact failure this
property exists to fix.

A frontend that does not read its parameters must leave the property ABSENT
rather than empty. Absent means "the kinds were never extracted"; empty
asserts "declares no positional parameters" and would make every correct
callee look like an arity mismatch. The languages that declare optionality in
the signature store their own marked lists (issue #2517).
"""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater, get_nodes


def _props_by_qn(mock_ingestor, label: str) -> dict[str, dict]:
    return {
        call[0][1][cs.KEY_QUALIFIED_NAME]: call[0][1]
        for call in get_nodes(mock_ingestor, label)
    }


def test_python_functions_record_their_positional_parameters(
    temp_repo: Path, mock_ingestor
) -> None:
    pkg = temp_repo / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text(
        "def plain(a, b):\n"
        "    return a\n"
        "\n"
        "def only_kw(*, a):\n"
        "    return a\n"
        "\n"
        "def star_args(a, *rest, b):\n"
        "    return a\n"
        "\n"
        "def sliced(a, /, b):\n"
        "    return a\n"
    )
    create_and_run_updater(temp_repo, mock_ingestor)

    functions = _props_by_qn(mock_ingestor, cs.NodeLabel.FUNCTION)
    declared = {
        qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]: props[cs.KEY_POSITIONAL_PARAMS]
        for qn, props in functions.items()
        if cs.KEY_POSITIONAL_PARAMS in props
    }
    assert declared["plain"] == ["a", "b"]
    assert declared["only_kw"] == []
    assert declared["star_args"] == ["a"]
    assert declared["sliced"] == ["a", "b"]


def test_python_methods_keep_the_receiver(temp_repo: Path, mock_ingestor) -> None:
    """CPython counts the bound `self`, so the stored list must keep it."""
    pkg = temp_repo / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text(
        "class C:\n    def m(self, a):\n        return a\n",
    )
    create_and_run_updater(temp_repo, mock_ingestor)

    methods = _props_by_qn(mock_ingestor, cs.NodeLabel.METHOD)
    stored = [
        props[cs.KEY_POSITIONAL_PARAMS]
        for qn, props in methods.items()
        if qn.endswith(".m")
    ]
    assert stored == [["self", "a"]]


def test_an_unread_frontend_leaves_the_property_absent(
    temp_repo: Path, mock_ingestor
) -> None:
    """Absent, never empty: empty would assert zero positional parameters."""
    (temp_repo / "main.lua").write_text(
        "local function handle(a, b)\n  return a\nend\n",
    )
    (temp_repo / "main.c").write_text("int handle(int a, int b) { return a; }\n")
    create_and_run_updater(temp_repo, mock_ingestor)

    functions = _props_by_qn(mock_ingestor, cs.NodeLabel.FUNCTION)
    assert functions
    for props in functions.values():
        assert cs.KEY_POSITIONAL_PARAMS not in props


def test_a_go_function_records_its_declared_parameters(
    temp_repo: Path, mock_ingestor
) -> None:
    (temp_repo / "main.go").write_text(
        "package main\n\nfunc Handle(a int, rest ...int) int {\n\treturn a\n}\n",
    )
    create_and_run_updater(temp_repo, mock_ingestor)

    (props,) = _props_by_qn(mock_ingestor, cs.NodeLabel.FUNCTION).values()
    assert props[cs.KEY_POSITIONAL_PARAMS] == ["a", "...rest"]
