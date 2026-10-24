# Julia structural support: modules, functions (explicit, concise, where,
# qualified, macros, arrows), types with `<:` inheritance, inner
# constructors. FLAT like Lua: an inner constructor's qn is `mod.Point.Point`
# (the C#/Java convention); the class pass mints only the type nodes.
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.julia import utils as julia_utils
from codebase_rag.tests.conftest import (
    get_node_names,
    get_nodes,
    get_relationships,
    run_updater,
)
from codebase_rag.types_defs import NodeType

if TYPE_CHECKING:
    from tree_sitter import Node, Parser

SKIP = "julia"


@pytest.fixture
def julia_project(temp_repo: Path) -> Path:
    project = temp_repo / "julia_defs"
    project.mkdir()
    return project


@pytest.fixture
def julia_parser() -> "Parser":
    parsers, _ = load_parsers()
    parser = parsers.get(cs.SupportedLanguage.JULIA)
    if parser is None:
        # Raise rather than pytest.skip: skip's NoReturn does not narrow the
        # dict's value type, and the fixture's contract is a live parser.
        raise pytest.skip.Exception("Julia parser not available")
    return parser


def _props_for(mock_ingestor: MagicMock, node_type: NodeType, suffix: str) -> dict:
    for call in get_nodes(mock_ingestor, node_type):
        props = call[0][1]
        if props["qualified_name"].endswith(suffix):
            return props
    raise AssertionError(f"no {node_type} qn ending with {suffix!r}")


def test_explicit_functions(julia_project: Path, mock_ingestor: MagicMock) -> None:
    (julia_project / "main.jl").write_text(
        """
function add(a, b)
    return a + b
end

function identity2(x) where T
    return x
end

function typed(x)::Int where T<:AbstractFloat
    return 1
end
""",
        encoding="utf-8",
    )
    run_updater(julia_project, mock_ingestor, skip_if_missing=SKIP)

    qns = get_node_names(mock_ingestor, NodeType.FUNCTION)
    assert any(qn.endswith(".add") for qn in qns), qns
    assert any(qn.endswith(".identity2") for qn in qns), qns
    assert any(qn.endswith(".typed") for qn in qns), qns


def test_concise_methods(julia_project: Path, mock_ingestor: MagicMock) -> None:
    (julia_project / "main.jl").write_text(
        """
first_fn(x) = x * 2

addw(a, b::Int) where T = a + b

function_begin(x) = begin
    y = x + 1
    return y
end

named(a::Int = 1, b = "x") = a
""",
        encoding="utf-8",
    )
    run_updater(julia_project, mock_ingestor, skip_if_missing=SKIP)

    qns = get_node_names(mock_ingestor, NodeType.FUNCTION)
    assert any(qn.endswith(".first_fn") for qn in qns), qns
    assert any(qn.endswith(".addw") for qn in qns), qns
    assert any(qn.endswith(".function_begin") for qn in qns), qns
    assert any(qn.endswith(".named") for qn in qns), qns


def test_typed_concise_methods(julia_project: Path, mock_ingestor: MagicMock) -> None:
    """A return type (and a `where` clause) wrap the concise method's left
    side in `typed_expression`/`where_expression`; the head is still the
    call underneath, and the decoration must not leak into the qn."""
    (julia_project / "main.jl").write_text(
        """
double(x)::Int = x * 2

quad(x)::Int where T = x * 4
""",
        encoding="utf-8",
    )
    run_updater(julia_project, mock_ingestor, skip_if_missing=SKIP)

    qns = get_node_names(mock_ingestor, NodeType.FUNCTION)
    assert any(qn.endswith(".double") for qn in qns), qns
    assert any(qn.endswith(".quad") for qn in qns), qns
    assert not any("::" in qn or "where" in qn for qn in qns), qns


def test_plain_assignment_is_not_function(
    julia_project: Path, mock_ingestor: MagicMock
) -> None:
    (julia_project / "main.jl").write_text(
        """
x = 1
y = f()
x::Int = 1
const C = 42
global G = 0
""",
        encoding="utf-8",
    )
    run_updater(julia_project, mock_ingestor, skip_if_missing=SKIP)

    qns = get_node_names(mock_ingestor, NodeType.FUNCTION)
    assert not any(qn.endswith(".x") for qn in qns), qns
    assert not any(qn.endswith(".y") for qn in qns), qns
    assert not any(qn.endswith(".C") for qn in qns), qns
    assert not any(qn.endswith(".G") for qn in qns), qns


