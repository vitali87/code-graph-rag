# Issue #68: a repo in one language paid for all 14 grammars anyway --
# every load_parsers() call imported every grammar module and compiled
# every language's query set up front (and re-compiled them on EVERY
# call). Grammars must load on first use per language, once per process.
from __future__ import annotations

import threading
from collections.abc import Callable

import pytest
from tree_sitter import Parser

from codebase_rag import constants as cs
from codebase_rag.parser_loader import (
    COMBINED_FUNC_CLASS_QUERIES,
    _reset_parser_cache,
    load_parsers,
)

THREADS = 6
THREAD_DEADLINE_S = 60


def test_load_parsers_defers_grammar_work() -> None:
    _reset_parser_cache()
    saved = dict(COMBINED_FUNC_CLASS_QUERIES)
    COMBINED_FUNC_CLASS_QUERIES.clear()
    try:
        parsers, queries = load_parsers()
        # nothing is compiled until a language is actually used
        assert cs.SupportedLanguage.PYTHON not in COMBINED_FUNC_CLASS_QUERIES
        assert cs.SupportedLanguage.RUST not in COMBINED_FUNC_CLASS_QUERIES

        lang_queries = queries[cs.SupportedLanguage.PYTHON]
        assert lang_queries[cs.KEY_PARSER] is parsers[cs.SupportedLanguage.PYTHON]
        assert COMBINED_FUNC_CLASS_QUERIES.get(cs.SupportedLanguage.PYTHON) is not None
        # touching python must not have loaded rust
        assert cs.SupportedLanguage.RUST not in COMBINED_FUNC_CLASS_QUERIES
    finally:
        _reset_parser_cache()
        COMBINED_FUNC_CLASS_QUERIES.clear()
        COMBINED_FUNC_CLASS_QUERIES.update(saved)


def test_membership_check_loads_on_demand() -> None:
    _reset_parser_cache()
    saved = dict(COMBINED_FUNC_CLASS_QUERIES)
    COMBINED_FUNC_CLASS_QUERIES.clear()
    try:
        parsers, _ = load_parsers()
        # conftest-style availability probe (`lang in parsers`) must load
        # that one language and answer truthfully
        if cs.SupportedLanguage.RUST not in parsers:
            pytest.skip("rust parser not available")
        assert COMBINED_FUNC_CLASS_QUERIES.get(cs.SupportedLanguage.RUST) is not None
        assert cs.SupportedLanguage.PYTHON not in COMBINED_FUNC_CLASS_QUERIES
    finally:
        _reset_parser_cache()
        COMBINED_FUNC_CLASS_QUERIES.clear()
        COMBINED_FUNC_CLASS_QUERIES.update(saved)


def test_loaded_grammars_are_shared_across_calls() -> None:
    # parsers must be process-cached: a second load_parsers() reuses the
    # same Parser objects instead of recompiling every query set
    first_parsers, _ = load_parsers()
    first = first_parsers[cs.SupportedLanguage.PYTHON]
    second_parsers, _ = load_parsers()
    assert second_parsers[cs.SupportedLanguage.PYTHON] is first


def test_unknown_language_stays_absent() -> None:
    parsers, queries = load_parsers()
    assert "cobol" not in parsers
    assert parsers.get("cobol") is None
    assert queries.get("cobol") is None


