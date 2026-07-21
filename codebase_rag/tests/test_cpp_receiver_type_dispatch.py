from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.cpp import CppTypeInferenceEngine
from codebase_rag.tests.conftest import (
    get_relationships,
    run_updater,
)

# Two classes define a method of the same name; only the receiver's type tells
# which one a call `z->run()` / `z.run()` targets. cgr resolved C++ member calls
# by the bare method name, so the name-only trie fallback bound every `run()` to
# whichever `run` sorted first (`Alpha.run`), regardless of receiver. With
# receiver type inference `z` is a `Zeta`, so the call must resolve to `Zeta.run`;
# `Alpha` sorts first, so the old wrong answer is deterministic and this is a real RED.
CPP_SOURCE = """
namespace ns {

class Alpha {
 public:
  int run() { return 1; }
};

class Zeta {
 public:
  int run() { return 2; }
};

int use_ptr(Zeta* z) { return z->run(); }

int use_val(Zeta z) { return z.run(); }

}  // namespace ns
"""


def _calls_to_run(mock_ingestor: MagicMock) -> dict[str, str]:
    # map caller-qn -> callee-qn for every CALLS edge whose callee is a `run`.
    out: dict[str, str] = {}
    for c in get_relationships(mock_ingestor, "CALLS"):
        callee = str(c.args[2][2])
        if callee.rsplit(".", 1)[-1] == "run":
            out[str(c.args[0][2])] = callee
    return out


def test_cpp_member_call_resolves_via_receiver_type(
    temp_repo: Path,
    mock_ingestor: MagicMock,
) -> None:
    project = temp_repo / "cpp_recv"
    project.mkdir()
    (project / "s.cpp").write_text(CPP_SOURCE, encoding="utf-8")

    run_updater(project, mock_ingestor)

    calls = _calls_to_run(mock_ingestor)
    ptr_caller = next(q for q in calls if q.endswith(".use_ptr"))
    val_caller = next(q for q in calls if q.endswith(".use_val"))

    assert calls[ptr_caller].endswith(".Zeta.run"), (
        f"z->run() should resolve to Zeta.run, got {calls[ptr_caller]}"
    )
    assert calls[val_caller].endswith(".Zeta.run"), (
        f"z.run() should resolve to Zeta.run, got {calls[val_caller]}"
    )


def _first_function_node(source: str):
    parsers, _ = load_parsers()
    tree = parsers["cpp"].parse(source.encode("utf-8"))

    def walk(node):
        if node.type == "function_definition":
            return node
        for child in node.children:
            if (found := walk(child)) is not None:
                return found
        return None

    node = walk(tree.root_node)
    assert node is not None
    return node


# A C++ reference parameter (`Alpha& ar`) parses as a `reference_declarator` that
# holds its identifier as a POSITIONAL child, not under the `declarator` field like
# `pointer_declarator` does. The field-only unwrap in `_declarator_name` stalled on
# it, so reference parameters never entered the type map and their member calls fell
# back to bare-name resolution. References are pervasive in C++, so a real coverage hole.
def test_cpp_reference_parameter_maps_to_type() -> None:
    node = _first_function_node("void f(Alpha& ar, Zeta* zp) { }")
    var_types = CppTypeInferenceEngine().build_local_variable_type_map(node, "m")
    assert var_types.get("ar") == "Alpha", (
        f"reference parameter ar should map to Alpha, got {var_types}"
    )
    assert var_types.get("zp") == "Zeta", (
        f"pointer parameter zp should map to Zeta, got {var_types}"
    )


_LOCAL_REF_SOURCE = """
namespace ns {

class Alpha {
 public:
  int run() { return 1; }
};

class Zeta {
 public:
  int run() { return 2; }
};

int use_local_ref(Zeta& zr) { Zeta& z = zr; return z.run(); }

}  // namespace ns
"""


def test_cpp_local_reference_receiver_resolves(
    temp_repo: Path,
    mock_ingestor: MagicMock,
) -> None:
    project = temp_repo / "cpp_local_ref"
    project.mkdir()
    (project / "s.cpp").write_text(_LOCAL_REF_SOURCE, encoding="utf-8")

    run_updater(project, mock_ingestor)

    calls = _calls_to_run(mock_ingestor)
    caller = next(q for q in calls if q.endswith(".use_local_ref"))
    assert calls[caller].endswith(".Zeta.run"), (
        f"z.run() on a `Zeta& z` should resolve to Zeta.run, got {calls[caller]}"
    )