def test_qualified_concise(julia_project: Path, mock_ingestor: MagicMock) -> None:
    (julia_project / "main.jl").write_text(
        """
Base.show(io, x) = print(io, x)
""",
        encoding="utf-8",
    )
    run_updater(julia_project, mock_ingestor, skip_if_missing=SKIP)

    qns = get_node_names(mock_ingestor, NodeType.FUNCTION)
    assert any(qn.endswith(".Base.show") for qn in qns), qns


def test_macro_definition_registers_with_is_macro(
    julia_project: Path, mock_ingestor: MagicMock
) -> None:
    (julia_project / "main.jl").write_text(
        """
macro build(expr)
    :()
end

macro plain(x, y)
    :()
end
""",
        encoding="utf-8",
    )
    run_updater(julia_project, mock_ingestor, skip_if_missing=SKIP)

    props = _props_for(mock_ingestor, NodeType.FUNCTION, ".build")
    assert props.get("is_macro") is True, props
    props_plain = _props_for(mock_ingestor, NodeType.FUNCTION, ".plain")
    assert props_plain.get("is_macro") is True, props_plain

    # A non-macro function must NOT carry the flag.
    (julia_project / "fn.jl").write_text(
        """
fn(x) = x
""",
        encoding="utf-8",
    )
    mock_ingestor.reset_mock()
    run_updater(julia_project, mock_ingestor, skip_if_missing=SKIP)
    props_fn = _props_for(mock_ingestor, NodeType.FUNCTION, ".fn")
    assert not props_fn.get("is_macro"), props_fn


def test_arrow_function(julia_project: Path, mock_ingestor: MagicMock) -> None:
    (julia_project / "main.jl").write_text(
        """
sq = x -> x * x

M.f = x -> x + 1
""",
        encoding="utf-8",
    )
    run_updater(julia_project, mock_ingestor, skip_if_missing=SKIP)

    qns = get_node_names(mock_ingestor, NodeType.FUNCTION)
    assert any(qn.endswith(".sq") for qn in qns), qns
    assert any(qn.endswith(".M.f") for qn in qns), qns


def test_structs_and_types(julia_project: Path, mock_ingestor: MagicMock) -> None:
    (julia_project / "main.jl").write_text(
        """
abstract type Animal end

primitive type MyFloat 32 end

struct Point
    x::Float64
    y::Float64
end

mutable struct Dog <: Animal
    name::String
end

struct Cat <: Base.Animal
    name::String
end

struct Pair{T, S}
    a::T
    b::S
end

struct WrappedDog{T} <: Dog{T}
    dog::T
end
""",
        encoding="utf-8",
    )
    run_updater(julia_project, mock_ingestor, skip_if_missing=SKIP)

    class_qns = get_node_names(mock_ingestor, NodeType.CLASS)
    for suffix in (".Animal", ".MyFloat", ".Point", ".Dog", ".Cat", ".Pair"):
        assert any(qn.endswith(suffix) for qn in class_qns), (suffix, class_qns)
    # Type parameters must not leak into the qn.
    assert not any("{" in qn for qn in class_qns), class_qns

    inherits = {
        (c.args[0][2].split(".")[-1], c.args[2][2].split(".")[-1])
        for c in get_relationships(mock_ingestor, "INHERITS")
    }
    assert ("Dog", "Animal") in inherits, inherits
    assert ("Cat", "Animal") in inherits, inherits  # qualified base: last segment
    assert ("WrappedDog", "Dog") in inherits, inherits  # parametrized base


def test_inner_constructor_is_flat_function(
    julia_project: Path, mock_ingestor: MagicMock
) -> None:
    (julia_project / "main.jl").write_text(
        """
struct Point
    x::Float64
    y::Float64
    function Point(x, y)
        new(Float64(x), Float64(y))
    end
    Point(x::Int) = Point(float(x), 0.0)
end
""",
        encoding="utf-8",
    )
    run_updater(julia_project, mock_ingestor, skip_if_missing=SKIP)

    qns = get_node_names(mock_ingestor, NodeType.FUNCTION)
    # C#/Java constructor convention: the type scope is part of the qn.
    assert any(qn.endswith(".Point.Point") for qn in qns), qns


