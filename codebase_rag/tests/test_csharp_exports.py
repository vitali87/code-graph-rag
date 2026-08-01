# C# Phase 4: is_exported follows C# visibility (public/internal/protected
# are API surface; a member with no visibility modifier is private).
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_node_names, get_nodes, run_updater
from codebase_rag.types_defs import NodeType

SKIP = "c_sharp"


@pytest.fixture
def csharp_project(temp_repo: Path) -> Path:
    project = temp_repo / "csharp_exports"
    project.mkdir()
    return project


def _exported_by_suffix(mock_ingestor: MagicMock) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for label in (NodeType.METHOD, NodeType.FUNCTION):
        for call in get_nodes(mock_ingestor, label):
            props = call[0][1]
            result[props["qualified_name"]] = props.get("is_exported", False)
    return result


def test_visibility_drives_is_exported(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "V.cs").write_text(
        """
namespace N;
public class C {
    public void Pub() {}
    internal void Intern() {}
    protected void Prot() {}
    private void Priv() {}
    void Implicit() {}
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    exported = _exported_by_suffix(mock_ingestor)

    def flag(suffix: str) -> bool:
        return next(v for qn, v in exported.items() if qn.endswith(suffix))

    assert flag("N.C.Pub") is True
    assert flag("N.C.Intern") is True
    assert flag("N.C.Prot") is True
    assert flag("N.C.Priv") is False
    # No visibility modifier on a class member defaults to private.
    assert flag("N.C.Implicit") is False


def test_interface_members_are_implicitly_public(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # C# interface members carry NO visibility modifier and are implicitly
    # public: they ARE the type's API surface. The modifier scan alone
    # leaves them is_exported=False, which turned every interface member
    # into a dead-code candidate (all 190 findings of the first Polly
    # dead-code dogfood, dominated by IAsyncPolicy.ExecuteAsync overloads
    # and IPolicyWrap properties).
    (csharp_project / "I.cs").write_text(
        """
namespace N;
public interface IPolicy {
    int Execute(int x);
    int Retries { get; }
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    exported = _exported_by_suffix(mock_ingestor)

    def flag(suffix: str) -> bool:
        return next(v for qn, v in exported.items() if qn.endswith(suffix))

    assert flag("N.IPolicy.Execute(int)") is True
    assert flag("N.IPolicy.Retries") is True


def test_explicit_interface_implementations_are_exported(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    # An explicit interface implementation (`IThing IThing.WithKey(...)`)
    # carries no modifier and lives in a class, but it is invocable from
    # outside through the interface -- API surface, not a private member
    # (Polly's AsyncPolicy `IAsyncPolicy.WithPolicyKey` and the
    # Context.Dictionary `IDictionary.Keys` family, all flagged dead).
    (csharp_project / "E.cs").write_text(
        """
namespace N;
public interface IThing {
    IThing WithKey(string k);
}
public class C : IThing {
    IThing IThing.WithKey(string k) => this;
    void Plain() {}
}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    exported = _exported_by_suffix(mock_ingestor)

    def flag(suffix: str) -> bool:
        return next(v for qn, v in exported.items() if qn.endswith(suffix))

    assert flag("N.C.WithKey(string)") is True
    # A plain modifier-less class member stays private.
    assert flag("N.C.Plain") is False


def _class_exported(mock_ingestor: MagicMock) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for call in get_nodes(mock_ingestor, NodeType.CLASS):
        props = call[0][1]
        result[props["qualified_name"]] = props.get("is_exported", False)
    return result


def test_top_level_type_defaults_internal_nested_defaults_private(
    csharp_project: Path, mock_ingestor: MagicMock
) -> None:
    (csharp_project / "T.cs").write_text(
        """
namespace N;
class TopLevel { class Nested {} }
public class Exposed {}
""",
        encoding="utf-8",
    )
    run_updater(csharp_project, mock_ingestor, skip_if_missing=SKIP)

    exported = _class_exported(mock_ingestor)

    def flag(suffix: str) -> bool:
        return next(v for qn, v in exported.items() if qn.endswith(suffix))

    # A top-level type with no modifier is internal (API surface); a nested
    # type with no modifier is private; an explicit `public` is exported.
    assert flag("N.TopLevel") is True
    assert flag("N.TopLevel.Nested") is False
    assert flag("N.Exposed") is True
    # Sanity: the nested type actually registered.
    assert any(
        qn.endswith("N.TopLevel.Nested")
        for qn in get_node_names(mock_ingestor, NodeType.CLASS)
    )
