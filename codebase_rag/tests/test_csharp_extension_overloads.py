"""An arity-ambiguous call to an overloaded extension method calls the set.

Two `Ext(this Calc c, ...)` overloads of one static class, both matching the
call's argument count, were refused as if they were a name colliding across
static classes, so `c.Ext(5)` had no CALLS edge at all, while the same
ambiguity among a class's own overloads fans out to every candidate (issue
#2839).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_relationships, run_updater

SKIP = "c_sharp"

_ISSUE = """\
namespace Demo;

internal class Calc
{
    internal int Add(int a) { return a; }
    internal int Add(string a) { return 0; }
    internal int CallInst() { return Add(5); }
}

internal static class Exts
{
    internal static int Ext(this Calc c, int a) { return a; }
    internal static int Ext(this Calc c, string a) { return 0; }
}

internal class App
{
    internal int CallExt(Calc c) { return c.Ext(5); }
}
"""


def _class_and_method(qn: str) -> str:
    # Split off the signature first: a qualified parameter type
    # (`Ext(N.Calc, int)`) has dots of its own.
    base, _, signature = qn.partition("(")
    return ".".join(base.rsplit(".", 2)[-2:]) + "(" + signature


def _callees(mock_ingestor: MagicMock, caller_suffix: str) -> set[str]:
    return set(_verdicts(mock_ingestor, caller_suffix))


def _verdicts(mock_ingestor: MagicMock, caller_suffix: str) -> dict[str, str]:
    return {
        _class_and_method(str(c.args[2][2])): str(
            (c.kwargs.get("properties") or {}).get(cs.KEY_RESOLUTION)
        )
        for c in get_relationships(mock_ingestor, "CALLS")
        if str(c.args[0][2]).split("(", 1)[0].endswith(caller_suffix)
    }


@pytest.fixture
def project(temp_repo: Path) -> Path:
    root = temp_repo / "csext"
    root.mkdir()
    return root


def test_the_overload_set_receives_the_call(
    project: Path, mock_ingestor: MagicMock
) -> None:
    (project / "Ext.cs").write_text(_ISSUE, encoding="utf-8")
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    assert _callees(mock_ingestor, ".App.CallExt") == {
        "Exts.Ext(Calc, int)",
        "Exts.Ext(Calc, string)",
    }
    # The instance overloads the issue compares against are unchanged.
    assert _callees(mock_ingestor, ".Calc.CallInst") == {
        "Calc.Add(int)",
        "Calc.Add(string)",
    }


def test_a_name_colliding_across_static_classes_is_not_fanned_out(
    project: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: two static classes each declaring Ext(this Calc, int) are a
    # genuine "which class?" ambiguity, not an overload set. The extension
    # lookup leaves it to the later name fallback, as on main, and the
    # overload fan-out never joins the two classes.
    (project / "Ext.cs").write_text(
        """\
namespace Demo;
internal class Calc { }
internal static class ExtsA { internal static int Ext(this Calc c, int a) { return a; } }
internal static class ExtsB { internal static int Ext(this Calc c, int a) { return a; } }
internal class App { internal int CallExt(Calc c) { return c.Ext(5); } }
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    bound = {c for c in _callees(mock_ingestor, ".App.CallExt") if c.startswith("Exts")}
    assert len(bound) <= 1, bound


