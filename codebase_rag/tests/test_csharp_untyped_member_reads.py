"""A member access passed as an argument is a method group only on a typed
receiver; an untyped receiver or a property read yields no edge
(issue #1998)."""

from __future__ import annotations

from pathlib import Path

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

FILES = {
    "src/Lib.cs": (
        "using System.Collections.Generic;\n\nnamespace Lib;\n\n"
        "public class EventProperty\n{\n    public string Value { get; set; }\n}\n\n"
        "public class Config\n{\n"
        "    public Config Override(string key, string value) { return this; }\n"
        "    public void Each(List<string> items, System.Action<string> f) { }\n}\n\n"
        "public class Printer\n{\n    public void Print(string s) { }\n}\n"
    ),
    "src/App.cs": (
        "using System.Collections.Generic;\nusing Lib;\n\nnamespace App;\n\n"
        "public class Bench\n{\n"
        "    public void Run(Dictionary<string, string> overrides, List<string> items)\n"
        "    {\n        var config = new Config();\n"
        "        foreach (var @override in overrides)\n        {\n"
        "            config = config.Override(@override.Key, @override.Value);\n"
        "        }\n"
        "        var printer = new Printer();\n"
        "        config.Each(items, printer.Print);\n"
        "    }\n}\n"
    ),
}

# The control: a property read on a TYPED receiver is a reference.
TYPED = {
    "src/Lib.cs": FILES["src/Lib.cs"],
    "src/App.cs": (
        "using Lib;\n\nnamespace App;\n\npublic class Bench\n{\n"
        "    public void Run()\n    {\n"
        "        var config = new Config();\n        var p = new EventProperty();\n"
        "        config.Override(p.Value, p.Value);\n"
        "    }\n}\n"
    ),
}


def _edges(root: Path, files: dict[str, str] = FILES) -> set[tuple[str, str]]:
    for rel, source in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,  # type: ignore[arg-type]
        repo_path=root,
        parsers=parsers,
        queries=queries,
    ).run()
    return {
        (kind, str(target))
        for _sl, source, kind, _tl, target in store.edges
        if ".Bench.Run" in str(source) and kind in ("CALLS", "REFERENCES")
    }


def test_an_untyped_receivers_member_is_not_a_reference(tmp_path: Path) -> None:
    edges = _edges(tmp_path / "proj")
    assert not any(
        target.endswith(".EventProperty.Value") and kind == "REFERENCES"
        for kind, target in edges
    ), sorted(edges)


def test_a_typed_receivers_method_group_still_binds(tmp_path: Path) -> None:
    edges = _edges(tmp_path / "proj")
    assert any(target.endswith(".Printer.Print(string)") for _k, target in edges), (
        sorted(edges)
    )


def test_a_typed_receivers_property_read_is_a_reference(tmp_path: Path) -> None:
    edges = _edges(tmp_path / "proj", TYPED)
    assert ("REFERENCES", "proj.src.Lib.Lib.EventProperty.Value") in edges or (
        "REFERENCES",
        "proj.src.Lib.EventProperty.Value",
    ) in edges, sorted(edges)


# Receivers the typed-local path does not cover but the engine can name.
SHAPES = {
    "src/Lib.cs": (
        "namespace Lib;\n\n"
        "public class BaseHandler\n{\n    public void Handle(string s) { }\n}\n\n"
        "public class Outer\n{\n    public static class Inner\n    {\n"
        "        public static void Go(string s) { }\n    }\n}\n\n"
        "public static class Util\n{\n    public static void Helper(string s) { }\n}\n\n"
        "public class Config\n{\n"
        "    public void Each(System.Action<string> f) { }\n}\n"
    ),
    "src/App.cs": (
        "using Lib;\n\nnamespace App;\n\npublic class Bench : BaseHandler\n{\n"
        "    public void Run()\n    {\n        var config = new Config();\n"
        "        config.Each(base.Handle);\n"
        "        config.Each(Outer.Inner.Go);\n"
        "        config.Each(Lib.Util.Helper);\n"
        "    }\n}\n"
    ),
}


def test_base_nested_and_namespace_qualified_method_groups_bind(tmp_path: Path) -> None:
    edges = _edges(tmp_path / "proj", SHAPES)
    targets = {target for _kind, target in edges}
    assert any(t.endswith(".BaseHandler.Handle(string)") for t in targets), sorted(
        targets
    )
    assert any(t.endswith(".Outer.Inner.Go(string)") for t in targets), sorted(targets)
    assert any(t.endswith(".Util.Helper(string)") for t in targets), sorted(targets)


