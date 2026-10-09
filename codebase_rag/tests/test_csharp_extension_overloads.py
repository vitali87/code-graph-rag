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


def _callees(mock_ingestor: MagicMock, caller_suffix: str) -> set[str]:
    return {
        str(c.args[2][2]).rsplit(".", 2)[-2]
        + "."
        + str(c.args[2][2]).rsplit(".", 1)[-1]
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
