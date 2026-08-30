# Return and parameter types as graph facts (issue #1527): every Function and
# Method node carries `return_type` / `param_types` as written, and the names
# in those annotations become RETURNS / ACCEPTS edges when they resolve to a
# type defined in the project.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.function_registry import FunctionRegistryTrie
from codebase_rag.parsers.type_facts import (
    TypeReferenceResolver,
    type_reference_names,
)
from codebase_rag.tests.conftest import create_and_run_updater
from codebase_rag.types_defs import NodeType


def _props(mock: MagicMock, label: str) -> dict[str, dict]:
    """Properties per qualified name."""
    merged: dict[str, dict] = {}
    for c in mock.ensure_node_batch.call_args_list:
        if str(c.args[0]) != label:
            continue
        merged.setdefault(c.args[1][cs.KEY_QUALIFIED_NAME], {}).update(c.args[1])
    return merged


def _edges(mock: MagicMock, rel: str) -> set[tuple[str, str]]:
    return {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == rel
    }


def _suffix(edges: set[tuple[str, str]]) -> set[tuple[str, str]]:
    # Project names are temp-dir derived; compare on the stable tail.
    return {(s.split(".", 1)[1], d.split(".", 1)[1]) for s, d in edges}


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


# --- Python -------------------------------------------------------------------


def test_python_annotations_become_properties_and_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/models.py": (
                "class Item:\n    pass\n\n\n"
                "class Store:\n"
                "    def get(self, key: str, default: Item | None = None) -> Item:\n"
                "        return Item()\n"
            ),
            "pkg/app.py": (
                "from pkg.models import Item, Store\n\n\n"
                "def build(n: int, store: Store, *rest: str, **kw) -> list[Item]:\n"
                "    return []\n\n\n"
                "def untyped(a, b=1):\n    return a\n"
            ),
        },
    )
    create_and_run_updater(temp_repo, mock_ingestor)
    project = temp_repo.name
    functions = _props(mock_ingestor, cs.NodeLabel.FUNCTION.value)
    methods = _props(mock_ingestor, cs.NodeLabel.METHOD.value)

    build = functions[f"{project}.pkg.app.build"]
    assert build[cs.KEY_RETURN_TYPE] == "list[Item]"
    assert build[cs.KEY_PARAM_TYPES] == ["int", "Store", "str", ""]

    # Unannotated: no return_type at all (not "None"), one "" per parameter.
    untyped = functions[f"{project}.pkg.app.untyped"]
    assert cs.KEY_RETURN_TYPE not in untyped
    assert untyped[cs.KEY_PARAM_TYPES] == ["", ""]

    get = methods[f"{project}.pkg.models.Store.get"]
    assert get[cs.KEY_RETURN_TYPE] == "Item"
    assert get[cs.KEY_PARAM_TYPES] == ["", "str", "Item | None"]

    assert _suffix(_edges(mock_ingestor, cs.RelationshipType.RETURNS)) == {
        ("pkg.app.build", "pkg.models.Item"),
        ("pkg.models.Store.get", "pkg.models.Item"),
    }
    assert _suffix(_edges(mock_ingestor, cs.RelationshipType.ACCEPTS)) == {
        ("pkg.app.build", "pkg.models.Store"),
        ("pkg.models.Store.get", "pkg.models.Item"),
    }