# The type map is keyed by name with no call-position context, so it cannot model
# true lexical scope: it cannot tell an outer `Zeta z` from an inner-block `Alpha z`
# that shadows it. Either write order emits a confidently wrong typed edge for one
# scope, so a name with conflicting types is NOT inferred at all; the call falls back
# to name-only resolution. An inner-block local whose name does NOT collide is still
# recorded, so common-case recall is preserved.
def test_cpp_conflicting_shadow_type_is_not_inferred() -> None:
    node = _first_function_node("void f() { Zeta z; { Alpha z; } }")
    var_types = CppTypeInferenceEngine().build_local_variable_type_map(node, "m")
    assert "z" not in var_types, (
        f"a name shadowed by a different type must not be inferred, got {var_types}"
    )


def test_cpp_non_conflicting_inner_block_local_is_recorded() -> None:
    node = _first_function_node("void f() { if (c) { Foo x; } }")
    var_types = CppTypeInferenceEngine().build_local_variable_type_map(node, "m")
    assert var_types.get("x") == "Foo", (
        f"an inner-block local with no name collision should resolve, got {var_types}"
    )


# One C++ declaration statement can declare several variables (`Zeta a, b;`), each
# its own `declarator` field child. Recording only the first left `b` unmapped, so
# `b.run()` fell back to bare-name resolution. Every declarator shares the leading
# type and must be recorded, including mixed pointer/plain forms (`Foo* p, q;`).
def test_cpp_multi_declarator_declaration_maps_all_names() -> None:
    node = _first_function_node("void f() { Zeta a, b; Foo* p, q; }")
    var_types = CppTypeInferenceEngine().build_local_variable_type_map(node, "m")
    assert var_types.get("a") == "Zeta" and var_types.get("b") == "Zeta", (
        f"both a and b should map to Zeta, got {var_types}"
    )
    assert var_types.get("p") == "Foo" and var_types.get("q") == "Foo", (
        f"both p and q should map to Foo, got {var_types}"
    )


_MULTI_DECL_SOURCE = """
namespace ns {

class Alpha {
 public:
  int run() { return 1; }
};

class Zeta {
 public:
  int run() { return 2; }
};

int use_second(Zeta& zr) { Zeta a = zr, b = zr; return b.run(); }

}  // namespace ns
"""


def test_cpp_second_declarator_receiver_resolves(
    temp_repo: Path,
    mock_ingestor: MagicMock,
) -> None:
    project = temp_repo / "cpp_multi_decl"
    project.mkdir()
    (project / "s.cpp").write_text(_MULTI_DECL_SOURCE, encoding="utf-8")

    run_updater(project, mock_ingestor)

    calls = _calls_to_run(mock_ingestor)
    caller = next(q for q in calls if q.endswith(".use_second"))
    assert calls[caller].endswith(".Zeta.run"), (
        f"b.run() on the second declarator `Zeta b` should resolve to Zeta.run, "
        f"got {calls[caller]}"
    )


# A lambda body opens its own scope. A same-named variable inside it must not leak
# into the enclosing function's map: without the scope guard the inner `Alpha z`
# conflicts with the outer `Zeta z`, so drop-on-conflict would discard `z` and the
# outer `z.run()` would fall back to name-only (Alpha.run).
def test_cpp_lambda_local_does_not_leak_into_enclosing_scope() -> None:
    node = _first_function_node("void f() { Zeta z; auto g = [](){ Alpha z; }; }")
    var_types = CppTypeInferenceEngine().build_local_variable_type_map(node, "m")
    assert var_types.get("z") == "Zeta", (
        f"outer z should stay Zeta despite a lambda-local Alpha z, got {var_types}"
    )


# When a receiver's inferred type is NOT a first-party class (a `std::string`, any
# external/STL type), a member call on it must not fall through to the bare-method
# trie fallback and rebind to a same-named first-party method. Here `s` is a
# `std::string`, so `s.size()` is an external call; it must NOT resolve to the
# first-party `ns.Widget.size`. Before the guard, the trie fallback bound the bare
# `size` to `Widget.size` (the only first-party `size`), a precision bug the C++
# retrieval eval flagged on leveldb.
_EXTERNAL_RECEIVER_SOURCE = """
#include <string>

namespace ns {

class Widget {
 public:
  int size() { return 1; }
};

int use(std::string s) { return s.size(); }

}  // namespace ns
"""


def test_cpp_external_receiver_call_is_not_rebound_to_first_party(
    temp_repo: Path,
    mock_ingestor: MagicMock,
) -> None:
    project = temp_repo / "cpp_ext"
    project.mkdir()
    (project / "s.cpp").write_text(_EXTERNAL_RECEIVER_SOURCE, encoding="utf-8")

    run_updater(project, mock_ingestor)

    # `use` calls std::string::size, which is external; no CALLS edge from it to
    # the first-party Widget.size may be emitted.
    bad = [
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in get_relationships(mock_ingestor, "CALLS")
        if str(c.args[0][2]).endswith(".use")
        and str(c.args[2][2]).endswith(".Widget.size")
    ]
    assert not bad, (
        f"std::string.size() must not resolve to first-party Widget.size, got {bad}"
    )
