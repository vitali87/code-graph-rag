# (H) nlohmann's ordered_map.hpp: a bare namespace-opening macro line
# (H) (`NLOHMANN_JSON_NAMESPACE_BEGIN`, no semicolon) glues onto the following
# (H) `template <...> struct ordered_map : std::vector<...>` and tree-sitter
# (H) parses the pair as ONE declaration -- the struct head vanishes into an
# (H) init_declarator/compound_literal_expression, the class never registers,
# (H) and every member leaks to module scope. The error is LOCAL (a couple of
# (H) small ERROR nodes), so the catastrophic whole-file recovery never fires.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import (
    get_nodes,
    get_qualified_names,
    run_updater,
)

_SOURCE = """\
#include <vector>

NLOHMANN_JSON_NAMESPACE_BEGIN

/// ordered_map: a minimal map-like container that preserves insertion order
template <class Key, class T, class IgnoredLess = std::less<Key>,
          class Allocator = std::allocator<std::pair<const Key, T>>>
              struct ordered_map : std::vector<std::pair<const Key, T>, Allocator>
{
    using key_type = Key;
    using Container = std::vector<std::pair<const Key, T>, Allocator>;

    ordered_map() noexcept {}

    T& at(const Key& key)
    {
        return locate(key);
    }

    T& locate(const Key& key)
    {
        return this->front().second;
    }
};

NLOHMANN_JSON_NAMESPACE_END
"""


def _write(root: Path) -> None:
    root.mkdir()
    (root / "ordered_map.hpp").write_text(_SOURCE, encoding="utf-8")


def test_macro_swallowed_struct_registers_as_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "msproj"
    _write(root)
    run_updater(root, mock_ingestor, skip_if_missing="cpp")

    classes = get_qualified_names(get_nodes(mock_ingestor, "Class"))
    assert "msproj.ordered_map.ordered_map" in classes, sorted(classes)

    methods = get_qualified_names(get_nodes(mock_ingestor, "Method"))
    assert "msproj.ordered_map.ordered_map.at" in methods, sorted(methods)
    assert "msproj.ordered_map.ordered_map.locate" in methods, sorted(methods)

    # (H) members must not leak to module scope as free functions
    functions = get_qualified_names(get_nodes(mock_ingestor, "Function"))
    assert "msproj.ordered_map.at" not in functions, sorted(functions)


def test_macro_swallowed_struct_keeps_member_call_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "mscalls"
    _write(root)
    run_updater(root, mock_ingestor, skip_if_missing="cpp")

    calls = {
        (c.args[0][2], c.args[2][2])
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if c.args[1] == "CALLS"
    }
    assert (
        "mscalls.ordered_map.ordered_map.at",
        "mscalls.ordered_map.ordered_map.locate",
    ) in calls, sorted(calls)


def test_macro_swallowed_namespace_recovers_qualified_names(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) the marker also swallows a following `namespace detail {` -- the pair
    # (H) parses as a function_definition literally NAMED `namespace`, erasing
    # (H) the namespace from every member's qualified name
    root = temp_repo / "mshealthy"
    root.mkdir()
    # (H) the opening marker carries a trailing line comment: matching must
    # (H) strip `//` prose first, like the brace counter does
    (root / "scope.hpp").write_text(
        """\
SOME_NAMESPACE_BEGIN  // opens the library scope

namespace detail
{
int helper()
{
    return 1;
}
}  // namespace detail

SOME_NAMESPACE_END
""",
        encoding="utf-8",
    )
    run_updater(root, mock_ingestor, skip_if_missing="cpp")

    functions = get_qualified_names(get_nodes(mock_ingestor, "Function"))
    assert "mshealthy.scope.detail.helper" in functions, sorted(functions)


def test_marker_retry_guard_shapes() -> None:
    import pytest as _pytest

    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.parsers.cpp.preproc_recovery import (
        _retry_without_macro_markers,
    )

    parsers, _ = load_parsers()
    if "cpp" not in parsers:
        _pytest.skip("cpp parser not available")
    cpp = parsers["cpp"]

    # (H) a clean tree is returned untouched
    clean = b"int x = 1;\n"
    tree = cpp.parse(clean)
    kept, kept_src = _retry_without_macro_markers(cpp, tree, clean)
    assert kept is tree
    assert kept_src is clean

    # (H) an erroring file with no marker line has nothing to blank
    broken = b"void f( {\n"
    tree2 = cpp.parse(broken)
    kept2, _ = _retry_without_macro_markers(cpp, tree2, broken)
    assert kept2 is tree2

    # (H) a marker that does NOT explain the error keeps the original tree via
    # (H) the strict-improvement guard: the brace garbage errors identically
    # (H) with or without the marker line
    mixed = b"SOME_MARKER_MACRO\n}}}}\n"
    tree3 = cpp.parse(mixed)
    kept3, kept3_src = _retry_without_macro_markers(cpp, tree3, mixed)
    assert kept3 is tree3
    assert kept3_src is mixed

    # (H) smallest subset wins: a marker-shaped line inside a raw string must
    # (H) survive when blanking the single real offender already explains the
    # (H) error (all-at-once would corrupt the string as collateral)
    raw = (
        b"SOME_MARKER_MACRO\n"
        b"struct swallowed : base {\n"
        b"  int x;\n"
        b"};\n"
        b'const char* payload = R"(\n'
        b"INSIDE_A_RAW_STRING\n"
        b')";\n'
    )
    tree4 = cpp.parse(raw)
    kept4, kept4_src = _retry_without_macro_markers(cpp, tree4, raw)
    assert b"INSIDE_A_RAW_STRING" in kept4_src
