# Issue #2534: a written base name only ever binds to a TYPE declaration
# (never a same-named property, field or method), and a C# base list is
# looked up in scope order (current namespace, enclosing namespaces, then
# `using` directives) before any project-wide name match, which is kept
# only as a last resort and marked heuristic.
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.parsers.csharp_frontend import CSharpSemanticFacts
from codebase_rag.parsers.frontends import csharp as csharp_fe
from codebase_rag.tests.conftest import get_relationships, run_updater

CSHARP = "c_sharp"

# (child label, child qn, parent label, parent qn, edge properties)
Edge = tuple[str, str, str, str, dict]


@pytest.fixture
def project(temp_repo: Path) -> Path:
    root = temp_repo / "basetargets"
    root.mkdir()
    return root


def _write(root: Path, files: dict[str, str]) -> None:
    for rel_path, text in files.items():
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _edges(mock_ingestor: MagicMock, rel_type: str) -> list[Edge]:
    return [
        (
            str(c.args[0][0]),
            c.args[0][2],
            str(c.args[2][0]),
            c.args[2][2],
            c.args[3] if len(c.args) > 3 and c.args[3] else {},
        )
        for c in get_relationships(mock_ingestor, rel_type)
    ]


def _edges_from(
    mock_ingestor: MagicMock, rel_type: str, child_suffix: str
) -> list[Edge]:
    return [e for e in _edges(mock_ingestor, rel_type) if e[1].endswith(child_suffix)]


def _only_edge_from(mock_ingestor: MagicMock, rel_type: str, child_suffix: str) -> Edge:
    edges = _edges_from(mock_ingestor, rel_type, child_suffix)
    assert len(edges) == 1, edges
    return edges[0]


_CSPROJ = (
    '<Project Sdk="Microsoft.NET.Sdk">\n'
    "  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>\n"
    "</Project>\n"
)

ISSUE_FILES = {
    "src/Handlers.cs": (
        "namespace Acme;\n"
        "public abstract class NotificationHandler<T>\n"
        "{\n"
        "    public abstract void Handle(T value);\n"
        "}\n"
    ),
    "samples/Results.cs": (
        "namespace Acme.Samples;\n"
        "public class RunResults\n"
        "{\n"
        "    public bool NotificationHandler { get; set; }\n"
        "}\n"
    ),
    "tests/HandlerTests.cs": (
        "namespace Acme.Tests;\n"
        "public class PongHandler : NotificationHandler<string>\n"
        "{\n"
        "    public override void Handle(string value) { }\n"
        "}\n"
    ),
}


