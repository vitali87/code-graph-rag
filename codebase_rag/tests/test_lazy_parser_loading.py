# (H) Issue #68: a repo in one language paid for all 14 grammars anyway --
# (H) every load_parsers() call imported every grammar module and compiled
# (H) every language's query set up front (and re-compiled them on EVERY
# (H) call). Grammars must load on first use per language, once per process.
from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag.parser_loader import (
    COMBINED_FUNC_CLASS_QUERIES,
    _reset_parser_cache,
    load_parsers,
)


def test_load_parsers_defers_grammar_work() -> None:
    _reset_parser_cache()
    saved = dict(COMBINED_FUNC_CLASS_QUERIES)
    COMBINED_FUNC_CLASS_QUERIES.clear()
    try:
        parsers, queries = load_parsers()
        # (H) nothing is compiled until a language is actually used
        assert cs.SupportedLanguage.PYTHON not in COMBINED_FUNC_CLASS_QUERIES
        assert cs.SupportedLanguage.RUST not in COMBINED_FUNC_CLASS_QUERIES

        lang_queries = queries[cs.SupportedLanguage.PYTHON]
        assert lang_queries[cs.KEY_PARSER] is parsers[cs.SupportedLanguage.PYTHON]
        assert COMBINED_FUNC_CLASS_QUERIES.get(cs.SupportedLanguage.PYTHON) is not None
        # (H) touching python must not have loaded rust
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
        # (H) conftest-style availability probe (`lang in parsers`) must load
        # (H) that one language and answer truthfully
        assert cs.SupportedLanguage.RUST in parsers
        assert COMBINED_FUNC_CLASS_QUERIES.get(cs.SupportedLanguage.RUST) is not None
        assert cs.SupportedLanguage.PYTHON not in COMBINED_FUNC_CLASS_QUERIES
    finally:
        _reset_parser_cache()
        COMBINED_FUNC_CLASS_QUERIES.clear()
        COMBINED_FUNC_CLASS_QUERIES.update(saved)


def test_loaded_grammars_are_shared_across_calls() -> None:
    # (H) parsers must be process-cached: a second load_parsers() reuses the
    # (H) same Parser objects instead of recompiling every query set
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
    # (H) An MCP server can probe the same language from several request
    # (H) threads at once; the mid-load window (attempted but not yet loaded)
    # (H) must not make a later thread see the language as unavailable
    # (H) (PR #802 review).
    import threading
    import time

    from codebase_rag import parser_loader

    _reset_parser_cache()
    saved = dict(COMBINED_FUNC_CLASS_QUERIES)
    COMBINED_FUNC_CLASS_QUERIES.clear()
    real = parser_loader._process_language

    def slow_process(lang, spec, parsers, queries):  # type: ignore[no-untyped-def]
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


def test_full_views_still_cover_every_available_language() -> None:
    # (H) a consumer that iterates the mapping gets the complete availability
    # (H) picture, not just what happens to be loaded already
    parsers, queries = load_parsers()
    assert cs.SupportedLanguage.PYTHON in set(parsers)
    assert len(parsers) == len(queries)
    assert set(parsers.keys()) == set(queries.keys())
