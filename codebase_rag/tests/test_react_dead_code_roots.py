# React class-component lifecycle methods (issue #978): render / componentDidMount
# / componentWillUnmount / constructor / ... are invoked by React at runtime,
# never by an explicit call the graph can see, so they are reachability roots on
# a class that INHERITS a React component base. A same-named method on a plain
# class must NOT be rooted.
from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.types_defs import ResultRow

_MODULE = cs.NodeLabel.MODULE.value
_CLASS = cs.NodeLabel.CLASS.value
_METHOD = cs.NodeLabel.METHOD.value
_DEFINES_METHOD = cs.RelationshipType.DEFINES_METHOD.value
_INHERITS = cs.RelationshipType.INHERITS.value
_CALLS = cs.RelationshipType.CALLS.value

_PATH = "app/App.tsx"


class FakeIngestor:
    def __init__(self, nodes: list[ResultRow], rels: list[ResultRow]) -> None:
        self._nodes = nodes
        self._rels = rels

    def fetch_all(
        self, query: str, params: dict[str, str] | None = None
    ) -> list[ResultRow]:
        if query == cq.CYPHER_DEAD_CODE_NODES:
            return self._nodes
        return self._rels


def _node(label: str, qn: str, name: str) -> ResultRow:
    return {
        "label": label,
        "qualified_name": qn,
        "name": name,
        "path": _PATH,
        "start_line": 1,
        "end_line": 2,
        "decorators": [],
        "is_exported": False,
        "overrides_external": False,
    }


def _rel(
    from_label: str, from_qn: str, rel_type: str, to_label: str, to_qn: str
) -> ResultRow:
    return {
        "from_label": from_label,
        "from_qn": from_qn,
        "rel_type": rel_type,
        "to_label": to_label,
        "to_qn": to_qn,
    }


def _dead(nodes: list[ResultRow], rels: list[ResultRow]) -> set[str]:
    config = default_dead_code_config(include_tests=True, include_classes=False)
    return {
        row["qualified_name"]
        for row in collect_dead_code(FakeIngestor(nodes, rels), "app", config)
    }


def test_react_lifecycle_methods_and_reachable_helpers_are_rooted() -> None:
    # App extends React.Component: componentDidMount is React-invoked (root), and
    # the helper it calls via this.helper() is reachable through it. Only the
    # unreferenced private helper is genuinely dead.
    nodes = [
        _node(_MODULE, "app.App", "App"),
        _node(_CLASS, "app.App.App", "App"),
        _node(_METHOD, "app.App.App.componentDidMount", "componentDidMount"),
        _node(_METHOD, "app.App.App.render", "render"),
        _node(_METHOD, "app.App.App.wire", "wire"),
        _node(_METHOD, "app.App.App.orphan", "orphan"),
    ]
    rels = [
        _rel(_CLASS, "app.App.App", _INHERITS, _CLASS, "react.Component"),
        *[
            _rel(_CLASS, "app.App.App", _DEFINES_METHOD, _METHOD, m)
            for m in (
                "app.App.App.componentDidMount",
                "app.App.App.render",
                "app.App.App.wire",
                "app.App.App.orphan",
            )
        ],
        # componentDidMount calls this.wire() -> wire is reachable.
        _rel(
            _METHOD,
            "app.App.App.componentDidMount",
            _CALLS,
            _METHOD,
            "app.App.App.wire",
        ),
    ]
    dead = _dead(nodes, rels)
    assert "app.App.App.componentDidMount" not in dead
    assert "app.App.App.render" not in dead
    assert "app.App.App.wire" not in dead  # reached through the rooted lifecycle
    assert "app.App.App.orphan" in dead  # never referenced -> genuinely dead


def test_lifecycle_named_method_on_plain_class_stays_dead() -> None:
    # A plain (non-React) class with a `render` method must stay dead.
    nodes = [
        _node(_MODULE, "app.App", "App"),
        _node(_CLASS, "app.App.Plain", "Plain"),
        _node(_METHOD, "app.App.Plain.render", "render"),
    ]
    rels = [
        _rel(_CLASS, "app.App.Plain", _DEFINES_METHOD, _METHOD, "app.App.Plain.render")
    ]
    dead = _dead(nodes, rels)
    assert "app.App.Plain.render" in dead


def test_non_react_component_base_is_not_rooted() -> None:
    # A base whose simple name is `Component` but is NOT React (Ember/Glimmer's
    # `@glimmer/component.Component`) must not make its subclass a React
    # component; a `render` method there stays dead.
    nodes = [
        _node(_MODULE, "app.App", "App"),
        _node(_CLASS, "app.App.Card", "Card"),
        _node(_METHOD, "app.App.Card.render", "render"),
    ]
    rels = [
        _rel(_CLASS, "app.App.Card", _INHERITS, _CLASS, "@glimmer/component.Component"),
        _rel(_CLASS, "app.App.Card", _DEFINES_METHOD, _METHOD, "app.App.Card.render"),
    ]
    dead = _dead(nodes, rels)
    assert "app.App.Card.render" in dead


def test_react_lookalike_namespace_is_not_rooted() -> None:
    # A base in a namespace that merely CONTAINS "react" (preact, notreact) is
    # not React: the namespace must match exactly, so a `render` there stays dead.
    for base in ("preact.Component", "notreact.Component"):
        nodes = [
            _node(_MODULE, "app.App", "App"),
            _node(_CLASS, "app.App.C", "C"),
            _node(_METHOD, "app.App.C.render", "render"),
        ]
        rels = [
            _rel(_CLASS, "app.App.C", _INHERITS, _CLASS, base),
            _rel(_CLASS, "app.App.C", _DEFINES_METHOD, _METHOD, "app.App.C.render"),
        ]
        assert "app.App.C.render" in _dead(nodes, rels), base


def test_transitive_react_base_lifecycle_is_rooted() -> None:
    # App extends a first-party BaseWidget that extends react.Component: App's
    # lifecycle methods are still React-invoked, so they must be rooted.
    nodes = [
        _node(_MODULE, "app.App", "App"),
        _node(_CLASS, "app.App.BaseWidget", "BaseWidget"),
        _node(_CLASS, "app.App.App", "App"),
        _node(_METHOD, "app.App.App.componentDidMount", "componentDidMount"),
    ]
    rels = [
        _rel(_CLASS, "app.App.BaseWidget", _INHERITS, _CLASS, "react.Component"),
        _rel(_CLASS, "app.App.App", _INHERITS, _CLASS, "app.App.BaseWidget"),
        _rel(
            _CLASS,
            "app.App.App",
            _DEFINES_METHOD,
            _METHOD,
            "app.App.App.componentDidMount",
        ),
    ]
    dead = _dead(nodes, rels)
    assert "app.App.App.componentDidMount" not in dead