def test_an_overload_for_another_receiver_is_not_called(
    project: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: a same-class, same-arity overload extending ANOTHER type is
    # not part of the set a Calc receiver can bind.
    (project / "Ext.cs").write_text(
        """\
namespace Demo;
internal class Calc { }
internal class Other { }
internal static class Exts
{
    internal static int Ext(this Calc c, int a) { return a; }
    internal static int Ext(this Calc c, string a) { return 0; }
    internal static int Ext(this Other o, int a) { return a; }
}
internal class App { internal int CallExt(Calc c) { return c.Ext(5); } }
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    assert _callees(mock_ingestor, ".App.CallExt") == {
        "Exts.Ext(Calc, int)",
        "Exts.Ext(Calc, string)",
    }


def test_a_differently_spelled_receiver_stays_in_the_set(
    project: Path, mock_ingestor: MagicMock
) -> None:
    # `this N.Calc` and `this Calc` declared in namespace N name the same
    # type, so a `new N.Calc()` receiver matches both overloads. Comparing
    # the receivers as written dropped the one `Ext(5)` actually runs.
    (project / "Ext.cs").write_text(
        """\
namespace N;
internal class Calc { }
internal static class Exts
{
    internal static int Ext(this N.Calc c, int a) { return a; }
    internal static int Ext(this Calc c, string a) { return 0; }
}
internal class App { internal int CallExt() { return new N.Calc().Ext(5); } }
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    assert _callees(mock_ingestor, ".App.CallExt") == {
        "Exts.Ext(N.Calc, int)",
        "Exts.Ext(Calc, string)",
    }


def test_the_overload_set_is_labelled_overload(
    project: Path, mock_ingestor: MagicMock
) -> None:
    # Arity chose the bound overload no more than the others, so no edge of
    # the set may claim `exact`: `--min-resolution exact` would read each as
    # a proven call. A lone extension the call binds stays exact.
    (project / "Ext.cs").write_text(
        """\
namespace Demo;
internal class Calc { }
internal static class Exts
{
    internal static int Ext(this Calc c, int a) { return a; }
    internal static int Ext(this Calc c, string a) { return 0; }
    internal static int Solo(this Calc c, int a) { return a; }
}
internal class App
{
    internal int CallExt(Calc c) { return c.Ext(5); }
    internal int CallSolo(Calc c) { return c.Solo(5); }
}
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    assert _verdicts(mock_ingestor, ".App.CallExt") == {
        "Exts.Ext(Calc, int)": cs.EdgeResolution.OVERLOAD,
        "Exts.Ext(Calc, string)": cs.EdgeResolution.OVERLOAD,
    }
    assert _verdicts(mock_ingestor, ".App.CallSolo") == {
        "Exts.Solo(Calc, int)": cs.EdgeResolution.EXACT,
    }


def test_explicit_type_arguments_skip_a_non_generic_overload(
    project: Path, mock_ingestor: MagicMock
) -> None:
    # C# refuses type arguments on a non-generic method, so `c.Ext<int>(5)`
    # can only run the generic overload. Without them, type inference lets
    # `c.Ext(5)` bind either, and the call still reaches both.
    (project / "Ext.cs").write_text(
        """\
namespace Demo;
internal class Calc { }
internal static class Exts
{
    internal static int Ext<T>(this Calc c, T a) { return 0; }
    internal static int Ext(this Calc c, object a) { return 0; }
}
internal class App
{
    internal int CallGeneric(Calc c) { return c.Ext<int>(5); }
    internal int CallPlain(Calc c) { return c.Ext(5); }
}
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    assert _callees(mock_ingestor, ".App.CallGeneric") == {"Exts.Ext(Calc, T)"}
    assert _callees(mock_ingestor, ".App.CallPlain") == {
        "Exts.Ext(Calc, T)",
        "Exts.Ext(Calc, object)",
    }


def test_explicit_type_arguments_need_as_many_type_parameters(
    project: Path, mock_ingestor: MagicMock
) -> None:
    # `c.Ext<int>(5)` passes one type argument, so `Ext<T, U>` cannot receive
    # it, and `c.Ext<int, string>("")` is the reverse: each call reaches only
    # the overload declaring as many type parameters as it passes.
    (project / "Ext.cs").write_text(
        """\
namespace Demo;
internal class Calc { }
internal static class Exts
{
    internal static int Ext<T>(this Calc c, T a) { return 0; }
    internal static int Ext<T, U>(this Calc c, U a) { return 0; }
}
internal class App
{
    internal int CallOne(Calc c) { return c.Ext<int>(5); }
    internal int CallTwo(Calc c) { return c.Ext<int, string>(""); }
}
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    assert _callees(mock_ingestor, ".App.CallOne") == {"Exts.Ext(Calc, T)"}
    assert _callees(mock_ingestor, ".App.CallTwo") == {"Exts.Ext(Calc, U)"}


_CHAIN_SOURCE = """\
namespace Demo;
internal class Calc { }
internal class IntResult { internal int Run() { return 0; } }
internal class StringResult { internal int Run() { return 0; } }
internal static class Exts
{
    internal static IntResult Ext(this Calc c, int a) { return new IntResult(); }
    internal static %s Ext(this Calc c, string a) { return new %s(); }
}
internal class App
{
    internal int Chain(Calc c) { return c.Ext("s").Run(); }
}
"""


def test_a_chained_call_on_an_overload_set_with_mixed_returns_is_not_typed(
    project: Path, mock_ingestor: MagicMock
) -> None:
    # `c.Ext("s")` binds one overload of the set but may run either, and they
    # return different types, so neither type may decide `.Run()`: binding
    # it to the int overload's `IntResult.Run` missed `StringResult.Run`.
    (project / "Ext.cs").write_text(
        _CHAIN_SOURCE % ("StringResult", "StringResult"), encoding="utf-8"
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    runs = {
        callee.split("(", 1)[0]
        for callee in _callees(mock_ingestor, ".App.Chain")
        if ".Run" in callee
    }
    assert runs != {"IntResult.Run"}


def test_a_chained_call_on_an_overload_set_with_one_return_type_is_typed(
    project: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: when every overload returns the same type, it still types
    # the next hop.
    (project / "Ext.cs").write_text(
        _CHAIN_SOURCE % ("IntResult", "IntResult"), encoding="utf-8"
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    runs = {
        callee.split("(", 1)[0]
        for callee in _callees(mock_ingestor, ".App.Chain")
        if ".Run" in callee
    }
    assert runs == {"IntResult.Run"}


def test_a_chained_call_on_mixed_returns_does_not_switch_to_an_extension(
    project: Path, mock_ingestor: MagicMock
) -> None:
    # `IntResult` has an instance `Run()` and there is also an extension
    # `Run(this IntResult)`. The mixed-return set leaves `c.Ext(5)` untyped,
    # and that must hold on the extension path too: reading `IntResult` there
    # bound the extension, which C# never calls while the instance method
    # exists, and lost the instance call.
    (project / "Ext.cs").write_text(
        """\
namespace Demo;
internal class Calc { }
internal class IntResult { internal int Run() { return 0; } }
internal class StringResult { }
internal static class Exts
{
    internal static IntResult Ext(this Calc c, int a) { return new IntResult(); }
    internal static StringResult Ext(this Calc c, string a) { return new StringResult(); }
}
internal static class RunExts
{
    internal static int Run(this IntResult r) { return 1; }
}
internal class App
{
    internal int Chain(Calc c) { return c.Ext(5).Run(); }
}
""",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)

    assert "RunExts.Run(IntResult)" not in _callees(mock_ingestor, ".App.Chain")