def test_parametrized_inner_constructor_name(
    julia_project: Path, mock_ingestor: MagicMock
) -> None:
    """`function Arr{T, N}(ex)` names the `Arr` constructor: the type
    parameters and the argument list must not leak into the qn, or the
    definition is unaddressable and every `Arr{...}(...)` call misses it."""
    (julia_project / "main.jl").write_text(
        """
struct Box{T, N}
    data::T
    function Box{T, N}(v) where {T, N}
        new{T}(v)
    end
end
""",
        encoding="utf-8",
    )
    run_updater(julia_project, mock_ingestor, skip_if_missing=SKIP)

    qns = get_node_names(mock_ingestor, NodeType.FUNCTION)
    assert any(qn.endswith(".Box.Box") for qn in qns), qns
    box_qns = [qn for qn in qns if ".Box" in qn]
    assert all("{" not in qn and "(" not in qn for qn in box_qns), box_qns


def test_closure_head_named_after_type(
    julia_project: Path, mock_ingestor: MagicMock
) -> None:
    """`(D::Differential)(x) = ...` is a method on type `Differential`:
    the closure head is named after the type it closes over, not the
    receiver variable or the raw head text. The TYPE owns the natural qn
    (types ingest before functions), so the closure method takes the
    duplicate variant (issue #1882 review)."""
    (julia_project / "main.jl").write_text(
        """
struct Differential
end

(D::Differential)(x) = x + 1
""",
        encoding="utf-8",
    )
    run_updater(julia_project, mock_ingestor, skip_if_missing=SKIP)

    fn_qns = get_node_names(mock_ingestor, NodeType.FUNCTION)
    class_qns = get_node_names(mock_ingestor, NodeType.CLASS)
    module_qn = f"{julia_project.name}.main"
    assert f"{module_qn}.Differential" in class_qns, class_qns
    assert any(
        qn.startswith(f"{module_qn}.Differential{cs.DUP_QN_MARKER}") for qn in fn_qns
    ), fn_qns
    assert not any("(" in qn or "::" in qn for qn in fn_qns), fn_qns


def test_macro_emitted_head(julia_project: Path, mock_ingestor: MagicMock) -> None:
    """`@eval $op(c, x, y) = ...` emits a method whose name is interpolated
    from the loop variable: the qn takes the variable's name, not the `$`
    marker or the full head text."""
    (julia_project / "main.jl").write_text(
        """
for op in (:foo, :bar)
    @eval $op(c, x, y) = $op(c, x) + $op(c, y)
end
""",
        encoding="utf-8",
    )
    run_updater(julia_project, mock_ingestor, skip_if_missing=SKIP)

    qns = get_node_names(mock_ingestor, NodeType.FUNCTION)
    assert any(qn.endswith(".op") for qn in qns), qns
    assert not any("$" in qn or "(" in qn for qn in qns), qns


def test_function_class_name_collision(
    julia_project: Path, mock_ingestor: MagicMock
) -> None:
    """A top-level function and a struct may share a name in one module
    (Julia's namespaces are separate). Pinned behavior: the TYPE owns the
    natural qn (field types, `Arr{T}` and the `Arr(v)` constructor call all
    resolve it to the type, and the inner constructor scopes on the type's
    real qn); the free function takes the `@<line>` duplicate variant.
    The reverse (functions first) orphaned the constructor: `Arr(...)`
    calls bound the shadowing free function (issue #1882 review)."""
    (julia_project / "main.jl").write_text(
        """
struct Arr
    x::Float64
    function Arr(v)
        new(v)
    end
end

function Arr(x)
    return x + 1
end
""",
        encoding="utf-8",
    )
    run_updater(julia_project, mock_ingestor, skip_if_missing=SKIP)

    fn_qns = get_node_names(mock_ingestor, NodeType.FUNCTION)
    class_qns = get_node_names(mock_ingestor, NodeType.CLASS)
    module_qn = f"{julia_project.name}.main"
    assert f"{module_qn}.Arr" in class_qns, class_qns
    assert not any(
        qn.startswith(f"{module_qn}.Arr{cs.DUP_QN_MARKER}") for qn in class_qns
    ), class_qns
    assert f"{module_qn}.Arr.Arr" in fn_qns, fn_qns
    assert any(qn.startswith(f"{module_qn}.Arr{cs.DUP_QN_MARKER}") for qn in fn_qns), (
        fn_qns
    )
    assert f"{module_qn}.Arr" not in fn_qns, fn_qns


