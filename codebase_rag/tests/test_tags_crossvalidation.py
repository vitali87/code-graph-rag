"""Spike: tags.scm as an oracle to cross-validate cgr's own definition extraction.

Issue #524 (rescoped). tags.scm marks `@definition.*` sites; we diff those against
the (name, start_line) pairs cgr already emits and flag any the graph is missing.
"""

from codebase_rag import constants as cs
from codebase_rag.parser_loader import _create_tags_query, load_parsers
from codebase_rag.parsers.tags_crossvalidate import crossvalidate
from codebase_rag.tests.test_function_ingest import parse_code

# def f -> line 1, def g -> line 4, class C -> line 7 (1-indexed)
_PY_CODE = """def f():
    pass

def g():
    pass

class C:
    pass
"""


def _tags_query():
    parsers, _ = load_parsers()
    return _create_tags_query(
        parsers[cs.SupportedLanguage.PYTHON].language, cs.SupportedLanguage.PYTHON
    )


def test_crossvalidate_flags_definition_cgr_missed() -> None:
    parsers, _ = load_parsers()
    root = parse_code(_PY_CODE, cs.SupportedLanguage.PYTHON, parsers)

    # cgr's own extraction dropped `g` (simulating a language_spec.py query gap).
    cgr_defs = {("f", 1), ("C", 7)}

    missed, extra = crossvalidate(root, _tags_query(), cgr_defs)

    assert missed == {("g", 4)}
    assert extra == set()


def test_crossvalidate_parity_reports_nothing() -> None:
    parsers, _ = load_parsers()
    root = parse_code(_PY_CODE, cs.SupportedLanguage.PYTHON, parsers)

    cgr_defs = {("f", 1), ("g", 4), ("C", 7)}

    missed, extra = crossvalidate(root, _tags_query(), cgr_defs)

    assert missed == set()
    assert extra == set()


def test_crossvalidate_flags_cgr_over_capture() -> None:
    parsers, _ = load_parsers()
    root = parse_code(_PY_CODE, cs.SupportedLanguage.PYTHON, parsers)

    # cgr emitted a phantom definition tags.scm does not see.
    cgr_defs = {("f", 1), ("g", 4), ("C", 7), ("ghost", 99)}

    missed, extra = crossvalidate(root, _tags_query(), cgr_defs)

    assert missed == set()
    assert extra == {("ghost", 99)}