class TestCSharpBaseNeverBindsToAMember:
    def test_generic_base_skips_same_named_property(
        self, project: Path, mock_ingestor: MagicMock
    ) -> None:
        _write(project, ISSUE_FILES)
        run_updater(project, mock_ingestor, skip_if_missing=CSHARP)

        _, _, label, parent, _ = _only_edge_from(
            mock_ingestor, cs.RelationshipType.INHERITS, "Acme.Tests.PongHandler"
        )
        assert label == cs.NodeLabel.CLASS
        assert parent.endswith("src.Handlers.Acme.NotificationHandler"), parent

    def test_override_of_generic_base_method_is_recorded(
        self, project: Path, mock_ingestor: MagicMock
    ) -> None:
        _write(project, ISSUE_FILES)
        run_updater(project, mock_ingestor, skip_if_missing=CSHARP)

        overrides = _edges_from(
            mock_ingestor, cs.RelationshipType.OVERRIDES, "PongHandler.Handle(string)"
        )
        assert [e[3] for e in overrides] == [
            f"{project.name}.src.Handlers.Acme.NotificationHandler.Handle(T)"
        ], overrides

    def test_base_skips_same_named_method(
        self, project: Path, mock_ingestor: MagicMock
    ) -> None:
        _write(
            project,
            {
                "src/Widget.cs": "namespace Acme;\npublic class Widget { }\n",
                "samples/Factory.cs": (
                    "namespace Acme.Samples;\n"
                    "public class Factory { public object Widget() { return null; } }\n"
                ),
                "tests/Pong.cs": "namespace Acme.Tests;\npublic class Pong : Widget { }\n",
            },
        )
        run_updater(project, mock_ingestor, skip_if_missing=CSHARP)

        _, _, label, parent, _ = _only_edge_from(
            mock_ingestor, cs.RelationshipType.INHERITS, "Acme.Tests.Pong"
        )
        assert (label, parent) == (
            cs.NodeLabel.CLASS,
            f"{project.name}.src.Widget.Acme.Widget",
        )

    def test_interface_list_after_base_still_yields_implements(
        self, project: Path, mock_ingestor: MagicMock
    ) -> None:
        _write(
            project,
            {
                "src/Base.cs": (
                    "namespace Acme;\n"
                    "public class Base<T> { }\n"
                    "public interface IFoo { }\n"
                ),
                "samples/R.cs": (
                    "namespace Acme.Samples;\n"
                    "public class R { public int Base { get; set; } "
                    "public int IFoo { get; set; } }\n"
                ),
                "tests/Pong.cs": (
                    "namespace Acme.Tests;\n"
                    "public class Pong : Base<int>, IFoo, System.IDisposable\n"
                    "{\n"
                    "    public void Dispose() { }\n"
                    "}\n"
                ),
            },
        )
        run_updater(project, mock_ingestor, skip_if_missing=CSHARP)

        _, _, label, parent, _ = _only_edge_from(
            mock_ingestor, cs.RelationshipType.INHERITS, "Acme.Tests.Pong"
        )
        assert (label, parent) == (
            cs.NodeLabel.CLASS,
            f"{project.name}.src.Base.Acme.Base",
        )
        implements = {
            (e[2], e[3])
            for e in _edges_from(
                mock_ingestor, cs.RelationshipType.IMPLEMENTS, "Acme.Tests.Pong"
            )
        }
        assert implements == {
            (cs.NodeLabel.INTERFACE, f"{project.name}.src.Base.Acme.IFoo"),
            (cs.NodeLabel.EXTERNAL_MODULE, "System.IDisposable"),
        }

    def test_unknown_base_stays_external_despite_same_named_members(
        self, project: Path, mock_ingestor: MagicMock
    ) -> None:
        _write(
            project,
            {
                "samples/R.cs": (
                    "namespace Acme.Samples;\n"
                    "public class R\n"
                    "{\n"
                    "    public int ExternalBase { get; set; }\n"
                    "    public void IThing() { }\n"
                    "}\n"
                ),
                "tests/Pong.cs": (
                    "namespace Acme.Tests;\n"
                    "public class Pong : ExternalBase, IThing { }\n"
                ),
            },
        )
        run_updater(project, mock_ingestor, skip_if_missing=CSHARP)

        inherits = _only_edge_from(
            mock_ingestor, cs.RelationshipType.INHERITS, "Acme.Tests.Pong"
        )
        implements = _only_edge_from(
            mock_ingestor, cs.RelationshipType.IMPLEMENTS, "Acme.Tests.Pong"
        )
        assert inherits[2:4] == (cs.NodeLabel.EXTERNAL_MODULE, "ExternalBase")
        assert implements[2:4] == (cs.NodeLabel.EXTERNAL_MODULE, "IThing")