def _first_of_type(root: "Node", node_type: str) -> "Node | None":
    if root.type == node_type:
        return root
    for child in root.children:
        if (found := _first_of_type(child, node_type)) is not None:
            return found
    return None


def test_recovered_span_is_not_a_name(julia_parser: "Parser") -> None:
    """The grammar's error recovery can make a definition head span source
    lines (on a `:(->)` it re-syncs hundreds of lines downstream, so the
    "callee" is the whole recovery chain). Reducing that span to a name
    would mint a qn from source code, so a multi-line candidate is refused
    and the node degrades to anonymous registration (issue #1882 review).
    Single-line best-effort names are untouched."""
    ternary = _first_of_type(
        julia_parser.parse(b"v = a ?\n    b :\n    c\n").root_node,
        "ternary_expression",
    )
    ternary_text = ternary.text if ternary is not None else None
    assert ternary_text is not None and b"\n" in ternary_text
    assert julia_utils.julia_callee_name(ternary) is None

    identifier = _first_of_type(
        julia_parser.parse(b"fn(x) = 1\n").root_node, "identifier"
    )
    identifier_text = identifier.text if identifier is not None else None
    assert identifier is not None and identifier_text is not None
    assert julia_utils.julia_callee_name(identifier) == identifier_text.decode()


def test_error_recovered_definition_stays_bounded(
    julia_project: Path, mock_ingestor: MagicMock
) -> None:
    """Adversarial heads -- the `:(->)` quoted-operator shape that triggers
    the grammar's long-range error recovery on real code (it minted an
    11 KB qn in Symbolics.jl, issue #1882 review): whatever the parser
    recovers, no function or class may register with a qn spanning lines
    or carrying source code, and the definitions after the broken region
    must survive."""
    (julia_project / "broken.jl").write_text(
        """
function _build_and_inject_function(mod, ex)
    if ex.head == :function && ex.args[1].head == :tuple
        ex.args[1] = Expr(:call, :($mod.$(gensym())), ex.args[1].args...)
    elseif ex.head == :(->)
        return _build_and_inject_function(mod, Expr(:function, ex.args...))
    end
    RuntimeGeneratedFunction(mod, mod, ex)
end

function after_fn(a)
    return after_helper(a) + 1
end

function after_helper(a)
    return a * 2
end
""",
        encoding="utf-8",
    )
    run_updater(julia_project, mock_ingestor, skip_if_missing=SKIP)

    for node_type in (NodeType.FUNCTION, NodeType.CLASS):
        for qn in get_node_names(mock_ingestor, node_type):
            assert "\n" not in qn, qn
            assert len(qn) < 200, qn
    qns = get_node_names(mock_ingestor, NodeType.FUNCTION)
    assert any(qn.endswith(".after_fn") for qn in qns), qns
    assert any(qn.endswith(".after_helper") for qn in qns), qns


def test_module_definition_scope(julia_project: Path, mock_ingestor: MagicMock) -> None:
    (julia_project / "main.jl").write_text(
        """
module Inner
    inner_fn(x) = x
    function inner_explicit(x)
        x
    end
end

outer_fn(x) = x
""",
        encoding="utf-8",
    )
    run_updater(julia_project, mock_ingestor, skip_if_missing=SKIP)

    qns = get_node_names(mock_ingestor, NodeType.FUNCTION)
    assert any(qn.endswith(".Inner.inner_fn") for qn in qns), qns
    assert any(qn.endswith(".Inner.inner_explicit") for qn in qns), qns
    assert any(qn.endswith(".outer_fn") and ".Inner." not in qn for qn in qns), qns


def test_primitive_type_inherits(julia_project: Path, mock_ingestor: MagicMock) -> None:
    """`primitive type MyFloat <: Signed 32 end` declares a `<:` base like
    struct/abstract: it gets the INHERITS edge (issue #1882 review: the
    extractor skipped the node type)."""
    (julia_project / "main.jl").write_text(
        """
struct Signed
end

primitive type MyFloat <: Signed 32 end
""",
        encoding="utf-8",
    )
    run_updater(julia_project, mock_ingestor, skip_if_missing=SKIP)

    class_qns = get_node_names(mock_ingestor, NodeType.CLASS)
    assert any(qn.endswith(".MyFloat") for qn in class_qns), class_qns
    inherits = {
        (c.args[0][2].split(".")[-1], c.args[2][2].split(".")[-1])
        for c in get_relationships(mock_ingestor, "INHERITS")
    }
    assert ("MyFloat", "Signed") in inherits, inherits


