"""Java hierarchy lookups read each file's AST once per call pass (#2470).

Resolving a Java call asks for a class's superclass, its interfaces and a
module's first declared type again and again: once per call site, and once
per level of the superclass chain for an inherited method. Each answer came
from a fresh recursive walk of the whole compilation unit, so gson's sync
spent most of its time re-walking the same ASTs. The answers depend only on
the file's tree, which is fixed for the length of one call pass.

The per-lookup walkers this replaced are kept below as plain functions, so
the equivalence tests compare against a reference that does not move with
the fix.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from tree_sitter import Language, Node, Parser

from codebase_rag import constants as cs
from codebase_rag.ast_cache import BoundedASTCache
from codebase_rag.function_registry import FunctionRegistryTrie
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.import_processor import ImportProcessor
from codebase_rag.parsers.java.type_inference import JavaTypeInferenceEngine
from codebase_rag.parsers.java.utils import (
    get_class_context_from_qn,
    get_root_node_from_module_qn,
)
from codebase_rag.parsers.utils import safe_decode_text
from codebase_rag.tests.conftest import (
    MockNode,
    create_mock_node,
    force_mtime_after_cache,
)
from codebase_rag.types_defs import PropertyValue
from evals.cgr_graph import _StatefulIngestor

tree_sitter_java = pytest.importorskip("tree_sitter_java")

_CALLS = cs.RelationshipType.CALLS.value
_LookupAnswer = str | list[str] | None
_HIERARCHY_LOOKUPS = (
    "_get_superclass_name",
    "_get_implemented_interfaces",
    "_get_current_class_name",
)


# --- counting the walks -----------------------------------------------------


@dataclass
class _WalkCounter:
    lookups: int = 0
    file_walks: int = 0
    visits: int = 0
    depth: int = 0


class _CountingNode:
    """A tree-sitter node that counts every expansion a walk makes of it.

    Only roots handed out by `ast_cache.load` while a hierarchy lookup runs
    are wrapped, so the count is the work those lookups do on ASTs and
    nothing else. Expanding a root is the start of one walk of that file.
    """

    __slots__ = ("_node", "_counter", "_is_root")

    def __init__(
        self, node: Node, counter: _WalkCounter, is_root: bool = False
    ) -> None:
        self._node = node
        self._counter = counter
        self._is_root = is_root

    @property
    def type(self) -> str:
        return self._node.type

    @property
    def text(self) -> bytes | None:
        return self._node.text

    @property
    def children(self) -> list[_CountingNode]:
        self._counter.visits += 1
        if self._is_root:
            self._counter.file_walks += 1
        return [_CountingNode(child, self._counter) for child in self._node.children]

    def child_by_field_name(self, name: str) -> _CountingNode | None:
        child = self._node.child_by_field_name(name)
        return None if child is None else _CountingNode(child, self._counter)


def _count_hierarchy_walks(monkeypatch: pytest.MonkeyPatch) -> _WalkCounter:
    counter = _WalkCounter()
    load = BoundedASTCache.load

    def counting_load(
        self: BoundedASTCache, key: Path
    ) -> tuple[Node | _CountingNode, cs.SupportedLanguage] | None:
        entry = load(self, key)
        if entry is None or not counter.depth:
            return entry
        root = _CountingNode(entry[0], counter, is_root=True)
        return root, entry[1]

    monkeypatch.setattr(BoundedASTCache, "load", counting_load)
    for name in _HIERARCHY_LOOKUPS:
        monkeypatch.setattr(
            JavaTypeInferenceEngine,
            name,
            _counted(getattr(JavaTypeInferenceEngine, name), counter),
        )
    return counter


def _counted(
    lookup: Callable[[JavaTypeInferenceEngine, str], _LookupAnswer],
    counter: _WalkCounter,
) -> Callable[[JavaTypeInferenceEngine, str], _LookupAnswer]:
    def counted(self: JavaTypeInferenceEngine, qn: str) -> _LookupAnswer:
        counter.lookups += 1
        counter.depth += 1
        try:
            return lookup(self, qn)
        finally:
            counter.depth -= 1

    return counted


def _java_parser() -> Parser:
    return Parser(Language(tree_sitter_java.language()))


def _node_count(source: str) -> int:
    stack = [_java_parser().parse(source.encode()).root_node]
    count = 0
    while stack:
        node = stack.pop()
        count += 1
        stack.extend(node.children)
    return count


_JDK_IMPORTS = [
    f"import java.util.{name};"
    for name in ("List", "Map", "Set", "ArrayList", "HashMap", "Optional")
]


def _hierarchy(depth: int) -> dict[str, str]:
    """A chain C0 <- C1 <- ... with one class per file and package.

    Every class calls a method only the root declares, methods nothing
    declares (each climbs the whole chain, then reads the class's
    interfaces) and types a local from an undeclared helper (the
    current-class lookup). The calls climb further the deeper the class, so
    lookups grow with the square of `depth`. Each file opens with an import
    block, as real ones do, which a walk crosses before reaching the class.
    """
    files: dict[str, str] = {}
    for level in range(depth):
        lines = [f"package com.example.l{level};", "", *_JDK_IMPORTS]
        if level:
            lines += [f"import com.example.l{level - 1}.C{level - 1};", ""]
            lines.append(f"public class C{level} extends C{level - 1} {{")
        else:
            lines += ["", "public class C0 {", "    public void base() {}"]
        lines += [
            f"    private int count{level};",
            f"    public int own{level}(int x) {{",
            f"        int y = x + {level};",
            "        for (int i = 0; i < y; i++) { y += i * 2; }",
            "        return y;",
            "    }",
            f"    public void use{level}() {{",
            "        base();",
            "        missing();",
            "        absent();",
            f"        var made = helper{level}();",
            "    }",
            "}",
        ]
        files[f"src/main/java/com/example/l{level}/C{level}.java"] = "\n".join(lines)
    return files


def _materialise(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _updater(
    store: _StatefulIngestor, root: Path, state: Path | None = None
) -> GraphUpdater:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.JAVA not in parsers:
        pytest.skip("java parser not available")
    return GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
        state_dir=state,
    )


@dataclass
class _HierarchyRun:
    counter: _WalkCounter
    files: int
    project_nodes: int


def _index_hierarchy(
    tmp_path: Path, depth: int, monkeypatch: pytest.MonkeyPatch
) -> _HierarchyRun:
    files = _hierarchy(depth)
    root = tmp_path / f"chain{depth}"
    _materialise(root, files)
    with monkeypatch.context() as patch:
        counter = _count_hierarchy_walks(patch)
        _updater(_StatefulIngestor(), root, tmp_path / f"state{depth}").run()
    project_nodes = sum(_node_count(text) for text in files.values())
    return _HierarchyRun(counter, len(files), project_nodes)


class TestEachFileWalkedOncePerPass:
    def test_walks_stay_within_one_per_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = _index_hierarchy(tmp_path, 12, monkeypatch)
        # Many lookups per file, so a per-lookup walk cannot pass by accident.
        assert run.counter.lookups > 5 * run.files, run.counter
        assert run.counter.file_walks <= run.files, run.counter
        assert run.counter.visits <= 2 * run.project_nodes, (
            run.counter,
            run.project_nodes,
        )

    def test_walk_cost_grows_with_files_not_lookups(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        small = _index_hierarchy(tmp_path, 6, monkeypatch)
        large = _index_hierarchy(tmp_path, 12, monkeypatch)
        lookup_growth = large.counter.lookups / small.counter.lookups
        visit_growth = large.counter.visits / small.counter.visits
        node_growth = large.project_nodes / small.project_nodes
        # Twice the files: lookups grow quadratically, the walking linearly.
        assert lookup_growth > 3, (small.counter, large.counter)
        assert visit_growth <= node_growth * 1.25, (
            small.counter,
            large.counter,
            node_growth,
        )


# --- the reference walkers the index replaced -------------------------------


def _legacy_type_name(parent_node: Node) -> str | None:
    for child in parent_node.children:
        if child.type == cs.TS_GENERIC_TYPE:
            for subchild in child.children:
                if subchild.type == cs.TS_TYPE_IDENTIFIER:
                    return safe_decode_text(subchild)
        elif child.type == cs.TS_TYPE_IDENTIFIER:
            return safe_decode_text(child)
    return None


def _legacy_find_superclass(
    engine: JavaTypeInferenceEngine, node: Node, target: str, module_qn: str
) -> str | None:
    if node.type == cs.TS_CLASS_DECLARATION:
        if (name_node := node.child_by_field_name(cs.FIELD_NAME)) and safe_decode_text(
            name_node
        ) == target:
            if superclass_node := node.child_by_field_name(cs.FIELD_SUPERCLASS):
                if superclass_name := _legacy_type_name(superclass_node):
                    return engine._resolve_java_type_name(superclass_name, module_qn)
    for child in node.children:
        if result := _legacy_find_superclass(engine, child, target, module_qn):
            return result
    return None


def _legacy_interface_names(
    engine: JavaTypeInferenceEngine, node: Node, names: list[str], module_qn: str
) -> None:
    for child in node.children:
        if child.type == cs.TS_TYPE_IDENTIFIER:
            if interface_name := safe_decode_text(child):
                names.append(engine._resolve_java_type_name(interface_name, module_qn))
        elif child.children:
            _legacy_interface_names(engine, child, names, module_qn)


def _legacy_find_interfaces(
    engine: JavaTypeInferenceEngine, node: Node, target: str, module_qn: str
) -> list[str]:
    if node.type == cs.TS_CLASS_DECLARATION:
        if (name_node := node.child_by_field_name(cs.FIELD_NAME)) and safe_decode_text(
            name_node
        ) == target:
            if interfaces_node := node.child_by_field_name(cs.FIELD_INTERFACES):
                names: list[str] = []
                _legacy_interface_names(engine, interfaces_node, names, module_qn)
                return names
    for child in node.children:
        if result := _legacy_find_interfaces(engine, child, target, module_qn):
            return result
    return []


def _legacy_type_names(node: Node, names: list[str]) -> None:
    if node.type in (
        cs.TS_CLASS_DECLARATION,
        cs.TS_INTERFACE_DECLARATION,
        cs.TS_ENUM_DECLARATION,
    ):
        if (name_node := node.child_by_field_name(cs.FIELD_NAME)) and (
            name := safe_decode_text(name_node)
        ):
            names.append(name)
    for child in node.children:
        _legacy_type_names(child, names)


def _legacy_superclass_name(self: JavaTypeInferenceEngine, class_qn: str) -> str | None:
    ctx = get_class_context_from_qn(
        class_qn, self.module_qn_to_file_path, self.ast_cache
    )
    if not ctx:
        return None
    return _legacy_find_superclass(
        self, ctx.root_node, ctx.target_class_name, ctx.module_qn
    )


def _legacy_implemented_interfaces(
    self: JavaTypeInferenceEngine, class_qn: str
) -> list[str]:
    parts = class_qn.split(cs.SEPARATOR_DOT)
    if len(parts) < 2:
        return []
    module_qn = cs.SEPARATOR_DOT.join(parts[:-1])
    file_path = self.module_qn_to_file_path.get(module_qn)
    if file_path is None or not (entry := self.ast_cache.load(file_path)):
        return []
    return _legacy_find_interfaces(self, entry[0], parts[-1], module_qn)


def _legacy_current_class_name(
    self: JavaTypeInferenceEngine, module_qn: str
) -> str | None:
    root_node = get_root_node_from_module_qn(
        module_qn, self.module_qn_to_file_path, self.ast_cache
    )
    if not root_node:
        return None
    names: list[str] = []
    _legacy_type_names(root_node, names)
    return f"{module_qn}{cs.SEPARATOR_DOT}{names[0]}" if names else None


def _use_legacy_walkers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        JavaTypeInferenceEngine, "_get_superclass_name", _legacy_superclass_name
    )
    monkeypatch.setattr(
        JavaTypeInferenceEngine,
        "_get_implemented_interfaces",
        _legacy_implemented_interfaces,
    )
    monkeypatch.setattr(
        JavaTypeInferenceEngine, "_get_current_class_name", _legacy_current_class_name
    )


# --- the same graph ---------------------------------------------------------

# Inheritance two levels deep, interfaces on a base, overrides, single and
# on-demand static imports, nested and local classes, a generic and a
# scoped superclass, an enum and a record implementing interfaces, a nested
# class sharing a top-level class's name, and locals typed from calls the
# current-class lookup answers.
_PROJECT: dict[str, str] = {
    "src/main/java/com/acme/core/Shape.java": (
        "package com.acme.core;\n\n"
        "public interface Shape {\n"
        "    double area();\n"
        "    String name();\n"
        "    default String kind() { return name(); }\n"
        "}\n"
    ),
    "src/main/java/com/acme/core/Named.java": (
        "package com.acme.core;\n\n"
        "public interface Named {\n"
        "    String label();\n"
        "    default String shout() { return label(); }\n"
        "}\n"
    ),
    "src/main/java/com/acme/core/AbstractShape.java": (
        "package com.acme.core;\n\n"
        "public abstract class AbstractShape implements Shape, Named {\n"
        '    public String name() { return "shape"; }\n'
        "    public String label() { return name(); }\n"
        "    protected double scale(double v) { return v * 2; }\n"
        "    public static double unit() { return 1.0; }\n"
        "    public static class Helper extends Base<String> {\n"
        "        public int help() { return assist(); }\n"
        "    }\n"
        "}\n"
    ),
    "src/main/java/com/acme/core/Base.java": (
        "package com.acme.core;\n\n"
        "public class Base<T> {\n"
        "    public int assist() { return 1; }\n"
        "    public T held() { return null; }\n"
        "}\n"
    ),
    "src/main/java/com/acme/util/MathUtil.java": (
        "package com.acme.util;\n\n"
        "public final class MathUtil {\n"
        "    public static final double PI_ISH = 3.14;\n"
        "    public static double square(double v) { return v * v; }\n"
        "    public static double cube(double v) { return v * square(v); }\n"
        "}\n"
    ),
    "src/main/java/com/acme/geo/Circle.java": (
        "package com.acme.geo;\n\n"
        "import com.acme.core.AbstractShape;\n"
        "import com.acme.core.Named;\n"
        "import static com.acme.util.MathUtil.square;\n"
        "import static com.acme.util.MathUtil.*;\n\n"
        "public class Circle extends AbstractShape implements Named {\n"
        "    private final double r;\n"
        "    public Circle(double r) { this.r = r; }\n"
        "    @Override public double area() { return scale(square(r)) * PI_ISH; }\n"
        '    @Override public String name() { return "circle"; }\n'
        "    public double volume() { return cube(r) * unit(); }\n"
        "    public String describe() {\n"
        "        var made = make();\n"
        "        return label() + kind() + shout() + made.name();\n"
        "    }\n"
        "    static Circle make() { return new Circle(1); }\n"
        "    public static class Builder extends AbstractShape.Helper {\n"
        "        public Circle build() { help(); held(); return new Circle(assist()); }\n"
        "    }\n"
        "    class Circle extends Base<Integer> {\n"
        "        int inner() { return assist(); }\n"
        "    }\n"
        "}\n"
    ),
    "src/main/java/com/acme/geo/Square.java": (
        "package com.acme.geo;\n\n"
        "import com.acme.core.Shape;\n\n"
        "public class Square extends Circle {\n"
        "    public Square() { super(2); }\n"
        "    @Override public double area() { return super.area() + scale(1); }\n"
        "    public String tag() {\n"
        "        Shape s = new Square();\n"
        "        Square q = new Square();\n"
        "        class Local extends Circle {\n"
        "            Local() { super(3); }\n"
        "            String local() { return describe(); }\n"
        "        }\n"
        "        var helper = fresh();\n"
        "        return s.name() + q.label() + q.volume() + helper.tag();\n"
        "    }\n"
        "    static Square fresh() { return new Square(); }\n"
        "}\n"
    ),
    "src/main/java/com/acme/geo/Palette.java": (
        "package com.acme.geo;\n\n"
        "import com.acme.core.Named;\n\n"
        "public enum Palette implements Named {\n"
        "    RED, GREEN;\n"
        "    public String label() { return name(); }\n"
        "    public String loud() { return shout(); }\n"
        "}\n"
    ),
    "src/main/java/com/acme/geo/Point.java": (
        "package com.acme.geo;\n\n"
        "import com.acme.core.Named;\n\n"
        "public record Point(int x, int y) implements Named {\n"
        '    public String label() { return "p"; }\n'
        "    public String twice() { return shout() + label(); }\n"
        "}\n"
    ),
    "src/main/java/com/acme/app/Main.java": (
        "package com.acme.app;\n\n"
        "import com.acme.core.Shape;\n"
        "import com.acme.geo.Circle;\n"
        "import com.acme.geo.Square;\n"
        "import com.acme.geo.Palette;\n\n"
        "public class Main {\n"
        "    public static void main(String[] args) {\n"
        "        Square sq = new Square();\n"
        "        Circle c = new Circle(1);\n"
        "        Shape s = c;\n"
        "        sq.label();\n"
        "        sq.describe();\n"
        "        sq.shout();\n"
        "        c.volume();\n"
        "        s.area();\n"
        "        s.kind();\n"
        "        Palette.RED.loud();\n"
        "        new Circle.Builder().build().area();\n"
        "        var other = pick();\n"
        "        other.area();\n"
        "    }\n"
        "    static Shape pick() { return new Square(); }\n"
        "}\n"
    ),
}

_Snapshot = tuple[
    frozenset[tuple[str, str, str]],
    frozenset[tuple[str, str]],
]


def _props(props: dict[str, PropertyValue]) -> str:
    # The sync stamp is the one property two runs never share.
    return repr(
        sorted(
            (key, repr(value))
            for key, value in props.items()
            if key != cs.KEY_LAST_SYNCED_AT
        )
    )


def _full_snapshot(store: _StatefulIngestor, root: Path) -> _Snapshot:
    # Every node and every edge with all their properties. Paths are made
    # relative so two stores over the same tree compare in a readable diff.
    prefix = root.resolve().as_posix() + "/"

    def norm(text: str) -> str:
        return text.replace(prefix, "")

    nodes = frozenset(
        (str(label), norm(str(uid)), norm(_props(props)))
        for (label, uid), props in store.nodes.items()
    )
    edges = frozenset(
        (norm(repr(edge)), norm(_props(store.props_for(edge))))
        for edge in store.keyed_edges
    )
    return nodes, edges


def _index_project(
    tmp_path: Path, files: dict[str, str], name: str
) -> tuple[_StatefulIngestor, Path]:
    root = tmp_path / "proj"
    if not root.exists():
        _materialise(root, files)
    store = _StatefulIngestor()
    _updater(store, root, tmp_path / f"state-{name}").run(force=True)
    return store, root


def _calls(store: _StatefulIngestor) -> set[tuple[str, str]]:
    return {(str(e[1]), str(e[4])) for e in store.edges if e[2] == _CALLS}


class TestGraphUnchanged:
    def test_graph_matches_the_per_lookup_walkers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with monkeypatch.context() as patch:
            _use_legacy_walkers(patch)
            legacy, root = _index_project(tmp_path, _PROJECT, "legacy")
        indexed, _ = _index_project(tmp_path, _PROJECT, "indexed")

        assert _full_snapshot(indexed, root) == _full_snapshot(legacy, root)

    def test_fixture_reaches_through_the_hierarchy(self, tmp_path: Path) -> None:
        # Guards the comparison above against a fixture whose calls never
        # need a superclass or an interface: each edge here does.
        store, _ = _index_project(tmp_path, _PROJECT, "edges")
        calls = _calls(store)
        geo = "proj.src.main.java.com.acme.geo"
        core = "proj.src.main.java.com.acme.core"
        expected = {
            # Inherited from the superclass.
            (
                f"{geo}.Circle.Circle.area()",
                f"{core}.AbstractShape.AbstractShape.scale(double)",
            ),
            (
                f"{geo}.Circle.Circle.describe()",
                f"{core}.AbstractShape.AbstractShape.label()",
            ),
            # A default method of an interface the class implements.
            (f"{geo}.Circle.Circle.describe()", f"{core}.Named.Named.shout()"),
        }
        assert expected <= calls, sorted(calls)


# --- the index answers what the walkers answered ----------------------------

_TRICKY = (
    b"package com.example;\n"
    b"import com.example.other.Remote;\n"
    b"class Plain { void run() {} }\n"
    b"class Twin extends Outer.Base implements Comparable<Twin>, Outer.Marker {\n"
    b"    static class Twin extends Remote implements Runnable {}\n"
    b"}\n"
    b"class Holder {\n"
    b"    void make() {\n"
    b"        class Local extends Remote implements Runnable, Cloneable {}\n"
    b"        Runnable r = new Runnable() { public void run() {} };\n"
    b"    }\n"
    b"    class Plain extends Holder {}\n"
    b"}\n"
    b"enum Mode implements Runnable { A; public void run() {} }\n"
    b"interface Marker extends Runnable {}\n"
    b"record Pair(int a) implements Runnable { public void run() {} }\n"
    b"class Generic<T> extends java.util.ArrayList<T> implements java.io.Serializable {}\n"
    b"class Broken implements {}\n"
    b"class Broken extends Remote implements Cloneable {}\n"
    b"class Dup extends First implements Alpha {}\n"
    b"class Shell { class Dup extends Second implements Beta {} }\n"
    b"class Gap implements { static class Gap implements Runnable {} }\n"
)
# Plain, Twin, Broken and Dup are declared twice; the walk answered from
# the first declaration in pre-order that settled the lookup. The first
# Broken's `implements` names no type, so its interfaces come from the
# second; Gap's recovers as an error node, which leaves its nested twin.
_TRICKY_NAMES = (
    "Plain",
    "Twin",
    "Holder",
    "Local",
    "Mode",
    "Marker",
    "Pair",
    "Generic",
    "Broken",
    "Dup",
    "Shell",
    "Gap",
    "Missing",
)
_MODULE = "proj.com.example.Tricky"


class _TreeCache:
    """The one file `_MODULE` maps to, loaded the way the real cache does."""

    def __init__(self, root: Node) -> None:
        self.root = root
        self.loads = 0

    def load(self, key: Path) -> tuple[Node, cs.SupportedLanguage] | None:
        self.loads += 1
        return self.root, cs.SupportedLanguage.JAVA


def _engine(cache: _TreeCache) -> JavaTypeInferenceEngine:
    return JavaTypeInferenceEngine(
        import_processor=ImportProcessor(repo_path=Path("/repo"), project_name="proj"),
        function_registry=FunctionRegistryTrie(),
        repo_path=Path("/repo"),
        project_name="proj",
        ast_cache=cache,
        queries={},
        module_qn_to_file_path={_MODULE: Path("/repo/com/example/Tricky.java")},
        class_inheritance={},
        simple_name_lookup=defaultdict(set),
    )


class TestIndexAnswersLikeTheWalkers:
    @pytest.mark.parametrize("name", _TRICKY_NAMES)
    def test_superclass_and_interfaces(self, name: str) -> None:
        cache = _TreeCache(_java_parser().parse(_TRICKY).root_node)
        engine = _engine(cache)
        class_qn = f"{_MODULE}.{name}"

        assert engine._get_superclass_name(class_qn) == _legacy_superclass_name(
            engine, class_qn
        )
        assert engine._get_implemented_interfaces(
            class_qn
        ) == _legacy_implemented_interfaces(engine, class_qn)

    def test_current_class_name(self) -> None:
        cache = _TreeCache(_java_parser().parse(_TRICKY).root_node)
        engine = _engine(cache)

        assert engine._get_current_class_name(_MODULE) == _legacy_current_class_name(
            engine, _MODULE
        )

    def test_unmapped_and_short_names_answer_nothing(self) -> None:
        cache = _TreeCache(_java_parser().parse(_TRICKY).root_node)
        engine = _engine(cache)

        assert engine._get_superclass_name("Plain") is None
        assert engine._get_implemented_interfaces("Plain") == []
        assert engine._get_current_class_name("example") is None
        assert engine._get_superclass_name("proj.com.example.Other.Plain") is None
        assert engine._get_implemented_interfaces("proj.Nowhere.Plain") == []
        assert cache.loads == 0

    def test_one_load_serves_every_lookup_in_the_file(self) -> None:
        cache = _TreeCache(_java_parser().parse(_TRICKY).root_node)
        engine = _engine(cache)

        for _ in range(3):
            for name in _TRICKY_NAMES:
                engine._get_superclass_name(f"{_MODULE}.{name}")
                engine._get_implemented_interfaces(f"{_MODULE}.{name}")
            engine._get_current_class_name(_MODULE)

        assert cache.loads == 1

    def test_empty_implements_hides_its_own_body(self) -> None:
        # An `implements` naming no type ended that class's search without
        # looking inside it; the next declaration of the name answered.
        # Tree-sitter wraps a real one in an error node, so it is built here.
        def declared(name: str, implements: list[str], *body: MockNode) -> MockNode:
            names = [create_mock_node(cs.TS_TYPE_IDENTIFIER, n) for n in implements]
            return create_mock_node(
                cs.TS_CLASS_DECLARATION,
                fields={
                    cs.FIELD_NAME: create_mock_node(cs.TS_TYPE_IDENTIFIER, name),
                    cs.FIELD_INTERFACES: create_mock_node(
                        "super_interfaces",
                        children=[create_mock_node("type_list", children=names)],
                    ),
                },
                children=[create_mock_node("class_body", children=list(body))],
            )

        root = create_mock_node(
            "program",
            children=[
                declared("Gap", [], declared("Gap", ["Runnable"])),
                declared("Gap", ["Cloneable"]),
            ],
        )
        engine = _engine(_TreeCache(root))
        class_qn = f"{_MODULE}.Gap"

        assert engine._get_implemented_interfaces(class_qn) == ["Cloneable"]
        assert _legacy_implemented_interfaces(engine, class_qn) == ["Cloneable"]

    def test_memo_holds_no_tree_nodes(self) -> None:
        # A memo holding nodes would pin every tree the bounded AST cache
        # evicts for the rest of the pass.
        cache = _TreeCache(_java_parser().parse(_TRICKY).root_node)
        engine = _engine(cache)
        engine._get_current_class_name(_MODULE)

        (headers,) = engine._class_headers.values()
        values = [
            *headers.type_names,
            *headers.superclasses,
            *headers.superclasses.values(),
            *headers.interfaces,
            *(name for names in headers.interfaces.values() for name in names),
        ]
        assert values
        assert all(type(value) is str for value in values)


class TestMemoLastsOnePass:
    def test_reset_reads_the_reparsed_tree(self) -> None:
        cache = _TreeCache(_java_parser().parse(b"class A extends B {}").root_node)
        engine = _engine(cache)
        class_qn = f"{_MODULE}.A"
        assert engine._get_superclass_name(class_qn) == "B"

        cache.root = (
            _java_parser().parse(b"class A extends C implements D {}").root_node
        )
        # Within a pass the tree cannot change, so the first answer stands...
        assert engine._get_superclass_name(class_qn) == "B"
        assert engine._get_implemented_interfaces(class_qn) == []

        engine.reset_resolution_memos()

        # ...and the next pass reads the tree it was handed.
        assert engine._get_superclass_name(class_qn) == "C"
        assert engine._get_implemented_interfaces(class_qn) == ["D"]
        assert cache.loads == 2


# --- an edited superclass is read again -------------------------------------

_BASE = "src/main/java/app/base/Base.java"
_EDIT_PROJECT: dict[str, str] = {
    "src/main/java/app/base/Root1.java": (
        "package app.base;\n\npublic class Root1 {\n    public void hello() {}\n}\n"
    ),
    "src/main/java/app/base/Root2.java": (
        "package app.base;\n\npublic class Root2 {\n    public void hello() {}\n}\n"
    ),
    "src/main/java/app/base/Greeter.java": (
        "package app.base;\n\n"
        "public interface Greeter {\n    default void ping() {}\n}\n"
    ),
    "src/main/java/app/base/Waver.java": (
        "package app.base;\n\npublic interface Waver {\n    default void ping() {}\n}\n"
    ),
    _BASE: (
        "package app.base;\n\n"
        "public class Base extends Root1 implements Greeter {\n"
        "    public void run() {\n        hello();\n        ping();\n    }\n"
        "}\n"
    ),
    "src/main/java/app/child/Child.java": (
        "package app.child;\n\n"
        "import app.base.Base;\n\n"
        "public class Child extends Base {\n"
        "    public void go() {\n        hello();\n        run();\n    }\n"
        "}\n"
    ),
}
_EDITED_BASE = (
    "package app.base;\n\n"
    "public class Base extends Root2 implements Waver {\n"
    "    public void run() {\n        hello();\n        ping();\n    }\n"
    "}\n"
)
_RUN = "proj.src.main.java.app.base.Base.Base.run()"
_ROOT1_HELLO = "proj.src.main.java.app.base.Root1.Root1.hello()"
_ROOT2_HELLO = "proj.src.main.java.app.base.Root2.Root2.hello()"
_GREETER_PING = "proj.src.main.java.app.base.Greeter.Greeter.ping()"
_WAVER_PING = "proj.src.main.java.app.base.Waver.Waver.ping()"


def _edit_base(root: Path) -> None:
    target = root / _BASE
    target.write_text(_EDITED_BASE, encoding="utf-8")
    force_mtime_after_cache(root, target)


def _clean_index(tmp_path: Path, root: Path) -> _StatefulIngestor:
    clean = _StatefulIngestor()
    _updater(clean, root, tmp_path / "state-clean").run(force=True)
    return clean


class TestEditedSuperclassIsReadAgain:
    def _indexed_then_edited(
        self, tmp_path: Path
    ) -> tuple[_StatefulIngestor, GraphUpdater, Path]:
        root = tmp_path / "proj"
        _materialise(root, _EDIT_PROJECT)
        store = _StatefulIngestor()
        updater = _updater(store, root)
        updater.run()
        before = _calls(store)
        assert (_RUN, _ROOT1_HELLO) in before
        assert (_RUN, _GREETER_PING) in before
        _edit_base(root)
        return store, updater, root

    def test_reused_updater_sync_matches_clean_index(self, tmp_path: Path) -> None:
        # The updater the first sync ran on answered Base's lookups from the
        # old tree; the next sync must not reuse those answers.
        store, updater, root = self._indexed_then_edited(tmp_path)
        updater.run()

        calls = _calls(store)
        assert (_RUN, _ROOT2_HELLO) in calls
        assert (_RUN, _WAVER_PING) in calls
        assert (_RUN, _ROOT1_HELLO) not in calls
        assert (_RUN, _GREETER_PING) not in calls
        clean = _clean_index(tmp_path, root)
        assert _full_snapshot(store, root) == _full_snapshot(clean, root)

    def test_reingest_matches_clean_index(self, tmp_path: Path) -> None:
        # The watcher's path: one file re-parsed on the same updater.
        store, updater, root = self._indexed_then_edited(tmp_path)
        updater.reingest([root / _BASE])

        calls = _calls(store)
        assert (_RUN, _ROOT2_HELLO) in calls
        assert (_RUN, _WAVER_PING) in calls
        assert (_RUN, _ROOT1_HELLO) not in calls
        assert (_RUN, _GREETER_PING) not in calls
        clean = _clean_index(tmp_path, root)
        assert _full_snapshot(store, root) == _full_snapshot(clean, root)