class TestCSharpBaseScopeOrder:
    def test_enclosing_namespace_beats_unrelated_namespace(
        self, project: Path, mock_ingestor: MagicMock
    ) -> None:
        # `a/` sorts first, so a name-only sweep reaches the unrelated
        # Acme.Samples.Handler before the enclosing Acme.Handler.
        _write(
            project,
            {
                "a/Handler.cs": "namespace Acme.Samples;\npublic class Handler { }\n",
                "b/Handler.cs": "namespace Acme;\npublic class Handler { }\n",
                "z/Pong.cs": "namespace Acme.Tests;\npublic class Pong : Handler { }\n",
            },
        )
        run_updater(project, mock_ingestor, skip_if_missing=CSHARP)

        _, _, _, parent, props = _only_edge_from(
            mock_ingestor, cs.RelationshipType.INHERITS, "Acme.Tests.Pong"
        )
        assert parent == f"{project.name}.b.Handler.Acme.Handler"
        assert cs.KEY_RESOLUTION not in props

    def test_using_namespace_beats_unrelated_namespace(
        self, project: Path, mock_ingestor: MagicMock
    ) -> None:
        _write(
            project,
            {
                "a/Handler.cs": "namespace Acme;\npublic class Handler { }\n",
                "b/Handler.cs": "namespace Other.Lib;\npublic class Handler { }\n",
                "z/Pong.cs": (
                    "using Other.Lib;\n"
                    "namespace Zed;\n"
                    "public class Pong : Handler { }\n"
                ),
            },
        )
        run_updater(project, mock_ingestor, skip_if_missing=CSHARP)

        _, _, _, parent, props = _only_edge_from(
            mock_ingestor, cs.RelationshipType.INHERITS, "Zed.Pong"
        )
        assert parent == f"{project.name}.b.Handler.Other.Lib.Handler"
        assert cs.KEY_RESOLUTION not in props

    @pytest.mark.parametrize("split_files", [False, True])
    def test_generic_arity_picks_the_matching_declaration(
        self, project: Path, mock_ingestor: MagicMock, split_files: bool
    ) -> None:
        one = "public class Base<T> { }\n"
        two = "public class Base<T1, T2> { }\n"
        files = (
            {
                "src/Base1.cs": f"namespace Acme;\n{one}",
                "src/Base2.cs": f"namespace Acme;\n{two}",
            }
            if split_files
            else {"src/Base.cs": f"namespace Acme;\n{one}{two}"}
        )
        files["tests/T.cs"] = (
            "namespace Acme.Tests;\n"
            "public class One : Base<int> { }\n"
            "public class Two : Base<int, string> { }\n"
        )
        _write(project, files)
        run_updater(project, mock_ingestor, skip_if_missing=CSHARP)

        one_parent = _only_edge_from(
            mock_ingestor, cs.RelationshipType.INHERITS, "Acme.Tests.One"
        )[3]
        two_parent = _only_edge_from(
            mock_ingestor, cs.RelationshipType.INHERITS, "Acme.Tests.Two"
        )[3]
        if split_files:
            assert one_parent == f"{project.name}.src.Base1.Acme.Base"
            assert two_parent == f"{project.name}.src.Base2.Acme.Base"
        else:
            # The second same-scope declaration registers under a
            # duplicate-marked qn (`Base@3`).
            assert one_parent == f"{project.name}.src.Base.Acme.Base"
            assert two_parent.startswith(f"{project.name}.src.Base.Acme.Base@")

    def test_using_alias_of_a_generic_base_resolves_first_party(
        self, project: Path, mock_ingestor: MagicMock
    ) -> None:
        _write(
            project,
            {
                "src/Handlers.cs": (
                    "namespace Acme;\npublic abstract class NotificationHandler<T> { }\n"
                ),
                "samples/R.cs": (
                    "namespace Acme.Samples;\n"
                    "public class R { public bool NH { get; set; } }\n"
                ),
                "tests/Pong.cs": (
                    "using NH = Acme.NotificationHandler<string>;\n"
                    "namespace Zed;\n"
                    "public class Pong : NH { }\n"
                ),
            },
        )
        run_updater(project, mock_ingestor, skip_if_missing=CSHARP)

        _, _, label, parent, _ = _only_edge_from(
            mock_ingestor, cs.RelationshipType.INHERITS, "Zed.Pong"
        )
        assert (label, parent) == (
            cs.NodeLabel.CLASS,
            f"{project.name}.src.Handlers.Acme.NotificationHandler",
        )


