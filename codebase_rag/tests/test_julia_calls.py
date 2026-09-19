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
