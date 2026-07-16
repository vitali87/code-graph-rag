# (H) C# Phase 2: INHERITS (base class, interface-extends-interface) and
# (H) IMPLEMENTS (class/struct/record implementing interfaces).
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater

SKIP = "c_sharp"


@pytest.fixture
def csharp_project(temp_repo: Path) -> Path:
    project = temp_repo / "csharp_inherit"
    project.mkdir()
    return project


def _pairs(mock_ingestor: MagicMock, rel_type: str) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, rel_type)
    }


def _has(pairs: set[tuple[str, str]], child_suffix: str, parent_suffix: str) -> bool:
    return any(
        ch.endswith(child_suffix) and pa.endswith(parent_suffix) for ch, pa in pairs
    )


def test_base_class_emits_inherits(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "Base.cs").write_text(
        """
namespace N;
public class Base { }
public class Derived : Base { }
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    inherits = _pairs(mock_ingestor, "INHERITS")
    assert _has(inherits, "N.Derived", "N.Base"), inherits


def test_interfaces_emit_implements_not_inherits(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "Shapes.cs").write_text(
        """
namespace N;
public class Base { }
public interface IShape { }
public interface IColor { }
public class Derived : Base, IShape, IColor { }
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    inherits = _pairs(mock_ingestor, "INHERITS")
    implements = _pairs(mock_ingestor, "IMPLEMENTS")
    # (H) The base class is inheritance; the interfaces are implementation.
    assert _has(inherits, "N.Derived", "N.Base"), inherits
    assert _has(implements, "N.Derived", "N.IShape"), implements
    assert _has(implements, "N.Derived", "N.IColor"), implements
    # (H) An interface must never be recorded as a base class.
    assert not _has(inherits, "N.Derived", "N.IShape"), inherits


def test_interface_extends_interface_is_inherits(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "Extend.cs").write_text(
        """
namespace N;
public interface IShape { }
public interface IColor { }
public interface IExtended : IShape, IColor { }
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    inherits = _pairs(mock_ingestor, "INHERITS")
    assert _has(inherits, "N.IExtended", "N.IShape"), inherits
    assert _has(inherits, "N.IExtended", "N.IColor"), inherits


def test_struct_implements_interface(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "Val.cs").write_text(
        """
namespace N;
public interface IShape { }
public struct Val : IShape { }
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    implements = _pairs(mock_ingestor, "IMPLEMENTS")
    assert _has(implements, "N.Val", "N.IShape"), implements


def test_qualified_generic_base_strips_type_arguments(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "Gen.cs").write_text(
        """
namespace N;
public class C : System.Collections.Generic.List<int> { }
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    inherits = _pairs(mock_ingestor, "INHERITS")
    # (H) The generic type arguments must not leak into the base's qn, or it
    # (H) can never match the registered (generic-free) List type.
    assert _has(inherits, "N.C", "System.Collections.Generic.List"), inherits
    assert not any("<" in parent for _, parent in inherits), inherits


def test_nongeneric_base_pair_resolves_to_generic_sibling(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) C# arity overloading: `class Widget : Widget<object>` names a DIFFERENT
    # (H) type that shares the simple name (the Polly Foo.cs + Foo.TResult.cs
    # (H) layout). Name resolution lands on the declaring type itself, and the
    # (H) self-loop guard must recover the sibling instead of dropping the edge.
    (csharp_project / "Widget.cs").write_text(
        "namespace N;\npublic class Widget : Widget<object> { }\n",
        encoding="utf-8",
    )
    (csharp_project / "Widget.TResult.cs").write_text(
        "namespace N;\npublic class Widget<TResult> { }\n",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    inherits = _pairs(mock_ingestor, "INHERITS")
    assert any(
        ch.endswith("N.Widget") and "TResult" in pa and pa.endswith("N.Widget")
        for ch, pa in inherits
    ), inherits


def test_nongeneric_interface_base_pair_resolves_to_generic_sibling(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) Same arity-pair shape on interfaces (Polly's ITtlStrategy :
    # (H) ITtlStrategy<object>); an interface's bases are INHERITS.
    (csharp_project / "ITtl.cs").write_text(
        "namespace N;\npublic interface ITtl : ITtl<object> { }\n",
        encoding="utf-8",
    )
    (csharp_project / "ITtl.TResult.cs").write_text(
        "namespace N;\npublic interface ITtl<TResult> { }\n",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    inherits = _pairs(mock_ingestor, "INHERITS")
    assert any(
        ch.endswith("N.ITtl") and "TResult" in pa and pa.endswith("N.ITtl")
        for ch, pa in inherits
    ), inherits


def test_arity_pair_across_directories_resolves_project_wide(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) Polly's BrokenCircuitException shape: the generic child lives in one
    # (H) project directory, the non-generic base in another, so the
    # (H) same-package tier finds nothing and the project-wide tier must
    # (H) recover the single other declaration.
    core = csharp_project / "Core"
    legacy = csharp_project / "Legacy"
    core.mkdir()
    legacy.mkdir()
    (core / "Broken.cs").write_text(
        "namespace N.Core;\npublic class Broken { }\n",
        encoding="utf-8",
    )
    (legacy / "Broken.TResult.cs").write_text(
        "namespace N.Legacy;\npublic class Broken<TResult> : Broken { }\n",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    inherits = _pairs(mock_ingestor, "INHERITS")
    assert any(
        "TResult" in ch and ch.endswith("Broken") and ".Core." in pa
        for ch, pa in inherits
    ), inherits


def test_same_file_arity_pair_child_first_recovers_variant_sibling(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) Issue #764 shape 1: both members of the arity pair in ONE file, child
    # (H) declared first. The two types collide on natural qn (the second gets a
    # (H) DUP_QN_MARKER variant), the base resolves to the child itself, and the
    # (H) unique other same-scope variant IS the written sibling.
    (csharp_project / "Ttl.cs").write_text(
        "namespace N;\n"
        "public interface ITtl : ITtl<object> { }\n"
        "public interface ITtl<TResult> { int GetTtl(); }\n",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    inherits = _pairs(mock_ingestor, "INHERITS")
    assert any(
        ch.endswith("N.ITtl") and pa.split("@")[0].endswith("N.ITtl") and pa != ch
        for ch, pa in inherits
    ), inherits


def test_same_file_arity_pair_generic_child_first_recovers_variant_sibling(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) Issue #764 shape 2 (Polly's ExecuteParameters, nested in a class): the
    # (H) GENERIC member is the child and declares first (bare qn); the
    # (H) non-generic base registers second as the variant.
    (csharp_project / "Exec.cs").write_text(
        "namespace N;\n"
        "public class Outer {\n"
        "    public class ExecuteParameters<T> : ExecuteParameters { }\n"
        "    public class ExecuteParameters { }\n"
        "}\n",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    inherits = _pairs(mock_ingestor, "INHERITS")
    assert any(
        ch.endswith("Outer.ExecuteParameters")
        and pa.split("@")[0].endswith("Outer.ExecuteParameters")
        and pa != ch
        for ch, pa in inherits
    ), inherits


def test_same_file_arity_pair_generic_first_still_resolves(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) Declaration-order regression pin: with the generic FIRST (bare qn),
    # (H) the child registers as the variant and the base already resolves to
    # (H) the bare sibling at parse time; the fix for the child-first order
    # (H) must not disturb this.
    (csharp_project / "Rev.cs").write_text(
        "namespace N;\n"
        "public interface IRev<TResult> { int GetIt(); }\n"
        "public interface IRev : IRev<object> { }\n",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    inherits = _pairs(mock_ingestor, "INHERITS")
    assert any(
        ch.split("@")[0].endswith("N.IRev") and pa.endswith("N.IRev") and pa != ch
        for ch, pa in inherits
    ), inherits


def test_arity_pair_with_partial_generic_sibling_resolves(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) Issue #764 shape 3 (Polly's PredicateBuilder): the non-generic child
    # (H) sits alone in its file; the generic sibling is `partial` across TWO
    # (H) other files, so the sibling recovery sees two candidate qns. The
    # (H) partial group says they are one type; the edge must not be dropped.
    (csharp_project / "Pred.cs").write_text(
        "namespace N;\npublic sealed class Pred : Pred<object> { }\n",
        encoding="utf-8",
    )
    (csharp_project / "Pred.TResult.cs").write_text(
        "namespace N;\npublic partial class Pred<TResult> { public int A() => 1; }\n",
        encoding="utf-8",
    )
    (csharp_project / "Pred.Operators.cs").write_text(
        "namespace N;\npublic partial class Pred<TResult> { public int B() => 2; }\n",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    inherits = _pairs(mock_ingestor, "INHERITS")
    assert any(
        ch.endswith("N.Pred") and ("TResult" in pa or "Operators" in pa)
        for ch, pa in inherits
    ), inherits


def test_enum_underlying_type_is_not_inheritance(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "Color.cs").write_text(
        "namespace N;\npublic enum Color : byte { R, G }\n",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    inherits = _pairs(mock_ingestor, "INHERITS")
    implements = _pairs(mock_ingestor, "IMPLEMENTS")
    # (H) `enum Color : byte` names an underlying integral type, not a base.
    assert not any(ch.endswith("N.Color") for ch, _ in inherits), inherits
    assert not any(ch.endswith("N.Color") for ch, _ in implements), implements