class TestCSharpHybridFrontendSharesTheLookup:
    def test_roslyn_base_kind_still_binds_the_type_not_the_property(
        self, project: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Hybrid mode only tells class from interface per simple name; the
        # target is still looked up by the tree-sitter resolver, so it had
        # the same property binding. The toolchain is faked (no .NET here).
        _write(project, {**ISSUE_FILES, "Acme.csproj": _CSPROJ})
        facts = CSharpSemanticFacts(
            base_kinds={("tests/HandlerTests.cs", 2): {"NotificationHandler": "class"}},
            call_sites={},
            partial_groups=[],
            query_calls=[],
            external_sites=set(),
            arg_flows={},
            bind_flows={},
            out_writes={},
        )
        monkeypatch.setattr(gu.settings, "CSHARP_FRONTEND", cs.CSharpFrontend.AUTO)
        monkeypatch.setattr(csharp_fe, "csharp_frontend_available", lambda: True)
        frontend = MagicMock(return_value=facts)
        monkeypatch.setattr(csharp_fe, "run_csharp_frontend", frontend)
        run_updater(project, mock_ingestor, skip_if_missing=CSHARP)

        frontend.assert_called_once()

        _, _, label, parent, _ = _only_edge_from(
            mock_ingestor, cs.RelationshipType.INHERITS, "Acme.Tests.PongHandler"
        )
        assert (label, parent) == (
            cs.NodeLabel.CLASS,
            f"{project.name}.src.Handlers.Acme.NotificationHandler",
        )


class TestCSharpScopeNegatives:
    def test_nested_base_through_outer_still_resolves(
        self, project: Path, mock_ingestor: MagicMock
    ) -> None:
        _write(
            project,
            {
                "src/Outer.cs": (
                    "namespace Acme;\npublic class Outer { public class Inner { } }\n"
                ),
                "samples/R.cs": (
                    "namespace Acme.Samples;\n"
                    "public class R { public int Inner { get; set; } }\n"
                ),
                "tests/Pong.cs": (
                    "namespace Acme.Tests;\npublic class Pong : Outer.Inner { }\n"
                ),
            },
        )
        run_updater(project, mock_ingestor, skip_if_missing=CSHARP)

        _, _, label, parent, _ = _only_edge_from(
            mock_ingestor, cs.RelationshipType.INHERITS, "Acme.Tests.Pong"
        )
        assert (label, parent) == (
            cs.NodeLabel.CLASS,
            f"{project.name}.src.Outer.Acme.Outer.Inner",
        )

    def test_base_in_an_unrelated_namespace_still_resolves_as_heuristic(
        self, project: Path, mock_ingestor: MagicMock
    ) -> None:
        # No enclosing namespace and no `using` reaches Lib.Core, but the
        # class is the only type of that name: keep the edge, flagged as a
        # name-only match rather than dropped.
        _write(
            project,
            {
                "lib/Widget.cs": "namespace Lib.Core;\npublic class Widget { }\n",
                "app/Pong.cs": "namespace App;\npublic class Pong : Widget { }\n",
            },
        )
        run_updater(project, mock_ingestor, skip_if_missing=CSHARP)

        _, _, label, parent, props = _only_edge_from(
            mock_ingestor, cs.RelationshipType.INHERITS, "App.Pong"
        )
        assert (label, parent) == (
            cs.NodeLabel.CLASS,
            f"{project.name}.lib.Widget.Lib.Core.Widget",
        )
        assert props.get(cs.KEY_RESOLUTION) == cs.EdgeResolution.HEURISTIC

    def test_base_reached_through_using_is_not_heuristic(
        self, project: Path, mock_ingestor: MagicMock
    ) -> None:
        _write(
            project,
            {
                "lib/Widget.cs": "namespace Lib.Core;\npublic class Widget { }\n",
                "app/Pong.cs": (
                    "using Lib.Core;\nnamespace App;\npublic class Pong : Widget { }\n"
                ),
            },
        )
        run_updater(project, mock_ingestor, skip_if_missing=CSHARP)

        _, _, _, parent, props = _only_edge_from(
            mock_ingestor, cs.RelationshipType.INHERITS, "App.Pong"
        )
        assert parent == f"{project.name}.lib.Widget.Lib.Core.Widget"
        assert cs.KEY_RESOLUTION not in props


class TestOtherLanguagesShareTheTypeOnlyRule:
    def test_python_base_skips_same_named_method(
        self, project: Path, mock_ingestor: MagicMock
    ) -> None:
        _write(
            project,
            {
                "a/factory.py": "class Factory:\n    def Widget(self):\n        return None\n",
                "b/widget.py": "class Widget:\n    def run(self):\n        pass\n",
                "c/pong.py": "class Pong(Widget):\n    def run(self):\n        pass\n",
            },
        )
        run_updater(project, mock_ingestor)

        _, _, label, parent, _ = _only_edge_from(
            mock_ingestor, cs.RelationshipType.INHERITS, "c.pong.Pong"
        )
        assert (label, parent) == (
            cs.NodeLabel.CLASS,
            f"{project.name}.b.widget.Widget",
        )

    def test_python_imported_base_still_resolves(
        self, project: Path, mock_ingestor: MagicMock
    ) -> None:
        _write(
            project,
            {
                "pkg/__init__.py": "",
                "pkg/base.py": "class Base:\n    def run(self):\n        pass\n",
                "pkg/child.py": (
                    "from pkg.base import Base\n\n\n"
                    "class Child(Base):\n    def run(self):\n        pass\n"
                ),
            },
        )
        run_updater(project, mock_ingestor)

        _, _, label, parent, _ = _only_edge_from(
            mock_ingestor, cs.RelationshipType.INHERITS, "pkg.child.Child"
        )
        assert (label, parent) == (cs.NodeLabel.CLASS, f"{project.name}.pkg.base.Base")

    def test_javascript_constructor_function_base_still_resolves(
        self, project: Path, mock_ingestor: MagicMock
    ) -> None:
        # An ES5 constructor function is a legal `extends` target, so the
        # type-only rule must keep Function there.
        _write(
            project,
            {
                "b/base.js": "function Base() {}\nBase.prototype.run = function () {};\n",
                "c/pong.js": "class Pong extends Base { run() {} }\n",
            },
        )
        run_updater(project, mock_ingestor, skip_if_missing="javascript")

        _, _, label, parent, _ = _only_edge_from(
            mock_ingestor, cs.RelationshipType.INHERITS, "c.pong.Pong"
        )
        assert (label, parent) == (cs.NodeLabel.FUNCTION, f"{project.name}.b.base.Base")
