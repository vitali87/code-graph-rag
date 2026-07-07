from __future__ import annotations

import pytest
from tree_sitter import Language

from codebase_rag.parsers.utils import get_cached_query

try:
    import tree_sitter_javascript as tsjs

    JS_AVAILABLE = True
except ImportError:
    JS_AVAILABLE = False

PATTERN = "(function_declaration) @f"


@pytest.mark.skipif(not JS_AVAILABLE, reason="tree-sitter-javascript not available")
def test_cache_keyed_by_grammar_not_wrapper_address() -> None:
    # Regression: the cache was keyed by id(language_obj) without retaining
    # the wrapper, so a GC'd wrapper's address could be reused by a Language
    # for a different grammar and serve its Query, silently dropping captures.
    # Keying by the Language itself (grammar identity) makes distinct wrappers
    # of the same grammar share one entry and pins the key alive.
    wrapper_a = Language(tsjs.language())
    wrapper_b = Language(tsjs.language())
    assert id(wrapper_a) != id(wrapper_b)
    assert get_cached_query(wrapper_a, PATTERN) is get_cached_query(wrapper_b, PATTERN)
