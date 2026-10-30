"""`python_docstring`: the string literal opening a module's or definition's
body, and nothing for a body that opens with anything else."""

from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag.parsers.python.docstring import python_docstring
from codebase_rag.tests.collaborator_support import first, parse


def _root(source: str):
    return parse(cs.SupportedLanguage.PYTHON, source)


def test_a_modules_opening_string_is_its_docstring() -> None:
    assert python_docstring(_root('"""Module doc."""\nx = 1\n')) == "Module doc."


def test_a_functions_and_a_classs_opening_strings_are_theirs() -> None:
    root = _root(
        "def f():\n    '''Does f.'''\n    return 1\n\n"
        'class C:\n    "Holds c."\n    x = 1\n'
    )
    assert python_docstring(first(root, cs.TS_PY_FUNCTION_DEFINITION)) == "Does f."
    assert python_docstring(first(root, cs.TS_PY_CLASS_DEFINITION)) == "Holds c."


def test_a_body_opening_with_anything_else_has_none() -> None:
    root = _root('def f():\n    x = 1\n    """Not first."""\n')
    assert python_docstring(first(root, cs.TS_PY_FUNCTION_DEFINITION)) is None
    assert python_docstring(_root("import os\n'late'\n")) is None
    assert python_docstring(_root("")) is None


def test_a_node_with_no_body_has_none() -> None:
    call = first(_root("f(1)\n"), cs.TS_PY_CALL)
    assert python_docstring(call) is None