# After the bot review: a base list declared on ANOTHER part (another file)
# of a partial class, a lowercase namespace on a type path, and a value chain on an
# external receiver (`Remote.Default`) beside a decoy type named `Default`.
REVIEW = {
    "src/Lib.cs": (
        "namespace myLib\n{\n    public static class Util\n    {\n"
        "        public static class Helper\n        {\n"
        "            public static void Run(string s) { }\n        }\n    }\n}\n\n"
        "namespace Lib\n{\n"
        "    public class BaseHandler\n    {\n        public void Handle(string s) { }\n    }\n\n"
        "    public class Default\n    {\n        public void Handle(string s) { }\n    }\n\n"
        "    public class Config\n    {\n"
        "        public void Each(System.Action<string> f) { }\n    }\n}\n"
    ),
    "src/App.cs": (
        "using Lib;\n\nnamespace App\n{\n"
        "    public partial class Bench\n    {\n"
        "        public void Run()\n        {\n            var config = new Config();\n"
        "            config.Each(base.Handle);\n"
        "            config.Each(myLib.Util.Helper.Run);\n"
        "            config.Each(Remote.Default.Handle);\n"
        "        }\n    }\n}\n"
    ),
    # A plain stem: a dotted one (`Bench.Base.cs`) defeats the syntactic
    # partial key on main until #1999 lands.
    "src/BenchBase.cs": (
        "using Lib;\n\nnamespace App\n{\n"
        "    public partial class Bench : BaseHandler\n    {\n    }\n}\n"
    ),
}


def test_a_base_list_on_another_partial_part_binds_base_handle(tmp_path: Path) -> None:
    targets = {target for _kind, target in _edges(tmp_path / "proj", REVIEW)}
    assert any(t.endswith(".BaseHandler.Handle(string)") for t in targets), sorted(
        targets
    )


def test_a_lowercase_namespace_type_path_binds(tmp_path: Path) -> None:
    targets = {target for _kind, target in _edges(tmp_path / "proj", REVIEW)}
    assert any(t.endswith(".Util.Helper.Run(string)") for t in targets), sorted(targets)


def test_an_external_value_chain_does_not_bind_a_decoy_type(tmp_path: Path) -> None:
    targets = {target for _kind, target in _edges(tmp_path / "proj", REVIEW)}
    assert not any(t.endswith(".Default.Handle(string)") for t in targets), sorted(
        targets
    )


# After the local review: a generic NON-LEAF segment (`Lib.Util<int>.Helper`)
# must keep the segments after it; a cut at the first `<` bound the outer
# type's same-name method.
GENERIC_SEGMENT = {
    "src/Lib.cs": (
        "namespace Lib\n{\n"
        "    public class Config\n    {\n"
        "        public void Each(System.Action<string> f) { }\n    }\n\n"
        "    public static class Util<T>\n    {\n"
        "        public static void Run(string s) { }\n\n"
        "        public static class Helper\n        {\n"
        "            public static void Run(string s) { }\n        }\n    }\n}\n"
    ),
    "src/App.cs": (
        "using Lib;\n\nnamespace App;\n\npublic class Bench\n{\n"
        "    public void Run()\n    {\n        var config = new Config();\n"
        "        config.Each(Lib.Util<int>.Helper.Run);\n"
        "    }\n}\n"
    ),
}


def test_a_generic_non_leaf_segment_keeps_the_leaf(tmp_path: Path) -> None:
    targets = {target for _kind, target in _edges(tmp_path / "proj", GENERIC_SEGMENT)}
    assert any(t.endswith(".Util.Helper.Run(string)") for t in targets), sorted(targets)
    assert not any(t.endswith(".Util.Run(string)") for t in targets), sorted(targets)


# After the bot review: a qualified type argument on the leaf
# (`Lib.Helper<System.String>`) must keep the leaf's arity, so the generic
# twin (declared second, so it carries the duplicate marker) is chosen
# over the non-generic one that holds the natural qn.
QUALIFIED_TYPE_ARGUMENT = {
    "src/Lib.cs": (
        "namespace Lib\n{\n"
        "    public class Config\n    {\n"
        "        public void Each(System.Action<string> f) { }\n    }\n\n"
        "    public static class Helper\n    {\n"
        "        public static void Run(string s) { }\n    }\n\n"
        "    public static class Helper<T>\n    {\n"
        "        public static void Run(string s) { }\n    }\n}\n"
    ),
    "src/App.cs": (
        "using Lib;\n\nnamespace App;\n\npublic class Bench\n{\n"
        "    public void Run()\n    {\n        var config = new Config();\n"
        "        config.Each(Lib.Helper<System.String>.Run);\n"
        "    }\n}\n"
    ),
}


def test_a_qualified_type_argument_keeps_the_leaf_arity(tmp_path: Path) -> None:
    targets = {
        target for _kind, target in _edges(tmp_path / "proj", QUALIFIED_TYPE_ARGUMENT)
    }
    assert any(
        t.startswith("proj.src.Lib.Lib.Helper@") and t.endswith(".Run(string)")
        for t in targets
    ), sorted(targets)
    assert "proj.src.Lib.Lib.Helper.Run(string)" not in targets, sorted(targets)


