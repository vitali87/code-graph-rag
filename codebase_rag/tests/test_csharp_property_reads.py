# (H) C# property READS: a getter access is a member_access_expression, not an
# (H) invocation, so without a read pass a heavily-read property has zero
# (H) inbound edges and dead-code flags it (Polly's Context.WrappedDictionary
# (H) and ResiliencePipeline<T>.Pipeline).
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater

SKIP = "c_sharp"


@pytest.fixture
def csharp_project(temp_repo: Path) -> Path:
    project = temp_repo / "csharp_prop_reads"
    project.mkdir()
    return project


def _reference_targets(mock_ingestor: MagicMock) -> set[str]:
    return {c.args[2][2] for c in get_relationships(mock_ingestor, "REFERENCES")}


def _call_targets(mock_ingestor: MagicMock) -> set[str]:
    return {c.args[2][2] for c in get_relationships(mock_ingestor, "CALLS")}


def test_receiver_position_property_read_is_referenced(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) `WrappedDictionary.Keys` reads the property as the RECEIVER of a
    # (H) member access; the read must land a REFERENCES edge on the property
    # (H) node (REFERENCES, not CALLS: the call graph stays invocation-only).
    (csharp_project / "R.cs").write_text(
        """
namespace N;
public class Ctx {
    private int Size { get; }
    public string Show() => Size.ToString();
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    refs = _reference_targets(mock_ingestor)
    assert any(t.endswith("N.Ctx.Size") for t in refs), refs
    assert not any(t.endswith("N.Ctx.Size") for t in _call_targets(mock_ingestor))


def test_cast_wrapped_property_read_is_referenced(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) `((IDictionary<...>)WrappedDictionary).Clear()` wraps the property
    # (H) read in a cast; the cast VALUE is still a read of the property.
    (csharp_project / "C.cs").write_text(
        """
namespace N;
public class Ctx {
    private object Bag { get; }
    public string Dump() => ((System.Collections.IEnumerable)Bag).ToString();
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    refs = _reference_targets(mock_ingestor)
    assert any(t.endswith("N.Ctx.Bag") for t in refs), refs


def test_invocation_receiver_property_read_is_referenced(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) `Pipeline.Execute(...)`: the invocation edge targets Execute, but the
    # (H) receiver position is itself a READ of the Pipeline property that must
    # (H) keep the property reachable.
    (csharp_project / "I.cs").write_text(
        """
namespace N;
public class Widget { public void Area() {} }
public class Pipe {
    private Widget Inner { get; }
    public void Go() { Inner.Area(); }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    refs = _reference_targets(mock_ingestor)
    assert any(t.endswith("N.Pipe.Inner") for t in refs), refs


def test_local_shadow_and_methods_are_not_referenced(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) Guards: a local variable shadowing a property name is NOT a property
    # (H) read, and a same-name METHOD receiver stays out of the read pass.
    (csharp_project / "G.cs").write_text(
        """
namespace N;
public class Guard {
    private int Size { get; }
    public string Show() {
        var Size = 3;
        return Size.ToString();
    }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    refs = _reference_targets(mock_ingestor)
    assert not any(t.endswith("N.Guard.Size") for t in refs), refs


def test_explicit_this_property_read_is_referenced(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) `this.Size` is always the member (locals can never shadow a
    # (H) this-qualified read), so the read pass must record the NAME field
    # (H) when the receiver is `this`.
    (csharp_project / "T.cs").write_text(
        """
namespace N;
public class Ctx {
    private int Size { get; }
    public string Show() => this.Size.ToString();
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    refs = _reference_targets(mock_ingestor)
    assert any(t.endswith("N.Ctx.Size") for t in refs), refs


def test_nested_local_function_shadow_does_not_suppress_outer_read(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) A local declared inside a NESTED local function must not shadow the
    # (H) outer body's property read: the read walk skips nested function
    # (H) scopes, so the shadow walk must skip them symmetrically.
    (csharp_project / "S.cs").write_text(
        """
namespace N;
public class Outer {
    private int Size { get; }
    public string Show() {
        string Local() {
            var Size = 3;
            return Size.ToString();
        }
        return Local() + Size.ToString();
    }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    refs = _reference_targets(mock_ingestor)
    assert any(t.endswith("N.Outer.Size") for t in refs), refs


def test_simple_lambda_parameter_shadows_property(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) A simple (untyped) lambda parameter is a bare identifier, not a
    # (H) `parameter` node; it still shadows a same-name property for reads
    # (H) inside the lambda body, which the read walk descends into.
    (csharp_project / "L.cs").write_text(
        """
namespace N;
public class Lam {
    private int Size { get; }
    public System.Func<int, string> Make() => Size => Size.ToString();
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    refs = _reference_targets(mock_ingestor)
    assert not any(t.endswith("N.Lam.Size") for t in refs), refs


def test_postfix_wrapped_cast_receiver_resolves(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) The null-forgiving postfix can sit OUTSIDE the parenthesized cast
    # (H) (`((Component)s)!.Reload()`); the receiver unwrap must peel
    # (H) interleaved postfix/paren wrappers to a fixpoint before the cast.
    (csharp_project / "P.cs").write_text(
        """
namespace N;
public class Component {
    public void Reload() {}
    public void Wire(object s) { ((Component)s)!.Reload(); }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    calls = {c.args[2][2] for c in get_relationships(mock_ingestor, "CALLS")}
    assert any(t.endswith("N.Component.Reload") for t in calls), calls
