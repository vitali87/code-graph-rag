"""`PythonFunctionIngest`: the `@overload` stubs left to the implementation
they fold into, and the positional parameter names a call is counted
against."""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.language_spec import LANGUAGE_SPECS
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.python.function_ingest import PythonFunctionIngest
from codebase_rag.tests.collaborator_support import index, nodes_of, parse
from codebase_rag.tests.conftest import get_nodes
from evals.recorded_graph import RecordingIngestor

_PY = cs.SupportedLanguage.PYTHON
_OVERLOADED = (
    "from typing import overload\n\n"
    "@overload\ndef f(x: int) -> int: ...\n"
    "@overload\ndef f(x: str) -> str: ...\n"
    "def f(x):\n    return x\n"
)


def test_a_function_starting_at_a_folded_stub_is_skipped() -> None:
    root = parse(_PY, _OVERLOADED)
    stub, _second, implementation = nodes_of(root, cs.TS_PY_FUNCTION_DEFINITION)
    ingest = PythonFunctionIngest({"p.mod": frozenset({stub.start_byte})})
    _, queries = load_parsers()
    spec = LANGUAGE_SPECS[_PY]

    def skips(node, module_qn: str) -> bool:
        return ingest.skips_function(
            node, module_qn, _PY, spec, queries[_PY], Path("mod.py")
        )

    assert skips(stub, "p.mod")
    assert not skips(implementation, "p.mod")
    # The fold is per module.
    assert not skips(stub, "p.other")


def test_positional_params_are_the_ones_cpython_counts() -> None:
    root = parse(_PY, "def f(a, b=1, /, c=2, *args, d, **kw):\n    pass\n")
    (func,) = nodes_of(root, cs.TS_PY_FUNCTION_DEFINITION)
    assert PythonFunctionIngest({}).positional_params(func, _PY) == ["a", "b", "c"]


def test_only_the_implementation_of_an_overload_is_a_function_node(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    index(temp_repo / "proj", mock_ingestor, {"mod.py": _OVERLOADED})
    functions = [
        n.properties
        for n in get_nodes(mock_ingestor, cs.NodeLabel.FUNCTION)
        if n.properties[cs.KEY_QUALIFIED_NAME] == "proj.mod.f"
    ]
    assert [p[cs.KEY_START_LINE] for p in functions] == [7]
