"""C# type qualified names carry the namespace only when it says something the
module path does not (issue #1629).

`src/Serilog/Capturing/PropertyBinder.cs` declaring `namespace
Serilog.Capturing;` used to produce
`proj.src.Serilog.Capturing.PropertyBinder.Serilog.Capturing.PropertyBinder`:
the namespace is a scope in the qualified-name walk, and PSR-4-style layouts
(234 of 265 serilog types) spell it a second time. A namespace the directory
does not carry still distinguishes two same-named types in one file, so it is
kept; either way the declared namespace is recorded on the type node.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.language_spec import CSHARP_FQN_SPEC, csharp_namespaced_from_graph
from codebase_rag.parser_loader import load_parsers
from codebase_rag.utils.fqn_resolver import resolve_fqn_from_ast
from evals.cgr_graph import _StatefulIngestor

MIRRORED = {
    "src/Serilog/Capturing/PropertyBinder.cs": (
        "namespace Serilog.Capturing;\n\n"
        "public class PropertyBinder\n{\n    public void Bind() { }\n}\n"
    ),
    "src/Serilog/Guard.cs": (
        "namespace Serilog\n{\n    public static class Guard\n    {\n"
        "        public static void AgainstNull() { }\n    }\n}\n"
    ),
    # Nested block namespaces fold as one run.
    "src/Serilog/Capturing/Deep.cs": (
        "namespace Serilog\n{\n    namespace Capturing\n    {\n"
        "        public class Deep { }\n    }\n}\n"
    ),
    # The directory says nothing about this namespace: kept.
    "src/Serilog/Nested.cs": (
        "namespace JetBrains.Annotations\n{\n"
        "    public class NoEnumerationAttribute { }\n}\n"
    ),
    # No namespace at all.
    "src/Serilog/Plain.cs": "public class Plain { }\n",
    # Two same-named types in one file: only the mirrored namespace folds.
    "src/Serilog/Two.cs": (
        "namespace Serilog\n{\n    public class Twin { }\n}\n\n"
        "namespace Other\n{\n    public class Twin { }\n}\n"
    ),
}

# `this.Poke()` inside Widget: the receiver's namespace-qualified form
# (`N.Widget`) must match the extension's unqualified `this Widget` through
# the extension's declared namespace. `Decoy.Poke` sits in the caller's own
# module, so the simple-name fallback would prefer it: only the precise
# matcher can produce the right edge.
EXTENSIONS = {
    "src/N/Widget.cs": (
        "namespace N;\n\npublic class Widget\n{\n"
        "    public void Self() { this.Poke(); }\n}\n\n"
        "public class Decoy\n{\n    public void Poke() { }\n}\n"
    ),
    "src/N/Ext.cs": (
        "namespace N;\n\npublic static class Ext\n{\n"
        "    public static void Poke(this Widget w) { }\n}\n"
    ),
    "src/N/App.cs": (
        "namespace N;\n\npublic class App\n{\n"
        "    public void Run() { var w = new Widget(); w.Poke(); }\n}\n"
    ),
}

# Two `Widget`s with an extension each on `this <namespace>.Widget`. The
# wrong pair shares the caller's FILE (a non-mirrored namespace, so its
# names keep the `Alpha.` run), which is where the fallback's distance rule
# would bind `this.Poke()`; the right one is in a sibling file that sorts
# after it. Only a receiver typed as `Zeta.Widget` reaches Zext.
TWINS = {
    "src/Zeta/Widget.cs": (
        "namespace Zeta\n{\n    public class Widget\n    {\n"
        "        public void Self() { this.Poke(); }\n    }\n}\n\n"
        "namespace Alpha\n{\n    public class Widget { }\n\n"
        "    public static class AlphaExt\n    {\n"
        "        public static void Poke(this Alpha.Widget w) { }\n    }\n}\n"
    ),
    "src/Zeta/Zext.cs": (
        "namespace Zeta;\n\npublic static class Zext\n{\n"
        "    public static void Poke(this Zeta.Widget w) { }\n}\n"
    ),
}

# A static factory that shares the class's simple name, in a mirrored
# namespace: `new LogEventProperty(...)` must still construct the class.
# The serilog parity run lost 55 CALLS and 31 INSTANTIATES edges to this
# shape once the class qn stopped repeating its namespace: the simple-name
# fallback ranks candidates by import distance, and the method sat closer.
FACTORY = {
    "src/Serilog/Events/LogEventProperty.cs": (
        "namespace Serilog.Events;\n\npublic class LogEventProperty\n{\n"
        "    public LogEventProperty(string name, object value) { }\n}\n"
    ),
    "test/Serilog.Tests/Support/Some.cs": (
        "using Serilog.Events;\n\nnamespace Serilog.Tests.Support;\n\n"
        "public static class Some\n{\n"
        "    public static LogEventProperty LogEventProperty()\n"
        '    {\n        return new LogEventProperty("x", 1);\n    }\n}\n'
    ),
    "test/Serilog.Tests/Core/CapturingTests.cs": (
        "using Serilog.Events;\nusing Serilog.Tests.Support;\n\n"
        "namespace Serilog.Tests.Core;\n\npublic class CapturingTests\n{\n"
        '    public void Direct() { var p = new LogEventProperty("a", 1); }\n'
        "    public void Made() { var p = Some.LogEventProperty(); }\n}\n"
    ),
}


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, source in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


def _index(root: Path, files: dict[str, str]) -> _StatefulIngestor:
    _write(root, files)
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,  # type: ignore[arg-type]
        repo_path=root,
        parsers=parsers,
        queries=queries,
    ).run()
    return store


def _qns(store: _StatefulIngestor, label: str) -> set[str]:
    return {qn for node_label, qn in store.nodes if node_label == label}


def _calls(store: _StatefulIngestor) -> set[tuple[str, str]]:
    return {
        (str(source), str(target))
        for _sl, source, rel, _tl, target in store.edges
        if rel == cs.RelationshipType.CALLS.value
    }


def _walk(node: Node) -> Iterator[Node]:
    yield node
    for child in node.children:
        yield from _walk(child)


class TestNamespaceInTheQualifiedName:
    def test_a_namespace_mirroring_the_directory_is_not_repeated(
        self, tmp_path: Path
    ) -> None:
        store = _index(tmp_path / "proj", MIRRORED)
        classes = _qns(store, cs.NodeLabel.CLASS.value)
        assert "proj.src.Serilog.Capturing.PropertyBinder.PropertyBinder" in classes
        assert "proj.src.Serilog.Guard.Guard" in classes
        assert "proj.src.Serilog.Capturing.Deep.Deep" in classes
        assert not any(".PropertyBinder.Serilog." in qn for qn in classes), classes
        methods = _qns(store, cs.NodeLabel.METHOD.value)
        assert (
            "proj.src.Serilog.Capturing.PropertyBinder.PropertyBinder.Bind" in methods
        )
        assert "proj.src.Serilog.Guard.Guard.AgainstNull" in methods

    def test_a_namespace_the_directory_does_not_carry_is_kept(
        self, tmp_path: Path
    ) -> None:
        store = _index(tmp_path / "proj", MIRRORED)
        classes = _qns(store, cs.NodeLabel.CLASS.value)
        assert (
            "proj.src.Serilog.Nested.JetBrains.Annotations.NoEnumerationAttribute"
            in classes
        )
        assert "proj.src.Serilog.Plain.Plain" in classes
        # Same file, same simple name: the folded and the kept one stay apart.
        assert "proj.src.Serilog.Two.Twin" in classes
        assert "proj.src.Serilog.Two.Other.Twin" in classes

    def test_the_declared_namespace_is_a_property_of_the_type(
        self, tmp_path: Path
    ) -> None:
        store = _index(tmp_path / "proj", MIRRORED)
        label = cs.NodeLabel.CLASS.value

        def namespace_of(qn: str) -> object:
            return store.nodes[(label, qn)].get(cs.KEY_NAMESPACE)

        assert (
            namespace_of("proj.src.Serilog.Capturing.PropertyBinder.PropertyBinder")
            == "Serilog.Capturing"
        )
        assert namespace_of("proj.src.Serilog.Guard.Guard") == "Serilog"
        assert (
            namespace_of("proj.src.Serilog.Capturing.Deep.Deep") == "Serilog.Capturing"
        )
        assert (
            namespace_of(
                "proj.src.Serilog.Nested.JetBrains.Annotations.NoEnumerationAttribute"
            )
            == "JetBrains.Annotations"
        )
        assert namespace_of("proj.src.Serilog.Two.Other.Twin") == "Other"
        assert (
            cs.KEY_NAMESPACE not in store.nodes[(label, "proj.src.Serilog.Plain.Plain")]
        )

    def test_source_lookup_by_qualified_name_uses_the_same_rule(
        self, tmp_path: Path
    ) -> None:
        """`get_function_source` walks the AST with the same spec; a method's
        name from that walk must be the one the graph registered."""
        root = tmp_path / "proj"
        _write(root, MIRRORED)
        parsers, _queries = load_parsers()
        path = root / "src/Serilog/Capturing/PropertyBinder.cs"
        tree = parsers[cs.SupportedLanguage.CSHARP].parse(path.read_bytes())
        method = next(
            node for node in _walk(tree.root_node) if node.type == "method_declaration"
        )
        assert (
            resolve_fqn_from_ast(method, path, root, "proj", CSHARP_FQN_SPEC)
            == "proj.src.Serilog.Capturing.PropertyBinder.PropertyBinder.Bind"
        )


class TestExtensionMethodsAcrossAMirroredNamespace:
    """The extension-method matcher compared namespace-qualified names it
    derived from the qn by stripping the module prefix; with the namespace
    folded out of the qn it reads the declared namespace instead."""

    def test_this_and_instance_receivers_bind_the_extension(
        self, tmp_path: Path
    ) -> None:
        store = _index(tmp_path / "proj", EXTENSIONS)
        calls = _calls(store)
        ext = "proj.src.N.Ext.Ext.Poke(Widget)"
        assert ("proj.src.N.Widget.Widget.Self", ext) in calls, sorted(calls)
        assert ("proj.src.N.App.App.Run", ext) in calls, sorted(calls)

    def test_this_binds_the_extension_of_its_own_namespace(
        self, tmp_path: Path
    ) -> None:
        store = _index(tmp_path / "proj", TWINS)
        calls = _calls(store)
        assert (
            "proj.src.Zeta.Widget.Widget.Self",
            "proj.src.Zeta.Zext.Zext.Poke(Zeta.Widget)",
        ) in calls, sorted(calls)
        assert (
            "proj.src.Zeta.Widget.Widget.Self",
            "proj.src.Zeta.Widget.Alpha.AlphaExt.Poke(Alpha.Widget)",
        ) not in calls, sorted(calls)


class TestObjectCreationNamesAType:
    def test_a_same_named_factory_method_does_not_capture_the_constructor(
        self, tmp_path: Path
    ) -> None:
        store = _index(tmp_path / "proj", FACTORY)
        calls = _calls(store)
        ctor = "proj.src.Serilog.Events.LogEventProperty.LogEventProperty.LogEventProperty(string, object)"
        assert (
            "proj.test.Serilog.Tests.Core.CapturingTests.CapturingTests.Direct",
            ctor,
        ) in calls, sorted(calls)
        instantiates = {
            (str(source), str(target))
            for _sl, source, rel, _tl, target in store.edges
            if rel == cs.RelationshipType.INSTANTIATES.value
        }
        assert (
            "proj.test.Serilog.Tests.Core.CapturingTests.CapturingTests.Direct",
            "proj.src.Serilog.Events.LogEventProperty.LogEventProperty",
        ) in instantiates, sorted(instantiates)
        # INSIDE the same-named factory too: the enclosing-scope tier found
        # the factory itself and the construction bound nothing (#1997).
        factory = "proj.test.Serilog.Tests.Support.Some.Some.LogEventProperty"
        assert (factory, ctor) in calls, sorted(calls)
        assert (
            factory,
            "proj.src.Serilog.Events.LogEventProperty.LogEventProperty",
        ) in instantiates, sorted(instantiates)
        assert (factory, factory) not in calls, sorted(calls)
        # The control: a plain call of the factory still binds the method.
        assert (
            "proj.test.Serilog.Tests.Core.CapturingTests.CapturingTests.Made",
            "proj.test.Serilog.Tests.Support.Some.Some.LogEventProperty",
        ) in calls, sorted(calls)


class TestWrittenNamespaceQualifiedNames:
    def test_a_qualified_base_list_resolves_to_the_folded_types(
        self, tmp_path: Path
    ) -> None:
        """`class Q : Zeta.BaseC, Zeta.ISink` matched the tail of the base's
        qn; with the mirrored namespace folded out, the declared form is
        looked up instead (bot review)."""
        store = _index(
            tmp_path / "proj",
            {
                "src/Zeta/Base.cs": (
                    "namespace Zeta;\n\npublic interface ISink { }\n\n"
                    "public class BaseC { }\n"
                ),
                "src/App/Q.cs": (
                    "namespace App;\n\npublic class Q : Zeta.BaseC, Zeta.ISink { }\n"
                ),
            },
        )
        edges = {
            (rel, str(source), str(target))
            for _sl, source, rel, _tl, target in store.edges
            if rel in ("INHERITS", "IMPLEMENTS")
        }
        assert ("INHERITS", "proj.src.App.Q.Q", "proj.src.Zeta.Base.BaseC") in edges, (
            edges
        )
        assert (
            "IMPLEMENTS",
            "proj.src.App.Q.Q",
            "proj.src.Zeta.Base.ISink",
        ) in edges, edges

    def test_a_same_stem_sibling_of_another_language_does_not_fold(
        self, tmp_path: Path
    ) -> None:
        """`src/Foo.c` beside `src/Foo.cs` gives the C# module the qn
        `proj.src.Foo.cs`; its directory is `src`, which does not spell
        `namespace Foo`, so the namespace stays (bot review)."""
        store = _index(
            tmp_path / "proj",
            {
                "src/Foo.c": "int foo(void) { return 1; }\n",
                "src/Foo.cs": "namespace Foo;\n\npublic class Bar { }\n",
                "src/Foo/Baz.cs": "namespace Foo;\n\npublic class Baz { }\n",
            },
        )
        classes = _qns(store, cs.NodeLabel.CLASS.value)
        assert "proj.src.Foo.cs.Foo.Bar" in classes, classes
        assert "proj.src.Foo.Baz.Baz" in classes, classes

    def test_the_declared_form_is_looked_up_for_csharp_references_only(
        self, tmp_path: Path
    ) -> None:
        """A Python base `Zeta.BaseC` that resolves to nothing must not bind
        the C# type declared under `namespace Zeta` (bot review)."""
        store = _index(
            tmp_path / "proj",
            {
                "src/Zeta/Base.cs": "namespace Zeta;\n\npublic class BaseC { }\n",
                "py/q.py": "class Q(Zeta.BaseC):\n    pass\n",
            },
        )
        inherits = {
            (str(source), str(target))
            for _sl, source, rel, _tl, target in store.edges
            if rel == "INHERITS"
        }
        assert not any(
            source == "proj.py.q.Q" and target.startswith("proj.src")
            for source, target in inherits
        ), sorted(inherits)

    def test_two_projects_declaring_one_name_stay_unresolved(
        self, tmp_path: Path
    ) -> None:
        """`N.Widget` declared under `src/N` and under `lib/N` (both folded,
        so neither qn ends with `N.Widget`) are two types; a base list
        naming `N.Widget` from a third place binds neither rather than one
        at random (bot review). Twins that KEEP the namespace in their qn
        are matched by the ordinary suffix tier, whose first-match pick is
        pre-existing and not covered here."""
        store = _index(
            tmp_path / "proj",
            {
                "src/N/W.cs": "namespace N;\n\npublic class Widget { }\n",
                "lib/N/W.cs": "namespace N;\n\npublic class Widget { }\n",
                "src/C/Q.cs": "namespace C;\n\npublic class Q : N.Widget { }\n",
            },
        )
        inherits = {
            (str(source), str(target))
            for _sl, source, rel, _tl, target in store.edges
            if rel == "INHERITS"
        }
        # The unresolved base keeps its external parent (`N.Widget`), as any
        # unresolved base does; neither first-party twin is bound.
        assert not any(
            source == "proj.src.C.Q.Q" and target.startswith("proj.")
            for source, target in inherits
        ), sorted(inherits)

    def test_a_partial_part_with_a_dotted_stem_joins_its_group(
        self, tmp_path: Path
    ) -> None:
        """`Widget.cs` and `Widget.Designer.cs` are one partial type; a base
        list naming `N.Widget` binds a part rather than refusing them as
        two projects (bot review)."""
        part = "namespace N;\n\npublic partial class Widget { }\n"
        store = _index(
            tmp_path / "proj",
            {
                "src/N/Widget.cs": part,
                "src/N/Widget.Designer.cs": part,
                "src/C/Q.cs": "namespace C;\n\npublic class Q : N.Widget { }\n",
            },
        )
        inherits = {
            str(target)
            for _sl, source, rel, _tl, target in store.edges
            if rel == "INHERITS" and str(source) == "proj.src.C.Q.Q"
        }
        assert inherits & {
            "proj.src.N.Widget.Widget",
            "proj.src.N.Widget.Designer.Widget",
        }, sorted(inherits)

    def test_an_incremental_run_binds_a_base_in_an_unchanged_file(
        self, tmp_path: Path
    ) -> None:
        """The declared-form index is rebuilt from the graph for files an
        incremental run does not re-parse: without it, editing `Q.cs` loses
        `: Zeta.BaseC`, whose folded qn no longer ends with the declared
        name (bot review)."""
        root = tmp_path / "proj"
        q_source = "namespace App;\n\npublic class Q : Zeta.BaseC { }\n"
        _write(
            root,
            {
                "src/Zeta/Base.cs": "namespace Zeta;\n\npublic class BaseC { }\n",
                "src/App/Q.cs": q_source,
            },
        )
        parsers, queries = load_parsers()
        store = _StatefulIngestor()
        for force in (True, False):
            GraphUpdater(
                ingestor=store,  # type: ignore[arg-type]
                repo_path=root,
                parsers=parsers,
                queries=queries,
            ).run(force=force)
            # A trailing comment re-parses Q.cs alone; Base.cs is rehydrated.
            (root / "src/App/Q.cs").write_text(q_source + "// touched\n")
        edge = ("proj.src.App.Q.Q", "proj.src.Zeta.Base.BaseC")
        inherits = {
            (str(source), str(target))
            for _sl, source, rel, _tl, target in store.edges
            if rel == "INHERITS"
        }
        assert edge in inherits, sorted(inherits)

    def test_an_incremental_run_binds_a_base_split_across_unchanged_parts(
        self, tmp_path: Path
    ) -> None:
        """Two unchanged parts of one partial type are both rehydrated into
        the declared-form index; without their partial group, `: N.Widget`
        read them as two projects and bound nothing (bot review)."""
        root = tmp_path / "proj"
        part = "namespace N;\n\npublic partial class Widget { }\n"
        q_source = "namespace C;\n\npublic class Q : N.Widget { }\n"
        _write(
            root,
            {
                "src/N/Widget.cs": part,
                "src/N/Widget.Designer.cs": part,
                "src/C/Q.cs": q_source,
            },
        )
        parsers, queries = load_parsers()
        store = _StatefulIngestor()
        for force in (True, False):
            GraphUpdater(
                ingestor=store,  # type: ignore[arg-type]
                repo_path=root,
                parsers=parsers,
                queries=queries,
            ).run(force=force)
            # Re-parses Q.cs alone; both Widget parts are rehydrated.
            (root / "src/C/Q.cs").write_text(q_source + "// touched\n")
        inherits = {
            str(target)
            for _sl, source, rel, _tl, target in store.edges
            if rel == "INHERITS" and str(source) == "proj.src.C.Q.Q"
        }
        assert inherits & {
            "proj.src.N.Widget.Widget",
            "proj.src.N.Widget.Designer.Widget",
        }, sorted(inherits)