def test_the_duplicate_marker_strip_keeps_a_verbatim_identifier() -> None:
    """The marker is `@<line>`; a verbatim identifier's `@` is part of the name.

    `_dotted_type_path_qn` matches a candidate by its WHOLE path with the
    registration marker removed. Splitting at the FIRST `@` truncated
    `proj.src.Lib.Lib.@event@12` to `proj.src.Lib.Lib.`, so the written
    path `Lib.@event` no longer matched and the real type was rejected
    (Copilot, #1998). Driven directly: the end-to-end call resolves through
    the name trie and never reaches this filter.
    """
    from codebase_rag.utils.qn_markers import strip_all_markers

    written = "Lib.@event"
    suffix = f".{written}"
    # A verbatim-named type that is also a same-file twin, so BOTH the
    # escape and a real registration marker are present.
    qn = "proj.src.Lib.Lib.@event@12"

    assert strip_all_markers(qn) == "proj.src.Lib.Lib.@event"
    assert strip_all_markers(qn).endswith(suffix)
    # The marker is still stripped where it really is one.
    assert strip_all_markers("proj.src.Lib.Lib.Helper@12_3") == (
        "proj.src.Lib.Lib.Helper"
    )
    # ...and a plain name is untouched.
    assert strip_all_markers("proj.src.Lib.Lib.Helper") == (
        "proj.src.Lib.Lib.Helper"
    )


FOREACH_SHADOW = {
    "src/Lib.cs": (
        "namespace Lib;\n\n"
        "public class Config { public static void Each() { } }\n\n"
        "public class Item { public void Ping() { } }\n"
    ),
    "src/App.cs": (
        "using System.Collections.Generic;\nusing Lib;\n\nnamespace App;\n\n"
        "public class Bench\n{\n"
        # Both loops live in Run: the edge helper filters on `.Bench.Run`,
        # so a second method's edges would be invisible to the control.
        "    public void Run(List<string> items, List<Item> typed)\n    {\n"
        "        foreach (var Config in items) { Config.Each(); }\n"
        "        foreach (Item it in typed) { it.Ping(); }\n"
        "    }\n}\n"
    ),
}


def test_a_foreach_var_shadowing_a_class_is_not_that_class(tmp_path: Path) -> None:
    """`foreach (var Config in items)` binds a LOCAL, not the class `Config`.

    Its element type is never inferred, so the name reached neither
    `local_var_types` nor any type check and fell through to the name trie,
    which bound `Config.Each()` to the registered `Lib.Config.Each` -- a
    confident edge onto a class the loop variable has nothing to do with
    (Copilot, #1998).
    """
    edges = _edges(tmp_path / "proj", FOREACH_SHADOW)

    assert not any(target.endswith("Config.Each") for _kind, target in edges), sorted(
        edges
    )


def test_a_typed_foreach_var_still_resolves_its_own_method(tmp_path: Path) -> None:
    """The control: an EXPLICITLY typed binding declares its type and must
    keep resolving, so the refusal cannot be satisfied by suppressing every
    foreach receiver."""
    edges = _edges(tmp_path / "proj", FOREACH_SHADOW)

    assert any(target.endswith("Item.Ping") for _kind, target in edges), sorted(edges)


LOCAL_SHADOW = {
    "src/Lib.cs": (
        "using System.Collections.Generic;\n\nnamespace Lib;\n\n"
        "public class Config\n{\n"
        "    public static void Each() { }\n"
        "    public static List<string> All() { return null; }\n}\n\n"
        "public class Printer { public static void Hello() { } }\n"
    ),
    "src/App.cs": (
        "using Lib;\n\nnamespace App;\n\n"
        "public class Bench\n{\n"
        "    public void Run()\n    {\n"
        # Uninferred: `Ext.Make()` is outside the graph, so the local never
        # reaches `local_var_types`.
        "        var Config = Ext.Make();\n"
        "        { Config.Each(); }\n"
        "        Take(Config.Each);\n"
        # The known positive: a static call on a class no local shadows.
        "        Printer.Hello();\n"
        "    }\n"
        "    void Take(System.Action a) { }\n}\n"
    ),
}


def test_an_untyped_local_shadowing_a_class_is_not_that_class(tmp_path: Path) -> None:
    """`var Config = Ext.Make();` binds a LOCAL whose type is not inferred.

    It never reaches `local_var_types`, so the static-type fallback read the
    name as the class `Config` and bound both the call and the method group
    to `Lib.Config.Each` (Copilot, #2011). Declared in an ENCLOSING block, so
    the use in the inner block is covered too."""
    edges = _edges(tmp_path / "proj", LOCAL_SHADOW)

    assert any(target.endswith("Printer.Hello") for _kind, target in edges), sorted(
        edges
    )
    assert not any(target.endswith("Config.Each") for _kind, target in edges), sorted(
        edges
    )


FOREACH_COLLECTION = {
    "src/Lib.cs": LOCAL_SHADOW["src/Lib.cs"],
    "src/App.cs": (
        "using Lib;\n\nnamespace App;\n\n"
        "public class Bench\n{\n"
        "    public void Run()\n    {\n"
        "        foreach (var Config in Config.All()) { }\n"
        "    }\n}\n"
    ),
}


def test_a_foreach_collection_names_the_class_not_the_loop_variable(
    tmp_path: Path,
) -> None:
    """The loop variable is in scope in the BODY only; in the collection
    expression `Config` is still the class, so `Config.All()` binds
    (CodeRabbit, #2011)."""
    edges = _edges(tmp_path / "proj", FOREACH_COLLECTION)

    assert any(target.endswith("Config.All") for _kind, target in edges), sorted(edges)
