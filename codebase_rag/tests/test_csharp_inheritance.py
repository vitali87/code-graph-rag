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