@pytest.mark.parametrize(
    ("qn", "path", "namespace", "expected"),
    [
        # Folded: the directory spells the namespace, the qn does not.
        ("proj.src.Zeta.Base.BaseC", "src/Zeta/Base.cs", "Zeta", "Zeta.BaseC"),
        # Kept: the qn already carries the namespace run.
        (
            "proj.src.Serilog.Nested.JetBrains.Annotations.Attr",
            "src/Serilog/Nested.cs",
            "JetBrains.Annotations",
            "JetBrains.Annotations.Attr",
        ),
        # No namespace, nested type.
        ("proj.src.Plain.Outer.Inner", "src/Plain.cs", None, "Outer.Inner"),
        # A same-stem sibling of another language: `<stem>.<ext>` module.
        ("proj.src.Foo.cs.Foo", "src/Foo.cs", None, "Foo"),
        # The duplicate-qn marker is a registration artefact.
        ("proj.src.N.W.Bench@24", "src/N/W.cs", "N", "N.Bench"),
        ("proj.src.N.W.Bench@24_5", "src/N/W.cs", "N", "N.Bench"),
        # A verbatim identifier is not the marker.
        ("proj.src.N.W.@event", "src/N/W.cs", "N", "N.@event"),
        # A module qn neither spelling predicts is not guessed.
        ("other.src.N.W.Widget", "src/N/W.cs", "N", None),
    ],
)
def test_the_declared_form_is_rebuilt_from_the_graph(
    qn: str, path: str, namespace: str | None, expected: str | None
) -> None:
    assert csharp_namespaced_from_graph(qn, path, "proj", namespace) == expected
