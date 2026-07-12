# (H) Spike for issue #524 (rescoped): tags.scm marks @definition.* sites; we diff
# (H) those against the (name, start_line) pairs cgr emits and flag any it missed.

from codebase_rag import constants as cs
from codebase_rag.parser_loader import _create_tags_query, load_parsers
from codebase_rag.parsers.tags_crossvalidate import crossvalidate, crossvalidate_calls
from codebase_rag.tests.test_function_ingest import parse_code

# (H) definitions sit on lines 1 (f), 4 (g), 7 (C), all 1-indexed
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

    # (H) cgr's own extraction dropped `g`, simulating a language_spec.py query gap.
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

    # (H) cgr emitted a phantom definition tags.scm does not see.
    cgr_defs = {("f", 1), ("g", 4), ("C", 7), ("ghost", 99)}

    missed, extra = crossvalidate(root, _tags_query(), cgr_defs)

    assert missed == set()
    assert extra == {("ghost", 99)}


# (H) call sites: g@2, method@3 (attribute callee), h@4 and nested k@4
_PY_CALLS = "def f():\n    g()\n    obj.method()\n    h(k())\n"


def test_crossvalidate_calls_flags_missed_call() -> None:
    parsers, _ = load_parsers()
    root = parse_code(_PY_CALLS, cs.SupportedLanguage.PYTHON, parsers)

    # (H) cgr resolved g() but dropped the obj.method() call site.
    cgr_calls = {("g", 2), ("h", 4), ("k", 4)}

    missed, extra = crossvalidate_calls(root, _tags_query(), cgr_calls)

    assert missed == {("method", 3)}
    assert extra == set()


def test_crossvalidate_calls_parity_reports_nothing() -> None:
    parsers, _ = load_parsers()
    root = parse_code(_PY_CALLS, cs.SupportedLanguage.PYTHON, parsers)

    cgr_calls = {("g", 2), ("method", 3), ("h", 4), ("k", 4)}

    missed, extra = crossvalidate_calls(root, _tags_query(), cgr_calls)

    assert missed == set()
    assert extra == set()


def test_tags_query_uses_community_oracle() -> None:
    # (H) The grammar-shipped community tags.scm captures @definition.constant for
    # (H) module-level assignments; the earlier hand-written oracle did not. This pins
    # (H) that the loader reads the real independent oracle, not a local copy.
    parsers, _ = load_parsers()
    root = parse_code("X = 1\n\ndef f():\n    pass\n", cs.SupportedLanguage.PYTHON, parsers)

    missed, _extra = crossvalidate(root, _tags_query(), {("f", 3)})

    assert ("X", 1) in missed