def test_python_annotation_naming_a_later_file_still_resolves(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `a.py` parses before `z.py` defines Widget; resolution runs after Pass 2.
    _write(
        temp_repo,
        {
            "a.py": "from z import Widget\n\n\ndef make() -> Widget:\n    return Widget()\n",
            "z.py": "class Widget:\n    pass\n",
        },
    )
    create_and_run_updater(temp_repo, mock_ingestor)
    assert _suffix(_edges(mock_ingestor, cs.RelationshipType.RETURNS)) == {
        ("a.make", "z.Widget")
    }


# --- Other languages ----------------------------------------------------------


TS_SRC = (
    "export class Item {}\n"
    "export interface Repo {}\n"
    "export function load(id: number, repo: Repo, cb): Promise<Item> {\n"
    "  return null as any;\n"
    "}\n"
    "export class Svc {\n"
    "  find(name: string): Item | undefined { return undefined; }\n"
    "}\n"
)

GO_SRC = (
    "package main\n\n"
    "type Item struct{}\n"
    "type Repo interface{}\n\n"
    "func Load(id int, repo Repo, a, b string, rest ...int) (*Item, error) {\n"
    "\treturn nil, nil\n"
    "}\n\n"
    "type Svc struct{}\n\n"
    "func (s *Svc) Find(name string) Item { return Item{} }\n"
)

JAVA_SRC = "package app;\n\npublic class Item {}\n"
JAVA_SVC = (
    "package app;\n\n"
    "import java.util.List;\n\n"
    "public class Svc {\n"
    "    public List<Item> load(int id, Item seed, String... names) { return null; }\n"
    "    public void noop() {}\n"
    "}\n"
)

RUST_SRC = (
    "pub struct Item;\n"
    "pub trait Repo {}\n\n"
    "pub fn load(id: u32, repo: &dyn Repo) -> Option<Item> { None }\n\n"
    "impl Item {\n"
    "    pub fn find(&self, name: &str) -> Item { Item }\n"
    "}\n"
)

CSHARP_SRC = (
    "namespace App {\n"
    "    public class Item {}\n"
    "    public interface IRepo {}\n"
    "    public class Svc {\n"
    "        public Item Load(int id, IRepo repo) { return null; }\n"
    "        public void Noop() {}\n"
    "    }\n"
    "}\n"
)


def test_typescript_annotations(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    _write(temp_repo, {"svc.ts": TS_SRC})
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="typescript")
    project = temp_repo.name
    load = _props(mock_ingestor, cs.NodeLabel.FUNCTION.value)[f"{project}.svc.load"]
    assert load[cs.KEY_RETURN_TYPE] == "Promise<Item>"
    assert load[cs.KEY_PARAM_TYPES] == ["number", "Repo", ""]
    find = _props(mock_ingestor, cs.NodeLabel.METHOD.value)[f"{project}.svc.Svc.find"]
    assert find[cs.KEY_RETURN_TYPE] == "Item | undefined"
    assert find[cs.KEY_PARAM_TYPES] == ["string"]
    assert _suffix(_edges(mock_ingestor, cs.RelationshipType.RETURNS)) == {
        ("svc.load", "svc.Item"),
        ("svc.Svc.find", "svc.Item"),
    }
    assert _suffix(_edges(mock_ingestor, cs.RelationshipType.ACCEPTS)) == {
        ("svc.load", "svc.Repo"),
    }


def test_go_annotations(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    _write(temp_repo, {"go.mod": "module app\n\ngo 1.21\n", "main.go": GO_SRC})
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="go")
    functions = _props(mock_ingestor, cs.NodeLabel.FUNCTION.value)
    load = next(p for qn, p in functions.items() if qn.endswith(".Load"))
    assert load[cs.KEY_RETURN_TYPE] == "(*Item, error)"
    assert load[cs.KEY_PARAM_TYPES] == ["int", "Repo", "string", "string", "...int"]
    methods = _props(mock_ingestor, cs.NodeLabel.METHOD.value)
    find = next(p for qn, p in methods.items() if qn.endswith(".Find"))
    assert find[cs.KEY_RETURN_TYPE] == "Item"
    assert find[cs.KEY_PARAM_TYPES] == ["string"]
    returns = _edges(mock_ingestor, cs.RelationshipType.RETURNS)
    assert {(s.rsplit(".", 1)[1], d.rsplit(".", 1)[1]) for s, d in returns} == {
        ("Load", "Item"),
        ("Find", "Item"),
    }
    accepts = _edges(mock_ingestor, cs.RelationshipType.ACCEPTS)
    assert {(s.rsplit(".", 1)[1], d.rsplit(".", 1)[1]) for s, d in accepts} == {
        ("Load", "Repo"),
    }


def test_java_annotations(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    _write(temp_repo, {"app/Item.java": JAVA_SRC, "app/Svc.java": JAVA_SVC})
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="java")
    methods = _props(mock_ingestor, cs.NodeLabel.METHOD.value)
    load = next(p for qn, p in methods.items() if ".Svc.load(" in qn)
    assert load[cs.KEY_RETURN_TYPE] == "List<Item>"
    assert load[cs.KEY_PARAM_TYPES] == ["int", "Item", "String..."]
    noop = next(p for qn, p in methods.items() if ".Svc.noop(" in qn)
    assert noop[cs.KEY_RETURN_TYPE] == "void"
    assert noop[cs.KEY_PARAM_TYPES] == []
    returns = _edges(mock_ingestor, cs.RelationshipType.RETURNS)
    assert {d.rsplit(".", 1)[1] for _s, d in returns} == {"Item"}
    accepts = _edges(mock_ingestor, cs.RelationshipType.ACCEPTS)
    assert {d.rsplit(".", 1)[1] for _s, d in accepts} == {"Item"}


def test_rust_annotations(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    _write(
        temp_repo,
        {
            "Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "src/lib.rs": RUST_SRC,
        },
    )
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="rust")
    functions = _props(mock_ingestor, cs.NodeLabel.FUNCTION.value)
    load = next(p for qn, p in functions.items() if qn.endswith(".load"))
    assert load[cs.KEY_RETURN_TYPE] == "Option<Item>"
    assert load[cs.KEY_PARAM_TYPES] == ["u32", "&dyn Repo"]
    methods = _props(mock_ingestor, cs.NodeLabel.METHOD.value)
    find = next(p for qn, p in methods.items() if qn.endswith(".find"))
    assert find[cs.KEY_RETURN_TYPE] == "Item"
    assert find[cs.KEY_PARAM_TYPES] == ["", "&str"]
    returns = _edges(mock_ingestor, cs.RelationshipType.RETURNS)
    assert {(s.rsplit(".", 1)[1], d.rsplit(".", 1)[1]) for s, d in returns} == {
        ("load", "Item"),
        ("find", "Item"),
    }
    accepts = _edges(mock_ingestor, cs.RelationshipType.ACCEPTS)
    assert {(s.rsplit(".", 1)[1], d.rsplit(".", 1)[1]) for s, d in accepts} == {
        ("load", "Repo"),
    }


def test_csharp_annotations(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    _write(temp_repo, {"Svc.cs": CSHARP_SRC})
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="c_sharp")
    methods = _props(mock_ingestor, cs.NodeLabel.METHOD.value)
    load = next(p for qn, p in methods.items() if ".Svc.Load" in qn)
    assert load[cs.KEY_RETURN_TYPE] == "Item"
    assert load[cs.KEY_PARAM_TYPES] == ["int", "IRepo"]
    noop = next(p for qn, p in methods.items() if ".Svc.Noop" in qn)
    assert noop[cs.KEY_RETURN_TYPE] == "void"
    assert noop[cs.KEY_PARAM_TYPES] == []
    returns = _edges(mock_ingestor, cs.RelationshipType.RETURNS)
    assert {d.rsplit(".", 1)[1] for _s, d in returns} == {"Item"}
    accepts = _edges(mock_ingestor, cs.RelationshipType.ACCEPTS)
    assert {d.rsplit(".", 1)[1] for _s, d in accepts} == {"IRepo"}


def test_uncovered_language_has_no_type_properties(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """Absent, not empty: "kinds unknown" must not read as "no parameters"."""
    _write(temp_repo, {"app.lua": "function greet(name)\n  return name\nend\n"})
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="lua")
    functions = _props(mock_ingestor, cs.NodeLabel.FUNCTION.value)
    greet = next(p for qn, p in functions.items() if qn.endswith(".greet"))
    assert cs.KEY_RETURN_TYPE not in greet
    assert cs.KEY_PARAM_TYPES not in greet
    assert not _edges(mock_ingestor, cs.RelationshipType.RETURNS)


# --- Resolver -----------------------------------------------------------------


def test_type_reference_names_strip_generics_and_paths() -> None:
    assert type_reference_names("Optional[list[pkg.Item]]") == [
        "Optional",
        "list",
        "pkg.Item",
    ]
    assert type_reference_names("Result<crate::model::Item, E>") == [
        "Result",
        "crate.model.Item",
        "E",
    ]
    assert type_reference_names("(*Item, error)") == ["Item", "error"]
    assert type_reference_names("") == []


def _resolver(
    entries: dict[str, NodeType], imports: dict[str, dict[str, str]]
) -> TypeReferenceResolver:
    registry = FunctionRegistryTrie()
    for qn, node_type in entries.items():
        registry[qn] = node_type
    return TypeReferenceResolver(registry, imports, "p")


def test_resolver_prefers_imports_then_scope_then_unique_suffix() -> None:
    resolver = _resolver(
        {
            "p.models.Item": NodeType.CLASS,
            "p.app.Item": NodeType.CLASS,
            "p.other.Lone": NodeType.INTERFACE,
            "p.app.helper": NodeType.FUNCTION,
        },
        {"p.svc": {"Item": "p.models.Item"}},
    )
    # Import binding wins over any suffix match.
    assert resolver.resolve("Item", "p.svc") == "p.models.Item"
    # Same module wins when nothing is imported.
    assert resolver.resolve("Item", "p.app") == "p.app.Item"
    # A unique project type resolves from anywhere.
    assert resolver.resolve("Lone", "p.svc") == "p.other.Lone"
    # A function is not a type.
    assert resolver.resolve("helper", "p.app") is None
    # Two equally distant candidates stay unresolved rather than guessed.
    assert resolver.resolve("Item", "p.elsewhere") is None
    # Builtins resolve to nothing.
    assert resolver.resolve("int", "p.app") is None


def test_resolver_prefers_the_nearest_package_among_candidates() -> None:
    resolver = _resolver({"p.a.x.Item": NodeType.CLASS, "p.b.Item": NodeType.CLASS}, {})
    assert resolver.resolve("Item", "p.a.y") == "p.a.x.Item"


@pytest.mark.parametrize(
    ("label", "rel"),
    [
        (cs.NodeLabel.FUNCTION, cs.RelationshipType.RETURNS),
        (cs.NodeLabel.METHOD, cs.RelationshipType.ACCEPTS),
    ],
)
def test_schema_documents_the_new_triples_and_properties(
    label: cs.NodeLabel, rel: cs.RelationshipType
) -> None:
    from codebase_rag.graph_audit import (
        documented_node_properties,
        documented_relationship_triples,
    )

    props = documented_node_properties()[label.value]
    assert props[cs.KEY_RETURN_TYPE] is False
    assert props[cs.KEY_PARAM_TYPES] is False
    triples = documented_relationship_triples()
    for target in (
        cs.NodeLabel.CLASS,
        cs.NodeLabel.INTERFACE,
        cs.NodeLabel.ENUM,
        cs.NodeLabel.TYPE,
        cs.NodeLabel.UNION,
    ):
        assert (label.value, rel.value, target.value) in triples


# --- Review findings on PR #1539 ----------------------------------------------


def test_csharp_params_array_is_kept(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    """`params Item[] items` sits under the parameter list as a bare
    array_type; it must still count as a parameter and resolve to Item."""
    _write(
        temp_repo,
        {
            "Svc.cs": (
                "namespace App {\n"
                "    public class Item {}\n"
                "    public class Svc {\n"
                "        public void Add(int n, params Item[] items) {}\n"
                "    }\n"
                "}\n"
            )
        },
    )
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="c_sharp")
    methods = _props(mock_ingestor, cs.NodeLabel.METHOD.value)
    add = next(p for qn, p in methods.items() if ".Svc.Add" in qn)
    assert add[cs.KEY_PARAM_TYPES] == ["int", "params Item[]"]
    accepts = _edges(mock_ingestor, cs.RelationshipType.ACCEPTS)
    assert {d.rsplit(".", 1)[1] for _s, d in accepts} == {"Item"}


def test_incremental_run_requeues_unchanged_definitions(temp_repo: Path) -> None:
    """A changed file adding the first resolvable `Widget` gives an UNCHANGED
    function's `-> Widget` annotation its RETURNS edge without re-parsing it."""
    from codebase_rag.graph_updater import GraphUpdater
    from codebase_rag.parser_loader import load_parsers
    from evals.cgr_graph import _StatefulIngestor

    root = temp_repo / "requeue"
    _write(root, {"a.py": "def make() -> Widget:\n    return None\n"})
    parsers, queries = load_parsers()
    store = _StatefulIngestor()

    def run(force: bool) -> None:
        GraphUpdater(
            ingestor=store,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name="requeue",
        ).run(force=force)

    run(force=True)
    assert not {e for e in store.edges if e[2] == cs.RelationshipType.RETURNS.value}

    cache = root / cs.HASH_CACHE_FILENAME
    _write(root, {"z.py": "class Widget:\n    pass\n"})
    future = cache.stat().st_mtime + 5
    import os

    os.utime(root / "z.py", (future, future))
    run(force=False)

    returns = {
        (e[1], e[4]) for e in store.edges if e[2] == cs.RelationshipType.RETURNS.value
    }
    assert returns == {("requeue.a.make", "requeue.z.Widget")}


def test_protobuf_export_carries_the_annotations(tmp_path: Path) -> None:
    import codec.schema_pb2 as pb
    from codebase_rag.services.protobuf_service import ProtobufFileIngestor

    ingestor = ProtobufFileIngestor(str(tmp_path), split_index=False)
    ingestor.ensure_node_batch(
        cs.NodeLabel.FUNCTION,
        {
            cs.KEY_QUALIFIED_NAME: "p.m.f",
            cs.KEY_NAME: "f",
            cs.KEY_PATH: "m.py",
            cs.KEY_RETURN_TYPE: "list[Item]",
            cs.KEY_PARAM_TYPES: ["int", ""],
        },
    )
    ingestor.flush_all()
    index = pb.GraphCodeIndex()
    index.ParseFromString((tmp_path / "index.bin").read_bytes())
    (node,) = index.nodes
    assert node.function.return_type == "list[Item]"
    assert list(node.function.param_types) == ["int", ""]
