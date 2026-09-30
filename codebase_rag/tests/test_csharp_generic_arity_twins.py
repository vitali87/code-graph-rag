"""C# `Foo` and `Foo<T>` are two types, `Foo` and ``Foo`1`` (issue #2579).

A non-generic type and a generic one sharing a simple name used to register
under one name plus a line-numbered duplicate (`PB` and `PB@8`), and nothing
that constructs, calls, extends or names one of them looked at the written
generic arity: a fresh index fanned every `new PB(...)` and `new
PB<int>(...)` out to both, an incremental one bound them all to the
non-generic type, and base lists and parameter types took whichever the name
search found first. The generic twin now carries its CLR arity in its
qualified name, and every reference binds the declaration its written arity
names, the same way from a clean index and from an incremental one.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import pytest
from tree_sitter import Parser

import codec.schema_pb2 as pb
from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.protobuf_service import ProtobufFileIngestor
from codebase_rag.types_defs import LanguageQueries
from evals.cgr_graph import _StatefulIngestor

PLAIN_SOURCE = (
    "public partial class PB\n{\n"
    "    internal PB(System.Func<System.Exception, System.Exception?> p) { }\n"
    "    public static PB Make() => null;\n}\n"
)
GENERIC_SOURCE = (
    "public partial class PB<TResult>\n{\n"
    "    internal PB(System.Func<TResult, bool> p) { }\n"
    "    public static PB<TResult> Make() => null;\n}\n"
)
USE_SOURCE = (
    "namespace Acme;\n"
    "public static class Explicit\n{\n"
    "    public static object NonGeneric() => new PB(e => e);\n"
    "    public static object GenericInt() => new PB<int>(x => x > 0);\n"
    "    public static PB<string> TargetTyped() => new(s => s.Length > 0);\n"
    "    public static object StaticPlain() => PB.Make();\n"
    "    public static object StaticGeneric() => PB<int>.Make();\n"
    "    public static void TakesPlain(PB p) { }\n"
    "    public static void TakesGeneric(PB<int> p) { }\n}\n"
    "public class FromPlain : PB { }\n"
    "public class FromGeneric : PB<int> { }\n"
)
NAMESPACE = "namespace Acme;\n"
USE_PATH = "src/Acme/Explicit.cs"

# (files declaring the pair, the non-generic qn, the generic qn). The
# directory spells the namespace, so it folds out of every qn.
LAYOUTS = {
    "one_file": (
        {"src/Acme/Policy.cs": NAMESPACE + PLAIN_SOURCE + "\n" + GENERIC_SOURCE},
        "proj.src.Acme.Policy.PB",
        "proj.src.Acme.Policy.PB`1",
    ),
    # Polly's own layout: the generic twin in `<Name>.TResult.cs`.
    "two_files": (
        {
            "src/Acme/PB.cs": NAMESPACE + PLAIN_SOURCE,
            "src/Acme/PB.TResult.cs": NAMESPACE + GENERIC_SOURCE,
        },
        "proj.src.Acme.PB.PB",
        "proj.src.Acme.PB.TResult.PB",
    ),
}
PAIR_RELS = (
    cs.RelationshipType.INSTANTIATES,
    cs.RelationshipType.CALLS,
    cs.RelationshipType.INHERITS,
    cs.RelationshipType.ACCEPTS,
    cs.RelationshipType.RETURNS,
)


def _parsers() -> tuple[
    Mapping[cs.SupportedLanguage, Parser],
    Mapping[cs.SupportedLanguage, LanguageQueries],
]:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.CSHARP not in parsers:
        pytest.skip("c_sharp parser not available")
    return parsers, queries


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _run(store: _StatefulIngestor, root: Path, force: bool) -> None:
    parsers, queries = _parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run(force=force)


def _index(root: Path, files: dict[str, str]) -> _StatefulIngestor:
    _write(root, files)
    store = _StatefulIngestor()
    _run(store, root, force=True)
    return store


def _targets(
    store: _StatefulIngestor, rel: cs.RelationshipType, member: str
) -> set[str]:
    # A C# method qn carries its parameter signature; match on the name.
    return {
        str(target)
        for _sl, source, kind, _tl, target in store.edges
        if kind == rel.value
        and str(source).split(cs.CHAR_PAREN_OPEN, 1)[0].endswith(f".{member}")
    }


def _resolutions(
    store: _StatefulIngestor, rel: cs.RelationshipType, member: str
) -> set[object]:
    return {
        store.props_for(edge).get(cs.KEY_RESOLUTION)
        for edge in store.edges
        if edge[2] == rel.value
        and str(edge[1]).split(cs.CHAR_PAREN_OPEN, 1)[0].endswith(f".{member}")
    }


def _class_nodes(store: _StatefulIngestor) -> dict[str, dict]:
    return {
        str(uid): props
        for (label, uid), props in store.nodes.items()
        if label == cs.NodeLabel.CLASS.value
    }


USE_SOURCES = (".Explicit.", ".FromPlain", ".FromGeneric")


def _pair_edges(store: _StatefulIngestor) -> set[tuple[object, ...]]:
    # Every edge leaving the using file, with its resolution, so a clean
    # index and an incremental one can be compared whole.
    rels = {rel.value for rel in PAIR_RELS}
    return {
        (*edge, store.props_for(edge).get(cs.KEY_RESOLUTION))
        for edge in store.edges
        if edge[2] in rels and any(part in str(edge[1]) for part in USE_SOURCES)
    }


@pytest.fixture(params=sorted(LAYOUTS))
def layout(request: pytest.FixtureRequest) -> tuple[dict[str, str], str, str]:
    return LAYOUTS[request.param]


@pytest.fixture
def indexed(
    tmp_path: Path, layout: tuple[dict[str, str], str, str]
) -> tuple[_StatefulIngestor, str, str]:
    files, plain, generic = layout
    store = _index(tmp_path / "proj", {**files, USE_PATH: USE_SOURCE})
    return store, plain, generic


class TestTheGenericTwinIsItsOwnType:
    def test_the_generic_twin_carries_its_clr_arity_in_its_name(
        self, tmp_path: Path
    ) -> None:
        files, plain, generic = LAYOUTS["one_file"]
        store = _index(tmp_path / "proj", files)
        classes = _class_nodes(store)
        assert set(classes) == {plain, generic}, sorted(classes)
        # The simple name stays the written one; only the qn tells them apart.
        assert classes[plain][cs.KEY_NAME] == "PB"
        assert classes[generic][cs.KEY_NAME] == "PB"

    def test_a_type_nested_in_the_generic_twin_is_named_under_it(
        self, tmp_path: Path
    ) -> None:
        store = _index(
            tmp_path / "proj",
            {
                "src/Acme/Outer.cs": NAMESPACE
                + "public class Outer { public class In { } }\n"
                + "public class Outer<T> { public class In { } }\n"
            },
        )
        base = "proj.src.Acme.Outer"
        assert set(_class_nodes(store)) == {
            f"{base}.Outer",
            f"{base}.Outer.In",
            f"{base}.Outer`1",
            f"{base}.Outer`1.In",
        }, sorted(_class_nodes(store))

    @pytest.mark.parametrize(
        "source",
        [
            "namespace Acme { public class Pair { } }\n"
            "namespace Acme { public class Pair<T> { } }\n",
            NAMESPACE
            + "public class Pair { }\n#if NET8\npublic class Pair<T> { }\n#endif\n",
        ],
        ids=["two_namespace_blocks", "conditional_block"],
    )
    def test_the_scope_is_what_the_qualified_name_is_built_from(
        self, tmp_path: Path, source: str
    ) -> None:
        store = _index(tmp_path / "proj", {"src/Acme/Pair.cs": source})
        base = "proj.src.Acme.Pair"
        assert set(_class_nodes(store)) == {f"{base}.Pair", f"{base}.Pair`1"}

    def test_each_arity_of_a_family_is_named_for_its_own_arity(
        self, tmp_path: Path
    ) -> None:
        store = _index(
            tmp_path / "proj",
            {
                "src/Acme/Tuple.cs": NAMESPACE
                + "public class Tup<T1> { }\n"
                + "public class Tup<T1, T2> { }\n"
            },
        )
        base = "proj.src.Acme.Tuple"
        assert set(_class_nodes(store)) == {f"{base}.Tup`1", f"{base}.Tup`2"}


class TestEachReferenceBindsItsWrittenArity:
    def test_each_construction_instantiates_its_own_type(
        self, indexed: tuple[_StatefulIngestor, str, str]
    ) -> None:
        store, plain, generic = indexed
        rel = cs.RelationshipType.INSTANTIATES
        assert _targets(store, rel, "Explicit.NonGeneric") == {plain}
        assert _targets(store, rel, "Explicit.GenericInt") == {generic}
        assert _targets(store, rel, "Explicit.TargetTyped") == {generic}
        for member in ("NonGeneric", "GenericInt", "TargetTyped"):
            assert cs.EdgeResolution.OVERLOAD not in _resolutions(
                store, rel, f"Explicit.{member}"
            ), member

    def test_each_construction_runs_its_own_constructor(
        self, indexed: tuple[_StatefulIngestor, str, str]
    ) -> None:
        store, plain, generic = indexed
        rel = cs.RelationshipType.CALLS
        assert _targets(store, rel, "Explicit.NonGeneric") == {
            f"{plain}.PB(System.Func)"
        }
        assert _targets(store, rel, "Explicit.GenericInt") == {
            f"{generic}.PB(System.Func)"
        }
        assert _targets(store, rel, "Explicit.TargetTyped") == {
            f"{generic}.PB(System.Func)"
        }

    def test_a_static_call_binds_the_written_arity(
        self, indexed: tuple[_StatefulIngestor, str, str]
    ) -> None:
        store, plain, generic = indexed
        rel = cs.RelationshipType.CALLS
        assert _targets(store, rel, "Explicit.StaticPlain") == {f"{plain}.Make"}
        assert _targets(store, rel, "Explicit.StaticGeneric") == {f"{generic}.Make"}

    def test_a_base_list_binds_the_written_arity(
        self, indexed: tuple[_StatefulIngestor, str, str]
    ) -> None:
        store, plain, generic = indexed
        rel = cs.RelationshipType.INHERITS
        assert _targets(store, rel, "FromPlain") == {plain}
        assert _targets(store, rel, "FromGeneric") == {generic}

    def test_parameter_and_return_types_bind_the_written_arity(
        self, indexed: tuple[_StatefulIngestor, str, str]
    ) -> None:
        store, plain, generic = indexed
        accepts = cs.RelationshipType.ACCEPTS
        assert _targets(store, accepts, "Explicit.TakesPlain") == {plain}
        assert _targets(store, accepts, "Explicit.TakesGeneric") == {generic}
        returns = cs.RelationshipType.RETURNS
        assert _targets(store, returns, "Explicit.TargetTyped") == {generic}


def test_an_incremental_run_binds_like_a_clean_index(
    tmp_path: Path, layout: tuple[dict[str, str], str, str]
) -> None:
    # The pair is indexed first; the using file arrives in an incremental
    # run, which knows the pair only from what the graph stores.
    files, plain, generic = layout
    root = tmp_path / "proj"
    store = _index(root, files)
    use = root / USE_PATH
    cache_mtime = (root / cs.HASH_CACHE_FILENAME).stat().st_mtime
    use.write_text(USE_SOURCE, encoding="utf-8")
    os.utime(use, (cache_mtime + 1, cache_mtime + 1))
    _run(store, root, force=False)

    clean = _StatefulIngestor()
    _run(clean, root, force=True)
    assert _pair_edges(store) == _pair_edges(clean)
    rel = cs.RelationshipType.INSTANTIATES
    assert _targets(store, rel, "Explicit.GenericInt") == {generic}
    assert _targets(store, rel, "Explicit.NonGeneric") == {plain}


def test_a_protobuf_export_carries_the_generic_arity(tmp_path: Path) -> None:
    # The exporter copies only properties the payload message declares, so an
    # undeclared arity would vanish from an exported index without a word.
    files, plain, generic = LAYOUTS["one_file"]
    repo = tmp_path / "proj"
    shape = "public interface IShape { }\npublic interface IShape<T> { }\n"
    _write(repo, {**files, "src/Acme/Shape.cs": NAMESPACE + shape})
    parsers, queries = _parsers()
    out = tmp_path / "out"
    exporter = ProtobufFileIngestor(output_path=str(out), repo_path=str(repo))
    GraphUpdater(
        ingestor=exporter,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run(force=True)
    exporter.flush_all()
    index = pb.GraphCodeIndex()
    index.ParseFromString((out / "index.bin").read_bytes())
    arities = {
        payload.qualified_name: payload.generic_arity
        for node in index.nodes
        if (kind := node.WhichOneof("payload")) in (cs.ONEOF_CLASS, cs.ONEOF_INTERFACE)
        for payload in (getattr(node, kind),)
    }
    # proto3 reads an absent int as 0, which is what "not generic" means here.
    assert arities[plain] == 0
    assert arities[generic] == 1
    interfaces = {qn: n for qn, n in arities.items() if ".IShape" in qn}
    assert sorted(interfaces.values()) == [0, 1], interfaces
    assert all(qn.endswith("`1") == (n == 1) for qn, n in interfaces.items())


def test_a_target_typed_new_argument_binds_the_parameter_type(tmp_path: Path) -> None:
    # C# types `new(...)` in an argument from the parameter the resolved
    # callee declares there.
    store = _index(
        tmp_path / "proj",
        {
            "src/Acme/Builder.cs": NAMESPACE
            + "public sealed class Builder { internal Builder(int seed) { } }\n"
            + "public static class Factory\n{\n"
            + "    public static int Arg() => Use(new(4));\n"
            + "    private static int Use(Builder b) => 0;\n}\n"
        },
    )
    builder = "proj.src.Acme.Builder.Builder"
    assert _targets(store, cs.RelationshipType.INSTANTIATES, "Factory.Arg") == {builder}
    assert f"{builder}.Builder(int)" in _targets(
        store, cs.RelationshipType.CALLS, "Factory.Arg"
    )


def test_a_target_typed_new_argument_is_not_guessed_between_twins(
    tmp_path: Path,
) -> None:
    # A parameter type comes from the callee's signature, which spells no
    # arity, so `PB` against `PB<TResult>` stays unresolved; so does a named
    # argument, which binds by a parameter name the signature does not keep.
    files, plain, generic = LAYOUTS["one_file"]
    store = _index(
        tmp_path / "proj",
        {
            **files,
            USE_PATH: NAMESPACE
            + "public sealed class Builder { internal Builder(int seed) { } }\n"
            + "public static class Factory\n{\n"
            + "    public static int Twin() => Use(new(x => x > 0));\n"
            + "    public static int Named() => Build(b: new(4));\n"
            + "    private static int Use(PB<int> p) => 0;\n"
            + "    private static int Build(Builder b) => 0;\n}\n",
        },
    )
    rel = cs.RelationshipType.INSTANTIATES
    assert _targets(store, rel, "Factory.Twin") == set()
    assert _targets(store, rel, "Factory.Named") == set()


class TestWhatStaysAsItWas:
    def test_a_generic_type_beside_a_twin_in_another_scope_keeps_its_bare_name(
        self, tmp_path: Path
    ) -> None:
        store = _index(
            tmp_path / "proj",
            {
                "src/Acme/Pair.cs": "namespace Left { public class Pair { } }\n"
                "namespace Right { public class Pair<T> { } }\n"
            },
        )
        base = "proj.src.Acme.Pair"
        assert set(_class_nodes(store)) == {
            f"{base}.Left.Pair",
            f"{base}.Right.Pair",
        }

    def test_a_lone_non_generic_type_keeps_its_bare_name(self, tmp_path: Path) -> None:
        store = _index(
            tmp_path / "proj",
            {
                "src/Acme/Widget.cs": NAMESPACE
                + "public class Widget { public Widget() { } }\n"
                + "public class Use { public object Run() => new Widget(); }\n"
            },
        )
        widget = "proj.src.Acme.Widget.Widget"
        assert widget in _class_nodes(store)
        rel = cs.RelationshipType.INSTANTIATES
        assert _targets(store, rel, "Use.Run") == {widget}
        assert _resolutions(store, rel, "Use.Run") == {cs.EdgeResolution.EXACT}

    def test_a_lone_generic_type_keeps_its_bare_name_and_binds_exactly(
        self, tmp_path: Path
    ) -> None:
        store = _index(
            tmp_path / "proj",
            {
                "src/Acme/Box.cs": NAMESPACE
                + "public class Box<T> { public Box() { } public static Box<T> Make() => null; }\n",
                "src/Acme/Use.cs": NAMESPACE
                + "public class Use\n{\n"
                + "    public object Build() => new Box<int>();\n"
                + "    public object Static() => Box<int>.Make();\n"
                + "    public void Takes(Box<int> b) { }\n}\n"
                + "public class Sub : Box<int> { }\n",
            },
        )
        box = "proj.src.Acme.Box.Box"
        assert set(_class_nodes(store)) >= {box}
        assert not any("`" in qn for qn in _class_nodes(store))
        rel = cs.RelationshipType.INSTANTIATES
        assert _targets(store, rel, "Use.Build") == {box}
        assert _resolutions(store, rel, "Use.Build") == {cs.EdgeResolution.EXACT}
        assert _targets(store, cs.RelationshipType.CALLS, "Use.Static") == {
            f"{box}.Make"
        }
        assert _targets(store, cs.RelationshipType.ACCEPTS, "Use.Takes") == {box}
        assert _targets(store, cs.RelationshipType.INHERITS, "Sub") == {box}

    @pytest.mark.parametrize("with_twin", [False, True], ids=["alone", "beside_plain"])
    @pytest.mark.parametrize("one_file", [False, True], ids=["two_files", "one_file"])
    def test_partial_parts_of_one_generic_type_still_merge(
        self, tmp_path: Path, with_twin: bool, one_file: bool
    ) -> None:
        plain = "public class Box { public void Plain() { } }\n" if with_twin else ""
        part_a = "public partial class Box<T> { public void FromA() { } }\n"
        part_b = "public partial class Box<T> { public void FromB() { } }\n"
        files = (
            {"src/Acme/Box.cs": NAMESPACE + plain + part_a + part_b}
            if one_file
            else {
                "src/Acme/Box.cs": NAMESPACE + plain + part_a,
                "src/Acme/Box.Part.cs": NAMESPACE + part_b,
            }
        )
        files["src/Acme/Use.cs"] = (
            NAMESPACE + "public class Use\n{\n"
            "    public void Run()\n    {\n"
            "        var b = new Box<int>();\n"
            "        b.FromA();\n        b.FromB();\n    }\n}\n"
        )
        store = _index(tmp_path / "proj", files)
        called = {
            target.rsplit(cs.SEPARATOR_DOT, 1)[-1]
            for target in _targets(store, cs.RelationshipType.CALLS, "Use.Run")
        }
        assert {"FromA", "FromB"} <= called, sorted(called)
        assert "Plain" not in called

    def test_other_languages_keep_their_generic_names(self, tmp_path: Path) -> None:
        parsers, _queries = _parsers()
        files = {
            "src/Box.java": "class Box<T> { }\nclass Use { Object r() { return new Box<String>(); } }\n"
        }
        if cs.SupportedLanguage.TS in parsers:
            files["src/box.ts"] = (
                "export class Tray<T> { }\n"
                "export function make(): Tray<number> { return new Tray<number>(); }\n"
            )
        store = _index(tmp_path / "proj", files)
        classes = set(_class_nodes(store))
        assert "proj.src.Box.Box" in classes, sorted(classes)
        assert not any("`" in qn for qn in classes), sorted(classes)
        assert _targets(store, cs.RelationshipType.INSTANTIATES, "Use.r") == {
            "proj.src.Box.Box"
        }
        if cs.SupportedLanguage.TS in parsers:
            assert "proj.src.box.Tray" in classes
            assert _targets(store, cs.RelationshipType.INSTANTIATES, "box.make") == {
                "proj.src.box.Tray"
            }