def test_concurrent_first_load_is_thread_safe(monkeypatch) -> None:
    # An MCP server can probe the same language from several request
    # threads at once; the mid-load window (attempted but not yet loaded)
    # must not make a later thread see the language as unavailable
    # (PR #802 review).
    import threading
    import time

    from codebase_rag import parser_loader
    from codebase_rag.language_spec import LanguageSpec
    from codebase_rag.types_defs import LanguageQueries

    _reset_parser_cache()
    saved = dict(COMBINED_FUNC_CLASS_QUERIES)
    COMBINED_FUNC_CLASS_QUERIES.clear()
    real = parser_loader._process_language

    def slow_process(
        lang: cs.SupportedLanguage,
        spec: LanguageSpec,
        parsers: dict[cs.SupportedLanguage, Parser],
        queries: dict[cs.SupportedLanguage, LanguageQueries],
    ) -> bool:
        time.sleep(0.05)
        return real(lang, spec, parsers, queries)

    monkeypatch.setattr(parser_loader, "_process_language", slow_process)
    try:
        parsers, _ = load_parsers()
        results: list[bool] = []

        def probe() -> None:
            results.append(cs.SupportedLanguage.PYTHON in parsers)

        threads = [threading.Thread(target=probe) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results == [True] * 8, results
    finally:
        _reset_parser_cache()
        COMBINED_FUNC_CLASS_QUERIES.clear()
        COMBINED_FUNC_CLASS_QUERIES.update(saved)


def test_mid_load_window_never_exposes_a_partial_language() -> None:
    # _process_language writes the parser BEFORE the queries; the ensure
    # fast path must not treat a present parser as a completed load, or a
    # concurrent queries[lang] in that window raises KeyError
    # (PR #802 review round 3).
    from unittest.mock import MagicMock

    from codebase_rag import parser_loader

    store = parser_loader._LazyGrammarStore()
    store._parser_data[cs.SupportedLanguage.PYTHON] = MagicMock()
    assert cs.SupportedLanguage.PYTHON in store.queries
    assert store.queries[cs.SupportedLanguage.PYTHON] is not None


def test_failed_load_leaves_no_orphan_parser(monkeypatch) -> None:
    # a query-compilation failure after the parser insert must not leave a
    # parser entry without its queries twin
    from codebase_rag import parser_loader

    store = parser_loader._LazyGrammarStore()

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("query compile failed")

    monkeypatch.setattr(parser_loader, "_create_language_queries", boom)
    assert cs.SupportedLanguage.PYTHON not in store.parsers
    assert store.parsers.get(cs.SupportedLanguage.PYTHON) is None


def test_full_views_still_cover_every_available_language() -> None:
    # a consumer that iterates the mapping gets the complete availability
    # picture, not just what happens to be loaded already
    parsers, queries = load_parsers()
    assert cs.SupportedLanguage.PYTHON in set(parsers)
    assert len(parsers) == len(queries)
    assert set(parsers.keys()) == set(queries.keys())


def _in_thread[T](work: Callable[[], T]) -> T:
    out: list[T] = []
    thread = threading.Thread(target=lambda: out.append(work()))
    thread.start()
    thread.join()
    return out[0]


def test_each_thread_gets_its_own_parser() -> None:
    # py-tree-sitter 0.27 releases the interpreter lock while it parses a
    # large buffer and refuses a second parse on a parser already in use, so
    # one process-wide parser shared by two MCP worker threads failed one of
    # them. Each thread gets its own, and the query set it reads names it.
    parsers, queries = load_parsers()
    here = parsers[cs.SupportedLanguage.PYTHON]

    def other() -> tuple[Parser, Parser]:
        theirs, their_queries = load_parsers()
        return (
            theirs[cs.SupportedLanguage.PYTHON],
            their_queries[cs.SupportedLanguage.PYTHON][cs.KEY_PARSER],
        )

    there, their_query_parser = _in_thread(other)

    assert there is not here
    assert their_query_parser is there
    assert queries[cs.SupportedLanguage.PYTHON][cs.KEY_PARSER] is here
    assert parsers[cs.SupportedLanguage.PYTHON] is here


def test_threads_parse_large_sources_at_once() -> None:
    # Large enough that 0.27 parses with the interpreter lock released
    # (PARSE_RELEASE_GIL_MIN_BYTES, 16 KiB), so the parses overlap.
    source = b"".join(
        f"def f{i}(a, b):\n    return [a * b for _ in range({i})]\n\n\n".encode()
        for i in range(20_000)
    )
    # A worker that fails before the barrier breaks it, so the others stop
    # waiting, and every wait and join is bounded: a failure is reported,
    # never a hung run.
    start = threading.Barrier(THREADS, timeout=THREAD_DEADLINE_S)
    failures: list[BaseException] = []
    top_level_counts: list[int] = []

    def parse() -> None:
        try:
            parser = load_parsers()[0][cs.SupportedLanguage.PYTHON]
            start.wait()
            for _ in range(3):
                top_level_counts.append(parser.parse(source).root_node.child_count)
        except BaseException as error:  # reported below, not swallowed
            start.abort()
            failures.append(error)

    threads = [threading.Thread(target=parse, daemon=True) for _ in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(THREAD_DEADLINE_S)

    assert not any(thread.is_alive() for thread in threads), "a parse never ended"
    assert failures == []
    assert len(top_level_counts) == THREADS * 3
    assert min(top_level_counts) > 0
