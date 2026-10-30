"""`PythonFileIngest`: the class names a file reserves before its functions
register, and the `@overload` stubs that fold into their implementation."""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.python.file_ingest import PythonFileIngest
from codebase_rag.tests.collaborator_support import index, nodes_of, parse
from evals.recorded_graph import RecordingIngestor

_PY = cs.SupportedLanguage.PYTHON


def test_the_overload_stubs_fold_and_the_implementation_does_not(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    updater = index(temp_repo / "proj", mock_ingestor, {"a.py": "x = 1\n"})
    ingest = updater.factory.definition_processor._for(_PY)
    assert isinstance(ingest, PythonFileIngest)
    root = parse(
        _PY,
        "from typing import overload\n\n"
        "@overload\ndef f(x: int) -> int: ...\n"
        "@overload\ndef f(x: str) -> str: ...\n"
        "def f(x):\n    return x\n",
    )
    _, queries = load_parsers()
    folded = ingest.folded_overloads(root, _PY, queries, None)
    first_stub, second_stub, implementation = nodes_of(
        root, cs.TS_PY_FUNCTION_DEFINITION
    )
    assert folded is not None
    assert implementation.start_byte not in folded
    assert {first_stub.start_byte, second_stub.start_byte} <= folded


def test_a_file_without_overloads_folds_nothing(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    updater = index(temp_repo / "proj", mock_ingestor, {"a.py": "x = 1\n"})
    ingest = updater.factory.definition_processor._for(_PY)
    _, queries = load_parsers()
    folded = ingest.folded_overloads(
        parse(_PY, "def f(x):\n    return x\n"), _PY, queries, None
    )
    assert not folded


def test_a_class_reserves_its_name_ahead_of_a_later_same_named_function(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    updater = index(
        temp_repo / "proj",
        mock_ingestor,
        # Functions register before classes; the class, written first,
        # still keeps the plain name.
        {"mod.py": "class Thing:\n    pass\n\ndef Thing():\n    return 1\n"},
    )
    registry = updater.factory.definition_processor.function_registry
    assert registry.get("proj.mod.Thing") == cs.NodeLabel.CLASS