def test_arrow_in_call_not_registered_as_function(
    julia_project: Path, mock_ingestor: MagicMock
) -> None:
    """`ys = map(x -> x + 1, xs)` registers NO function: the arrow is an
    argument of the call, not a direct binding, and naming the callback
    `ys` would mint a phantom twin (issue #1882 review)."""
    (julia_project / "main.jl").write_text(
        "ys = map(x -> x + 1, xs)\n",
        encoding="utf-8",
    )
    run_updater(julia_project, mock_ingestor, skip_if_missing=SKIP)

    fn_qns = get_node_names(mock_ingestor, NodeType.FUNCTION)
    assert not any(qn.endswith(".ys") for qn in fn_qns), fn_qns


def test_arrow_assigned_name_direct_binding_only(julia_parser: "Parser") -> None:
    """Unit: only the assignment whose VALUE is the arrow names it — behind
    at most typed/where/paren decoration; a call value names nothing."""

    def first_arrow(code: bytes):
        def find(node: "Node") -> "Node | None":
            if node.type == "arrow_function_expression":
                return node
            for child in node.children:
                if (hit := find(child)) is not None:
                    return hit
            return None

        arrow = find(julia_parser.parse(code).root_node)
        assert arrow is not None
        return arrow

    assert (
        julia_utils.julia_arrow_assigned_name(first_arrow(b"f = x -> x + 1\n")) == "f"
    )
    assert (
        julia_utils.julia_arrow_assigned_name(
            first_arrow(b"f::Function = x -> x + 1\n")
        )
        == "f"
    )
    assert (
        julia_utils.julia_arrow_assigned_name(first_arrow(b"f = (x -> x + 1)\n")) == "f"
    )
    assert (
        julia_utils.julia_arrow_assigned_name(first_arrow(b"M.f = x -> x\n")) == "M.f"
    )
    assert (
        julia_utils.julia_arrow_assigned_name(
            first_arrow(b"ys = map(x -> x + 1, xs)\n")
        )
        is None
    )
    assert (
        julia_utils.julia_arrow_assigned_name(first_arrow(b"g = f(x -> x)\n")) is None
    )


def test_module_docstring_not_from_block_comment(
    julia_project: Path, mock_ingestor: MagicMock
) -> None:
    """A leading `#=` block is an ordinary comment (license headers and the
    like): Julia's Module.docstring comes from a docstring literal, not
    from comments (issue #1882 review)."""
    (julia_project / "main.jl").write_text(
        """#=
MIT License header text.
=#
x = 1
""",
        encoding="utf-8",
    )
    run_updater(julia_project, mock_ingestor, skip_if_missing=SKIP)

    props = _props_for(mock_ingestor, NodeType.MODULE, ".main")
    assert props.get("docstring") in (None, ""), props


def test_anonymous_long_form_function_names_nothing(
    julia_project: Path, mock_ingestor: MagicMock
) -> None:
    """`function (x) ... end` is anonymous: the argument-list signature must
    not name a function after its first parameter (issue #1882 review: the
    head reduction produced a phantom function named `x`)."""
    (julia_project / "main.jl").write_text(
        """
function (x)
    return x + 1
end
""",
        encoding="utf-8",
    )
    run_updater(julia_project, mock_ingestor, skip_if_missing=SKIP)

    fn_qns = get_node_names(mock_ingestor, NodeType.FUNCTION)
    assert not any(qn.endswith(".x") for qn in fn_qns), fn_qns


def test_argument_list_signature_names_nothing(julia_parser: "Parser") -> None:
    """Unit: `julia_function_head_name` returns None for an anonymous
    long-form function (argument-list signature), not the first parameter."""

    def head(code: bytes) -> "Node":
        return julia_parser.parse(code).root_node.named_children[0]

    assert julia_utils.julia_function_head_name(head(b"function (x) x end\n")) is None
    assert (
        julia_utils.julia_function_head_name(head(b"function (x, y) x + y end\n"))
        is None
    )
    # A named definition still names itself.
    assert julia_utils.julia_function_head_name(head(b"function f(x) x end\n")) == "f"
