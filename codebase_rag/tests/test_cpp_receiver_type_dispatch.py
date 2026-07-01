from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.cpp import CppTypeInferenceEngine
from codebase_rag.tests.conftest import (
    get_relationships,
    run_updater,
)

# (H) Two classes define a method of the same name; only the receiver's type tells
# (H) which one a call `z->run()` / `z.run()` targets. cgr resolved C++ member calls
# (H) by the bare method name (the field_expression yielded only `run`), so the
# (H) name-only trie fallback bound every `run()` call to whichever `run` sorted
# (H) first (`Alpha.run`), regardless of the receiver. With receiver type inference
# (H) (parameter/local var -> bare class name), `z` is a `Zeta`, so the call must
# (H) resolve to `Zeta.run`. `Alpha` sorts before `Zeta`, so the wrong (old) answer
# (H) is deterministic and this test is a real RED.
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
    # (H) map caller-qn -> callee-qn for every CALLS edge whose callee is a `run`.
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


# (H) A C++ reference parameter (`Alpha& ar`) parses as a `reference_declarator` that
# (H) holds its identifier as a POSITIONAL child, not under the `declarator` field the
# (H) way `pointer_declarator` does. The field-only unwrap in `_declarator_name` stalled
# (H) on it, so reference parameters/locals never entered the type map and their member
# (H) calls fell back to bare-name resolution. References are pervasive in C++, so this
# (H) is a real coverage hole.
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


# (H) A single-type-per-name map cannot model true lexical scope, but an inner-block
# (H) redeclaration must NOT clobber the outer binding for calls made in the outer
# (H) scope. Traversal is outermost-first, so first-write-wins keeps the outer `Zeta z`
# (H) even though an inner block shadows it with `Alpha z`; the trailing `z.run()` sits
# (H) in the outer scope and must bind to Zeta.run.
_SHADOW_SOURCE = """
namespace ns {

class Alpha {
 public:
  int run() { return 1; }
};

class Zeta {
 public:
  int run() { return 2; }
};

int use_shadow() {
  Zeta z;
  { Alpha z; }
  return z.run();
}

}  // namespace ns
"""


def test_cpp_outer_scope_survives_inner_shadow(
    temp_repo: Path,
    mock_ingestor: MagicMock,
) -> None:
    project = temp_repo / "cpp_shadow"
    project.mkdir()
    (project / "s.cpp").write_text(_SHADOW_SOURCE, encoding="utf-8")

    run_updater(project, mock_ingestor)

    calls = _calls_to_run(mock_ingestor)
    caller = next(q for q in calls if q.endswith(".use_shadow"))
    assert calls[caller].endswith(".Zeta.run"), (
        f"outer-scope z.run() should resolve to Zeta.run despite inner Alpha shadow, "
        f"got {calls[caller]}"
    )
