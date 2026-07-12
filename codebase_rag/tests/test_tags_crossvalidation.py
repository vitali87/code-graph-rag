# (H) Spike for issue #524 (rescoped): tags.scm marks @definition.* sites; we diff
# (H) those against the (name, start_line) pairs cgr emits and flag any it missed.

from pathlib import Path
from unittest.mock import MagicMock

from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import _create_tags_query, load_parsers
from codebase_rag.parsers.tags_crossvalidate import crossvalidate, crossvalidate_calls
from codebase_rag.tests.test_function_ingest import find_node_by_name, parse_code

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


def _make_definition_processor(temp_repo: Path, mock_ingestor: MagicMock):
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor, repo_path=temp_repo, parsers=parsers, queries=queries
    )
    return updater.factory.definition_processor, parsers, queries


def test_definition_crosscheck_warns_on_drift_not_constants(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) Option B: the cross-check warns on genuine function/class drift but ignores
    # (H) module-level constants (cgr does not model them). We hand cgr's captures only
    # (H) f, so the oracle's g is real drift; constant X must NOT be reported.
    dp, parsers, queries = _make_definition_processor(temp_repo, mock_ingestor)
    code = "X = 1\ndef f():\n    pass\n\ndef g():\n    pass\n"
    root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
    f_node = find_node_by_name(root, "f", "function_definition")
    combined = {cs.CAPTURE_FUNCTION: [f_node], cs.CAPTURE_CLASS: []}

    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        dp._crossvalidate_definitions(
            root, cs.SupportedLanguage.PYTHON, queries, combined, "m.py"
        )
    finally:
        logger.remove(sink)

    warned = [m for m in messages if "cross" in m.lower()]
    assert warned, "expected a warning for the missed function g"
    assert "'g'" in warned[0]
    assert "'X'" not in warned[0]


def test_process_file_runs_crosscheck_without_noise(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch
) -> None:
    # (H) process_file must invoke the cross-check, and a clean file (functions plus a
    # (H) module constant) must stay silent: constants filtered, extraction matches.
    dp, _parsers, queries = _make_definition_processor(temp_repo, mock_ingestor)
    calls: list[int] = []
    original = dp._crossvalidate_definitions

    def spy(*a, **k):
        calls.append(1)
        return original(*a, **k)

    monkeypatch.setattr(dp, "_crossvalidate_definitions", spy)
    fp = temp_repo / "m.py"
    fp.write_text("X = 1\ndef f():\n    pass\n")

    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        dp.process_file(fp, cs.SupportedLanguage.PYTHON, queries, {})
    finally:
        logger.remove(sink)

    assert calls, "process_file did not invoke the cross-check"
    assert not [m for m in messages if "cross" in m.lower()]
