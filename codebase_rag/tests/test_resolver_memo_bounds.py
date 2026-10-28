"""The resolver's name memos are bounded.

`_trie_search_name`, the candidate-name split and Go's stored result parse
memoised into module dicts that were never cleared. A long-lived MCP server
or `cgr watch` indexing several repositories kept every call name and
qualified name it had ever seen.
"""

from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers import call_resolver
from codebase_rag.parsers.go import interfaces as go_ifaces


def test_the_search_name_memo_stays_within_its_bound() -> None:
    call_resolver._trie_search_name.cache_clear()
    for i in range(cs.RESOLVER_NAME_CACHE_SIZE + 10):
        assert call_resolver._trie_search_name(f"pkg.mod.f{i}") == f"f{i}"
    info = call_resolver._trie_search_name.cache_info()
    assert info.currsize == cs.RESOLVER_NAME_CACHE_SIZE


def test_the_qualified_name_split_stays_within_its_bound() -> None:
    call_resolver._qn_parts.cache_clear()
    for i in range(cs.RESOLVER_NAME_CACHE_SIZE + 10):
        assert call_resolver._qn_parts(f"a.b{i}") == ("a", f"b{i}")
    assert call_resolver._qn_parts.cache_info().currsize == cs.RESOLVER_NAME_CACHE_SIZE


def test_go_result_parses_stay_within_their_bound() -> None:
    parser = load_parsers()[0][cs.SupportedLanguage.GO]
    go_ifaces._parsed_result_types.cache_clear()
    assert go_ifaces.stored_result_types("(int, error)", parser) == ("int", "error")
    for i in range(cs.GO_RESULT_CACHE_SIZE + 10):
        go_ifaces.stored_result_types(f"T{i}", parser)
    info = go_ifaces._parsed_result_types.cache_info()
    assert info.currsize == cs.GO_RESULT_CACHE_SIZE


def test_a_go_result_without_a_parser_is_unknown() -> None:
    assert go_ifaces.stored_result_types("(int, error)", None) is None
    assert go_ifaces.stored_result_types("", None) == go_ifaces.declared_types(None)
