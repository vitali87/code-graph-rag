# Julia call edges: intra-file calls, concise-method targets, macro calls
# (the @-namespace gate), broadcast calls, do blocks, and cross-file
# resolution through the generic trie (no imports needed in the same project).
from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import get_relationships, run_updater

SKIP = "julia"


def _calls(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }


def _calls_to(mock_ingestor: MagicMock, suffix: str) -> list[tuple[str, str]]:
    return [(src, dst) for (src, dst) in _calls(mock_ingestor) if dst.endswith(suffix)]


def _calls_with_resolution(
    mock_ingestor: MagicMock,
) -> set[tuple[str, str, str]]:
    out = set()
    for c in get_relationships(mock_ingestor, "CALLS"):
        props = c.kwargs.get("properties") or {}
        out.add((c.args[0][2], c.args[2][2], props.get("resolution", "")))
    return out


def test_intra_file_calls(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    project = temp_repo / "julia_calls"
    project.mkdir()
    (project / "main.jl").write_text(
        """
function helper(x)
    return x + 1
end

function caller(x)
    y = helper(x)
    return y * 2
end
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    hits = _calls_to(mock_ingestor, ".helper")
    assert any(src.endswith(".caller") for src, _ in hits), _calls(mock_ingestor)


def test_concise_method_call_target(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    project = temp_repo / "julia_calls"
    project.mkdir()
    (project / "main.jl").write_text(
        """
double(x) = x * 2

function user(x)
    return double(x)
end
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    hits = _calls_to(mock_ingestor, ".double")
    assert any(src.endswith(".user") for src, _ in hits), _calls(mock_ingestor)


def test_typed_concise_method_call_target(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """`double(x)::Int = ...` registers `double` (the return type wraps the
    head), and a call site resolves to it."""
    project = temp_repo / "julia_calls"
    project.mkdir()
    (project / "main.jl").write_text(
        """
double(x)::Int = x * 2

function user(x)
    return double(x)
end
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    hits = _calls_to(mock_ingestor, ".double")
    assert any(src.endswith(".user") for src, _ in hits), _calls(mock_ingestor)


def test_typed_heads_are_not_calls(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """A concise method's return type / `where` clause wraps its head: the
    head is still NOT a call site (no self-edge), while a typed assignment's
    right side (`y::Int = f(x)`) and a plain call are real calls."""
    project = temp_repo / "julia_calls"
    project.mkdir()
    (project / "main.jl").write_text(
        """
double(x)::Int = x * 2

quad(x)::Int where T = x * 4

function user(x)
    y::Int = double(x)
    return quad(x) + y
end
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    edges = _calls(mock_ingestor)
    assert not any(src == dst for src, dst in edges), edges
    hits = _calls_to(mock_ingestor, ".double")
    assert any(src.endswith(".user") for src, _ in hits), edges
    hits = _calls_to(mock_ingestor, ".quad")
    assert any(src.endswith(".user") for src, _ in hits), edges


def test_parametrized_constructor_call_instantiates(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """`Box{Float64, 2}(1.0)` names the type `Box` (parameters stripped, the
    same reduction the definition head gets): the edge goes to the class as
    INSTANTIATES, exactly as a plain `Box(v)` construction does."""
    project = temp_repo / "julia_calls"
    project.mkdir()
    (project / "lib.jl").write_text(
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
    (project / "main.jl").write_text(
        """
function make()
    return Box{Float64, 2}(1.0)
end
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    inst = {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, "INSTANTIATES")
    }
    assert any(src.endswith(".make") and dst.endswith(".Box") for src, dst in inst), (
        inst
    )


def test_macrocall_creates_edge_to_macro(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "julia_calls"
    project.mkdir()
    (project / "main.jl").write_text(
        """
macro check(cond)
    :()
end

function run()
    @check(true)
    nothing
end
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    hits = _calls_to(mock_ingestor, ".check")
    assert any(src.endswith(".run") for src, _ in hits), _calls(mock_ingestor)


def test_macro_namespace_gating(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """A bare `f(...)` call must not resolve to a macro named `f`, and a
    `@f` macro call must not resolve to a plain function named `f`."""
    project = temp_repo / "julia_calls"
    project.mkdir()
    (project / "main.jl").write_text(
        """
macro f(x)
    :()
end

function g(x)
    return x
end

function call_macro()
    @f(1)
    nothing
end

function call_fn()
    g(2)
    nothing
end
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    edges = _calls(mock_ingestor)
    # @f(1) must target the macro `f`, and only from call_macro.
    f_hits = _calls_to(mock_ingestor, ".f")
    assert any(src.endswith(".call_macro") for src, _ in f_hits), edges
    assert not any(src.endswith(".call_fn") for src, _ in f_hits), edges
    # g(2) must target the function `g`, and only from call_fn.
    g_hits = _calls_to(mock_ingestor, ".g")
    assert any(src.endswith(".call_fn") for src, _ in g_hits), edges
    assert not any(src.endswith(".call_macro") for src, _ in g_hits), edges


def test_broadcast_call(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    project = temp_repo / "julia_calls"
    project.mkdir()
    (project / "main.jl").write_text(
        """
transform(x) = x + 1

function apply_all(xs)
    return transform.(xs)
end
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    hits = _calls_to(mock_ingestor, ".transform")
    assert any(src.endswith(".apply_all") for src, _ in hits), _calls(mock_ingestor)


def test_do_block_call(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    project = temp_repo / "julia_calls"
    project.mkdir()
    (project / "main.jl").write_text(
        """
function for_each(xs, f)
    for x in xs
        f(x)
    end
    nothing
end

function use(xs)
    for_each(xs do x
        println(x)
    end)
    nothing
end
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    hits = _calls_to(mock_ingestor, ".for_each")
    assert any(src.endswith(".use") for src, _ in hits), _calls(mock_ingestor)


def test_cross_file_call_via_trie(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    project = temp_repo / "julia_calls"
    project.mkdir()
    (project / "lib.jl").write_text(
        """
function shared_helper(x)
    return x * 10
end
""",
        encoding="utf-8",
    )
    (project / "main.jl").write_text(
        """
function client(x)
    return shared_helper(x) + 1
end
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    hits = _calls_to(mock_ingestor, ".shared_helper")
    assert any(src.endswith(".client") for src, _ in hits), _calls(mock_ingestor)


def test_signature_head_is_not_a_call(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """Definition heads (explicit signatures and concise LHS) are call-shaped
    nodes but must not produce self-CALLS edges."""
    project = temp_repo / "julia_calls"
    project.mkdir()
    (project / "main.jl").write_text(
        """
function add(a, b)
    return a + b
end

sub(a, b) = a - b
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    edges = _calls(mock_ingestor)
    assert not any(src == dst for src, dst in edges), edges


def test_macro_and_function_same_name(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """A macro and a function share ONE name in one module (the @-namespace
    is the only discriminator). Pinned behavior (issue #1882 review): a bare
    `f(x)` call binds the FUNCTION and a `@f(x, 2)` invocation binds the
    MACRO. The old gate dropped the bare call entirely: the resolver
    returned the macro twin, the namespaces disagreed, and the call
    vanished."""
    project = temp_repo / "julia_macro_collision"
    project.mkdir()
    (project / "main.jl").write_text(
        """
macro f(a, b)
    return :($(a) * $(b))
end

function f(x)
    return x + 1
end

function user(x)
    y = f(x)
    z = @f(x, 2)
    return y + z
end
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    calls = _calls(mock_ingestor)
    module_qn = f"{project.name}.main"
    macro_qn = f"{module_qn}.f"
    # The invocation reaches the macro twin (registered first, keeps the
    # natural qn).
    assert any(s.endswith(".user") and d == macro_qn for s, d in calls), calls
    # The bare call reaches the function twin via its duplicate variant.
    assert any(
        s.endswith(".user") and d.startswith(f"{macro_qn}@") for s, d in calls
    ), calls


def test_top_level_anonymous_arrow_calls(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """`ys = map(x -> helper(x), xs)` at module level: the anonymous arrow
    gets no caller node, so its calls stay attributed to the MODULE. The old
    top-level filter excluded the arrow body unconditionally and dropped the
    helper edge entirely (issue #1882 review)."""
    project = temp_repo / "julia_anon_arrow"
    project.mkdir()
    (project / "main.jl").write_text(
        """
function helper(x)
    return x + 1
end

ys = map(x -> helper(x), xs)
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    calls = _calls(mock_ingestor)
    module_qn = f"{project.name}.main"
    assert (module_qn, f"{module_qn}.helper") in calls, calls
    fn_qns = {d for _, d in calls}
    assert f"{module_qn}.ys" not in fn_qns, fn_qns


def test_nested_function_calls_owned_by_inner(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """A call inside a nested function belongs to the nested function only;
    the outer one must not keep a second copy of it (issue #1882 review:
    Julia kept the whole nested subtree, unlike Rust's owned-by path)."""
    project = temp_repo / "julia_nested_fn"
    project.mkdir()
    (project / "main.jl").write_text(
        """
function other()
    return 1
end

function outer()
    function inner()
        return other()
    end
    return inner()
end
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    calls = _calls(mock_ingestor)
    module_qn = f"{project.name}.main"
    # Julia is flat: the nested fn registers as `main.inner`, not nested.
    assert (f"{module_qn}.inner", f"{module_qn}.other") in calls, calls
    assert (f"{module_qn}.outer", f"{module_qn}.inner") in calls, calls
    assert (f"{module_qn}.outer", f"{module_qn}.other") not in calls, calls


def test_inner_constructor_calls_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """An inner constructor's body emits a CALLS edge from the registered
    constructor qn (issue #1882 review: the class pass could not recover
    Julia's nameless definition node and skipped the body, so the
    constructor's calls attributed to nothing)."""
    project = temp_repo / "julia_ctor_calls"
    project.mkdir()
    (project / "main.jl").write_text(
        """
function normalize(v)
    return v / 2
end

struct Arr
    x
    function Arr(v)
        new(normalize(v))
    end
end
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    calls = _calls(mock_ingestor)
    module_qn = f"{project.name}.main"
    assert (f"{module_qn}.Arr.Arr", f"{module_qn}.normalize") in calls, calls


def test_nested_arrow_calls_owned_by_inner(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """A NAMED arrow's body owns its calls: the arrow has exactly two named
    children (parameter, body; `->` is anonymous), so the body-span guard
    must accept the two-child shape, or the enclosing function double-owns
    the call (issue #1882 review: the `>= 3` guard never matched)."""
    project = temp_repo / "julia_nested_arrow"
    project.mkdir()
    (project / "main.jl").write_text(
        """
function helper(x)
    return x + 1
end

function outer(x)
    f = y -> helper(y)
    return f(x)
end
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    hits = _calls_to(mock_ingestor, ".helper")
    srcs = {src for src, _ in hits}
    assert any(src.endswith(".f") for src in srcs), hits
    assert not any(src.endswith(".outer") for src in srcs), hits


def test_nested_concise_method_calls_owned_by_inner(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """A concise method's body (the right side of `f(x) = ...`) owns its
    calls: the assignment's named children are [call head, `=`, body], so
    the span guard must take the right side, or the enclosing function
    double-owns the call (issue #1882 review)."""
    project = temp_repo / "julia_nested_concise"
    project.mkdir()
    (project / "main.jl").write_text(
        """
function helper(x)
    return x + 1
end

function outer(x)
    g(y) = helper(y)
    return g(x)
end
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    hits = _calls_to(mock_ingestor, ".helper")
    srcs = {src for src, _ in hits}
    assert any(src.endswith(".g") for src in srcs), hits
    assert not any(src.endswith(".outer") for src in srcs), hits


def test_macro_invocation_does_not_fan_out_to_function(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """A macro invocation binds ONLY the macro: the duplicate bucket of a
    same-named macro/function pair fans the generic edge out onto both
    twins, so the final target list must be filtered by the macro registry
    (issue #1882 review: the gate validated only the first candidate)."""
    project = temp_repo / "julia_macro_fanout"
    project.mkdir()
    (project / "main.jl").write_text(
        """
macro f(x)
    return x
end

function f(x)
    return x + 1
end

function user_macro(x)
    @f(x)
    return x
end
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    calls = _calls(mock_ingestor)
    module_qn = f"{project.name}.main"
    src = f"{module_qn}.user_macro"
    targets = {dst for (s, dst) in calls if s == src}
    # Exactly one twin: the macro (the function is registered first in the
    # query order only if it precedes the macro; assert on the count and on
    # the absence of the @-suffixed free-function variant in BOTH
    # directions' over-emission).
    assert len(targets) == 1, calls
    assert not any(
        t.endswith(f".f@{line}") for t in targets for line in range(1, 30)
    ), calls


def test_struct_constructor_call_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """`Point(v)` construction runs the inner same-name constructor: with
    the type owning the natural qn, the class branch must redirect a CALLS
    edge to `mod.Point.Point` (not only INSTANTIATES), like Java/C#
    (issue #1882 review: the constructor was left edge-free)."""
    project = temp_repo / "julia_ctor_edge"
    project.mkdir()
    (project / "main.jl").write_text(
        """
struct Point
    x::Float64
    function Point(v)
        new(v)
    end
end

function make(v)
    return Point(v)
end
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    calls = _calls(mock_ingestor)
    module_qn = f"{project.name}.main"
    assert (f"{module_qn}.make", f"{module_qn}.Point.Point") in calls, calls
    inst = {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, "INSTANTIATES")
    }
    assert (f"{module_qn}.make", f"{module_qn}.Point") in inst, inst


def test_shadowing_function_wins_constructor_call(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """Julia call syntax dispatches in the FUNCTION namespace: with a free
    function shadowing the type's name, `Arr(x)` calls the function twin,
    so the CALLS edge goes to it, not the inner constructor (issue #1882
    review)."""
    project = temp_repo / "julia_shadow_call"
    project.mkdir()
    (project / "main.jl").write_text(
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

function make(x)
    return Arr(x)
end
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    calls = _calls(mock_ingestor)
    module_qn = f"{project.name}.main"
    src = f"{module_qn}.make"
    targets = {
        dst
        for (s, dst) in calls
        if s == src and (dst.endswith(".Arr") or ".Arr@" in dst)
    }
    assert any(".Arr@" in dst for dst in targets), calls
    assert not any(dst.endswith(".Arr.Arr") for dst in targets), calls


def test_nested_typed_concise_method_calls_owned_by_inner(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """A typed concise method `g(y)::Int = helper(y)` nested in a function
    is an attributable caller of its own: the body-span pass must
    recognize the typed/where-wrapped head, or the enclosing function
    double-owns the call (issue #1882 review: only a bare call_expression
    head was accepted)."""
    project = temp_repo / "julia_typed_nested"
    project.mkdir()
    (project / "main.jl").write_text(
        """
function helper(x)
    return x + 1
end

function outer(x)
    g(y)::Int = helper(y)
    return g(x)
end
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    hits = _calls_to(mock_ingestor, ".helper")
    srcs = {src for src, _ in hits}
    assert any(src.endswith(".g") for src in srcs), hits
    assert not any(src.endswith(".outer") for src in srcs), hits


def test_shadowing_function_call_no_instantiates(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """With a free function shadowing the type's name, `Point()` dispatches
    to the function (the FUNCTION namespace wins): the class branch must
    not ALSO emit INSTANTIATES for the same call (issue #1882 review)."""
    project = temp_repo / "julia_shadow_inst"
    project.mkdir()
    (project / "main.jl").write_text(
        """
struct Point
    x::Int
end

Point() = Point(0)

function user()
    return Point()
end
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    module_qn = f"{project.name}.main"
    src = f"{module_qn}.user"
    calls = _calls(mock_ingestor)
    assert any(s == src and ".Point@" in dst for s, dst in calls), calls
    inst = {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, "INSTANTIATES")
    }
    assert (src, f"{module_qn}.Point") not in inst, inst


def test_selected_import_exact_member(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """`using .api: alpha` binds the member inside the selected module;
    a same-named function in another imported module must not soak the
    edge through the name trie (issue #1882 review: the trie tie-break
    picked the decoy)."""
    project = temp_repo / "julia_selected_member"
    project.mkdir()
    (project / "aaa.jl").write_text("module aaa\nalpha() = 9\nend\n", encoding="utf-8")
    (project / "api.jl").write_text("module api\nalpha() = 7\nend\n", encoding="utf-8")
    (project / "main.jl").write_text(
        "using .aaa\nusing .api: alpha\nf() = alpha()\n", encoding="utf-8"
    )

    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    prefix = f"{project.name}."
    edges = _calls_with_resolution(mock_ingestor)
    assert (
        f"{prefix}main.f",
        f"{prefix}api.api.alpha",
        "exact",
    ) in edges, edges


def test_struct_macro_collision_single_ctor_not_overload(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """`struct Point` plus `macro Point` leaves ONE callable constructor
    target; the edge resolution must not inherit `overload` from the
    pre-filter duplicate bucket (issue #1882 review: consumers of exact
    edges discarded the unambiguous edge)."""
    project = temp_repo / "julia_macro_ctor"
    project.mkdir()
    (project / "point.jl").write_text(
        """
module P
struct Point
    x::Int
    Point(x::Int) = new(x)
end
macro Point()
    :();
end
f() = Point(1)
end
""",
        encoding="utf-8",
    )

    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    prefix = f"{project.name}.point"
    target = f"{prefix}.P.Point.Point"
    edges = {
        (src, dst, res)
        for (src, dst, res) in _calls_with_resolution(mock_ingestor)
        if dst == target
    }
    assert len(edges) == 1, edges
    src, _, resolution = next(iter(edges))
    assert src == f"{prefix}.P.f", edges
    assert resolution != "overload", edges


def test_plain_call_not_linked_to_same_name_macro(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """A plain `Point(...)` call must not reach a same-named macro: the
    class branch's duplicate bucket contains both twins, and the
    constructor redirection must filter the macro twin out (issue #1882
    review: macros are stored as NodeType.FUNCTION)."""
    project = temp_repo / "julia_macro_ctor"
    project.mkdir()
    (project / "main.jl").write_text(
        """
struct Point
    x::Int
    function Point(x)
        new(x)
    end
end

macro Point()
    return :nothing
end

function user()
    return Point(1)
end
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    module_qn = f"{project.name}.main"
    src = f"{module_qn}.user"
    calls = _calls(mock_ingestor)
    targets = {dst for s, dst in calls if s == src}
    assert f"{module_qn}.Point.Point" in targets, calls
    assert not any(".Point@" in dst for dst in targets), calls
