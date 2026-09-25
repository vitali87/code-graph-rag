from __future__ import annotations

from pathlib import Path

import pytest
from tree_sitter import Node, Parser

from codebase_rag import ast_cache as ast_cache_module
from codebase_rag import constants as cs
from codebase_rag.ast_cache import BoundedASTCache
from codebase_rag.parser_loader import load_parsers


@pytest.fixture(scope="module")
def py_parser() -> Parser:
    parsers, _ = load_parsers()
    if cs.SupportedLanguage.PYTHON not in parsers:
        pytest.skip("python parser not available")
    return parsers[cs.SupportedLanguage.PYTHON]


def _tree(parser: Parser, n_funcs: int) -> tuple[Node, cs.SupportedLanguage]:
    src = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(n_funcs))
    return parser.parse(src.encode()).root_node, cs.SupportedLanguage.PYTHON


def _expected_total(cache: BoundedASTCache) -> int:
    return sum(node.end_byte for node, _ in cache.cache.values())


def test_memory_cap_evicts_large_sources(py_parser: Parser) -> None:
    # Each tree spans ~100 KB of source; a 1 MB cap must hold roughly ten of
    # them. The old estimate (sys.getsizeof of the tuple, ~56 bytes) kept all 50.
    cache = BoundedASTCache(max_entries=1000, max_memory_mb=1)
    big = _tree(py_parser, 3000)
    assert big[0].end_byte > 50_000
    for i in range(50):
        cache[Path(f"m{i}.py")] = big

    assert 1 < len(cache.cache) < 50
    assert cache.total_bytes <= cache.max_memory_bytes
    # LRU order: the newest files survive, the oldest were evicted.
    assert Path("m49.py") in cache
    assert Path("m0.py") not in cache


def test_memory_cap_respects_lru_access(py_parser: Parser) -> None:
    entry = _tree(py_parser, 50)
    cache = BoundedASTCache(max_entries=1000)
    cache.max_memory_bytes = entry[0].end_byte * 3
    for name in ("a", "b", "c"):
        cache[Path(name)] = entry
    _ = cache[Path("a")]  # refresh "a" so "b" is now least recently used
    cache[Path("d")] = entry

    assert Path("b") not in cache
    assert all(Path(n) in cache for n in ("a", "c", "d"))


def test_oversized_single_entry_is_kept(py_parser: Parser) -> None:
    # The caller is about to use the tree it just inserted; dropping it would
    # only force load() to re-parse it.
    cache = BoundedASTCache(max_entries=10)
    cache.max_memory_bytes = 1
    cache[Path("x")] = _tree(py_parser, 10)
    assert Path("x") in cache


def test_running_total_consistent_across_replace_evict_delete_clear(
    py_parser: Parser,
) -> None:
    small, medium, large = (_tree(py_parser, n) for n in (5, 50, 500))
    cache = BoundedASTCache(max_entries=3, max_memory_mb=100)

    cache[Path("a")] = small
    cache[Path("b")] = medium
    assert cache.total_bytes == _expected_total(cache)

    cache[Path("a")] = large  # replace must drop the old size, not add to it
    assert cache.total_bytes == _expected_total(cache)
    assert cache.total_bytes == medium[0].end_byte + large[0].end_byte

    cache[Path("c")] = small
    cache[Path("d")] = small  # count cap evicts "b"
    assert Path("b") not in cache
    assert cache.total_bytes == _expected_total(cache)

    del cache[Path("a")]
    del cache[Path("missing")]  # absent key is a no-op, total unchanged
    assert cache.total_bytes == _expected_total(cache)

    cache.clear()
    assert cache.total_bytes == 0
    assert not cache.cache
    cache[Path("e")] = medium
    assert cache.total_bytes == medium[0].end_byte


def test_insert_sizes_only_the_new_entry(
    py_parser: Parser, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sizing happens once per insert; re-summing every entry made each insert
    # O(n) in the cache size.
    calls: list[int] = []
    real = ast_cache_module._estimate_entry_bytes

    def counting(value: tuple[Node, cs.SupportedLanguage]) -> int:
        calls.append(1)
        return real(value)

    monkeypatch.setattr(ast_cache_module, "_estimate_entry_bytes", counting)
    entry = _tree(py_parser, 5)
    cache = BoundedASTCache(max_entries=1000, max_memory_mb=100)
    for i in range(200):
        cache[Path(f"m{i}.py")] = entry

    assert len(calls) == 200
    assert cache.total_bytes == 200 * entry[0].end_byte
