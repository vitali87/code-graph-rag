"""Where a Python tree defines each class, top-level function and method.

Type inference reads a callee's definition to learn what it returns, and finds
that definition by name in the callee's file: `_find_class_node`,
`_find_function_ast_node` and `_find_method_ast_node` each ran the class or
function query over the whole file and scanned its captures, once per lookup.
Every call site whose receiver or result needs typing asked again, so a
module used across the repository was searched once per call into it
(docs/reports/INDEXER_RUST_PLAN.md, step 1 of the plan).

`PythonDefinitionIndex` runs each of those queries once per tree, with the
same query, the same source order (`sorted_captures`) and the same name and
nesting rules, and remembers what they found. Every answer is the first match
the per-lookup scan would have returned, quirks included: a class matches at
any depth, and a class's methods are every function anywhere in its body, so
a function nested in a method or a nested class's method is found too.
"""

from collections import OrderedDict

from tree_sitter import Node, Query, QueryCursor

from ... import constants as cs
from ..utils import safe_decode_text, sorted_captures


def definition_name(node: Node) -> str | None:
    # The decoded `name` field of a definition, or None when it has no text.
    name_node = node.child_by_field_name(cs.TS_FIELD_NAME)
    if not name_node or name_node.text is None:
        return None
    return safe_decode_text(name_node)


def _is_top_level(node: Node) -> bool:
    # Nested and method definitions carry longer qns than `mod.func`, so a
    # function under any class or function is not the one a module-level qn
    # names, wherever else (an `if`, a `try`) it sits.
    parent = node.parent
    while parent is not None:
        if parent.type in (cs.TS_PY_CLASS_DEFINITION, cs.TS_PY_FUNCTION_DEFINITION):
            return False
        parent = parent.parent
    return True


def _by_name(scope: Node, query: Query, capture: str) -> dict[str, list[Node]]:
    """Every definition `query` captures under `scope`, by name, in source order."""
    found: dict[str, list[Node]] = {}
    for node in sorted_captures(QueryCursor(query), scope).get(capture, []):
        if isinstance(node, Node) and (name := definition_name(node)) is not None:
            found.setdefault(name, []).append(node)
    return found


class PythonDefinitionIndex:
    """One tree's definitions by name, each part found on its first lookup.

    The queries are the engine's own, passed with each lookup; they never
    change for an engine, so the part the first lookup fills answers every
    later one. Parts a tree is never asked about are never computed.
    """

    __slots__ = ("_classes", "_functions", "_methods", "_root", "_top_level")

    def __init__(self, root: Node) -> None:
        self._root = root
        self._classes: dict[str, list[Node]] | None = None
        self._functions: dict[str, list[Node]] | None = None
        self._top_level: dict[str, Node | None] = {}
        # Per class name: each method name's first definition across every
        # class of that name, in source order.
        self._methods: dict[str, dict[str, Node]] = {}

    def _classes_named(self, name: str, class_query: Query) -> list[Node]:
        if self._classes is None:
            self._classes = _by_name(self._root, class_query, cs.QUERY_CAPTURE_CLASS)
        return self._classes.get(name, [])

    def class_node(self, name: str, class_query: Query) -> Node | None:
        """The first class named `name`, at any depth."""
        classes = self._classes_named(name, class_query)
        return classes[0] if classes else None

    def top_level_function(self, name: str, function_query: Query) -> Node | None:
        """The first function named `name` with no class or function around it."""
        if name not in self._top_level:
            if self._functions is None:
                self._functions = _by_name(
                    self._root, function_query, cs.QUERY_CAPTURE_FUNCTION
                )
            self._top_level[name] = next(
                (node for node in self._functions.get(name, ()) if _is_top_level(node)),
                None,
            )
        return self._top_level[name]

    def method(
        self,
        class_name: str,
        method_name: str,
        class_query: Query,
        function_query: Query,
    ) -> Node | None:
        """The first function named `method_name` in the body of the first
        class named `class_name` that has one."""
        if (methods := self._methods.get(class_name)) is None:
            methods = self._methods[class_name] = {}
            for class_node in self._classes_named(class_name, class_query):
                if not (body := class_node.child_by_field_name(cs.TS_FIELD_BODY)):
                    continue
                found = _by_name(body, function_query, cs.QUERY_CAPTURE_FUNCTION)
                for name, nodes in found.items():
                    methods.setdefault(name, nodes[0])
        return methods.get(method_name)


class PythonDefinitionIndexes:
    """The indexes of the trees looked up last, at most `max_trees` of them.

    Keyed by the root node, which tree-sitter compares by tree as well as by
    position, so a file the AST cache evicted and parsed again is a new tree
    with a new index and never reads the old tree's answers. An index holds
    nodes, and a node keeps its whole tree alive where the AST cache's memory
    cap cannot see it, so the bound is on trees, as for the scope walks
    (`ast_analyzer.scope_facts`). The engine clears it with its other caches
    at the start of every run and reingest (#2927).
    """

    __slots__ = ("_indexes", "_max_trees")

    def __init__(self, max_trees: int = cs.PY_DEFINITION_INDEX_TREES) -> None:
        self._max_trees = max_trees
        self._indexes: OrderedDict[Node, PythonDefinitionIndex] = OrderedDict()

    def __call__(self, root: Node) -> PythonDefinitionIndex:
        if (index := self._indexes.get(root)) is not None:
            self._indexes.move_to_end(root)
            return index
        index = self._indexes[root] = PythonDefinitionIndex(root)
        if len(self._indexes) > self._max_trees:
            self._indexes.popitem(last=False)
        return index

    def __len__(self) -> int:
        return len(self._indexes)

    def clear(self) -> None:
        self._indexes.clear()
