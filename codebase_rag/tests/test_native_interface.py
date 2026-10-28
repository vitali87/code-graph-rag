"""The Rust interface fingerprint against the Python twin it replaces (issue #3473).

`TreeBridge.interface_fingerprint` must return exactly what
`python_interface_fingerprint` returns. These compare the two on a source
per language written to reach every rule the fingerprint applies, and on
every file of cgr's own Python package; they also pin when the bridge must
step aside.

What the fingerprint keeps and leaves out (issue #2531) is pinned on both
paths by test_interface_fingerprint.py, through the `both_interface_walks`
fixture.
"""

import importlib
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.embedded_graph.extension import rust_extension
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers import native_trees
from codebase_rag.parsers.interface_fingerprint import (
    bridge_rules,
    interface_fingerprint,
    python_interface_fingerprint,
)
from codebase_rag.parsers.py.ast_analyzer import clear_scope_facts

RUST = rust_extension()
FINGERPRINT = importlib.import_module("codebase_rag.parsers.interface_fingerprint")
PARSERS, _ = load_parsers()
PACKAGE_DIR = Path(native_trees.__file__).parents[1]

PY = cs.SupportedLanguage.PYTHON
JAVA = cs.SupportedLanguage.JAVA
JS = cs.SupportedLanguage.JS
TS = cs.SupportedLanguage.TS
TSX = cs.SupportedLanguage.TSX

needs_interface_walk = pytest.mark.skipif(
    RUST is None
    or not hasattr(
        getattr(RUST, cs.EG_RUST_TREE_BRIDGE, None), cs.EG_RUST_INTERFACE_FINGERPRINT
    ),
    reason="cgr_graph_rs with the interface fingerprint is not installed",
)

# Bodies elided and kept for every reason `_elidable_body` has: dunders, a
# called parameter (also through a closure, and not through one that
# shadows it), each whole-body reader, a member or callee resolved through
# a local, the projection's nested definitions, returns of every shape and
# call bindings (keyword, position, member, local or not, a comment between
# arguments, a call in a lambda or a decorator), and a syntax error.
PY_SAMPLE = b"""import os  # comment
from . import helpers

VALUE = helpers.make()

class Widget(Base):
    def __init__(self, size):
        self.size = size

    def area(self) -> int:
        return self.size * 2

    def handle(self, cb):
        return cb()

    def captured(self, cb):
        def inner():
            return cb()
        return 1

    def shadowed(self, cb):
        def inner(cb):
            return cb()
        lam = lambda cb: cb()
        class Hidden:
            def run(self):
                return cb()
        return 2

    def assigned(self):
        def inner():
            cb = make()
            cb()
        for (self.x, y) in pairs():
            pass
        return y

def setup(spec, flag=True, *rest, key: int = 1, **kw):
    label = "x"
    handler = make()
    Spec(handler=helper, retries=3)
    relay(handler, label, # between
          key=cb.run, other=spec.handle)
    run(lambda: register(handler))
    @decorate(label)
    def nested():
        return 1
    if flag:
        return Widget()
    elif rest:
        return spec.attr
    elif kw:
        return [label]
    return

def through_local():
    r = A()
    return r.run

def calls_through_local():
    r = A()
    return r.build()

def hands_member():
    r = A()
    relay(r.run)

def returns_local():
    out = A()
    return out

def returns_self(self):
    return self

def blocked():
    global counter
    counter = 1

def imports_inside():
    import json
    return json.dumps(1)

def generator():
    yield 1

def deletes(obj):
    del obj.attr

def with_member(self):
    with open(path) as self.handle:
        pass

def matches(value, cb):
    def inner():
        match value:
            case [cb]:
                return cb()
    return inner

def caf\xc3\xa9(a):
    return a

def broken(x:
    return x +
"""

JAVA_SAMPLE = b"""package p;

import java.util.List;

public class A extends B implements C {
    private int count = 1;

    public A(int count) {
        this.count = count;
    }

    public int twice(int x) {
        int y = x * 2; // comment
        return y;
    }

    public Runnable make() {
        Runnable r = new Runnable() {
            public void run() { System.out.println(count); }
        };
        return () -> System.out.println(r);
    }

    interface Inner { void go(); }
    enum Kind { ONE, TWO }
    @interface Note { String value(); }
    record Point(int x, int y) {
        Point {
            if (x < 0) throw new IllegalArgumentException();
        }
    }
}
"""

# JS/TS: every whole-body reader (an assignment, a value return, `require`,
# a symbol-keyed member, a dynamic import, `yield`, `with`, an export), a
# kept constructor, a called parameter (captured or shadowed), call bindings
# unwrapped through `.bind`, `.call` and casts, members and callees resolved
# through a local, and every binder a body can shadow a name with.
JS_SAMPLE = b"""const helper = require('./helper');
import { other } from './other';

export function relay(a, b) {
  const label = 'x';
  register(a, label, /* between */ helper.run);
  new Spec(handler, { retries: 3 });
  route(handle.bind(this), notify.call(this));
}

export function returnsValue() {
  return make();
}

export function returnsNothing() {
  return;
}

function assigns(obj) {
  obj.x = 1;
}

function requires() {
  const m = require('m');
}

function symbolKeyed(v) {
  const table = { [kX]: v };
}

async function dynamicImport() {
  const m = await import('./m');
}

function* generator() {
  yield 1;
}

function callsParam(cb) {
  cb();
}

function captures(cb) {
  const inner = () => { cb(); };
}

function shadows(cb) {
  const inner = (cb) => { cb(); };
  function named(cb) { cb(); }
  class Hidden { run() { cb(); } }
}

function throughLocal() {
  const r = make();
  relay(r.run);
}

function binders(x) {
  for (const k in x) { use(k); }
  try { risky(); } catch (err) { report(err); }
  const { a, b: [c] } = x;
  class Local {}
  relay(Local);
}

class Widget extends Base {
  constructor(size) {
    super();
    this.size = size;
  }

  area() {
    return this.size * 2;
  }

  handle(cb) {
    cb();
  }
}

const arrow = (x) => x + 1;
const blockArrow = (x) => {
  log(x);
};
"""

TS_SAMPLE = b"""import type { T } from './t';

export interface Shape { area(): number; }
export type Alias = Shape | null;
export enum Kind { One, Two }
declare function signature(x: number): void;
namespace Space { export const v = 1; }

export abstract class Base<T> implements Shape {
  abstract area(): number;
  protected size: number = 1;

  constructor(private readonly name: string) {}

  handle(cb: () => void, opt?: number): void {
    cb();
  }

  relay(a: T): void {
    register(a as unknown as T, b!, c satisfies T, (d as any).bind(this));
  }
}

export function typed(x: number, y?: string): number {
  const z: number = x * 2;
  log(z);
  return;
}

export const generic = <T,>(value: T): T => value;
"""

TSX_SAMPLE = b"""import React from 'react';

export function Button({ onClick, label }: Props) {
  track(onClick, label);
  return <button onClick={onClick}>{label}</button>;
}

export function Panel(props: Props) {
  const handle = () => props.onClose();
  register(handle);
}

export const List = ({ items }: { items: string[] }) => (
  <ul>{items.map((item) => <li key={item}>{item}</li>)}</ul>
);
"""

SAMPLES = [
    pytest.param(PY, PY_SAMPLE, id="python"),
    pytest.param(JAVA, JAVA_SAMPLE, id="java"),
    pytest.param(JS, JS_SAMPLE, id="javascript"),
    pytest.param(TS, TS_SAMPLE, id="typescript"),
    pytest.param(TSX, TSX_SAMPLE, id="tsx"),
]


def _nodes(root: Node) -> Iterator[Node]:
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        yield node


def _parse(language: cs.SupportedLanguage, source: bytes) -> Node:
    if language not in PARSERS:
        pytest.skip(f"{language} parser not available")
    return PARSERS[language].parse(source).root_node


def _python_twin(root: Node, language: cs.SupportedLanguage) -> str | None:
    # The twin reads the scope walks, which take the bridge when it is on:
    # compare with Python's own all the way down.
    enabled = native_trees.ENABLED
    native_trees.ENABLED = False
    clear_scope_facts()
    try:
        return python_interface_fingerprint(root, language)
    finally:
        native_trees.ENABLED = enabled
        clear_scope_facts()


@pytest.fixture
def bridge() -> native_trees.TreeBridge:
    native_trees._bridge.cache_clear()
    found = native_trees.interface_bridge()
    assert found is not None, "the extension carries the walk but it is off"
    return found


@pytest.fixture(autouse=True)
def _fresh_scope_caches() -> Iterator[None]:
    clear_scope_facts()
    yield
    clear_scope_facts()


@needs_interface_walk
class TestParity:
    @pytest.mark.parametrize(("language", "source"), SAMPLES)
    def test_every_sample(
        self,
        bridge: native_trees.TreeBridge,
        language: cs.SupportedLanguage,
        source: bytes,
    ) -> None:
        root = _parse(language, source)
        assert bridge.interface_fingerprint(root, language) == _python_twin(
            root, language
        )

    @pytest.mark.parametrize(("language", "source"), SAMPLES)
    def test_every_function_of_every_sample(
        self,
        bridge: native_trees.TreeBridge,
        language: cs.SupportedLanguage,
        source: bytes,
    ) -> None:
        # The digest of a subtree reads the same rules from a different
        # root: each def and class on its own, nested ones included.
        definitions = FINGERPRINT._RULES[language].definitions
        compared = 0
        for node in _nodes(_parse(language, source)):
            if node.type in definitions:
                assert bridge.interface_fingerprint(node, language) == (
                    _python_twin(node, language)
                ), node
                compared += 1
        assert compared >= 5

    @pytest.mark.parametrize(("language", "source"), SAMPLES)
    def test_each_sample_elides_and_keeps_bodies(
        self, language: cs.SupportedLanguage, source: bytes
    ) -> None:
        # Guards the samples: a parity test over files whose bodies are all
        # kept, or all elided, would pass for a walk missing half the rules.
        rules = FINGERPRINT._RULES[language]
        functions = [
            node
            for node in _nodes(_parse(language, source))
            if node.type in rules.functions
        ]
        elided = [
            node
            for node in functions
            if FINGERPRINT._elidable_body(node, rules, language) is not None
        ]
        assert elided
        assert len(elided) < len(functions)

    def test_every_file_of_cgr(self, bridge: native_trees.TreeBridge) -> None:
        compared = 0
        for path in sorted(PACKAGE_DIR.rglob("*.py")):
            if "tests" in path.relative_to(PACKAGE_DIR).parts:
                continue
            root = _parse(PY, path.read_bytes())
            assert bridge.interface_fingerprint(root, PY) == _python_twin(root, PY), (
                path
            )
            compared += 1
        assert compared > 300

    def test_the_fingerprint_takes_the_bridge(
        self, bridge: native_trees.TreeBridge, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _parse(PY, PY_SAMPLE)
        expected = _python_twin(root, PY)

        def refuse(_root: Node, _language: cs.SupportedLanguage) -> str | None:
            raise AssertionError("the Python twin ran")

        monkeypatch.setattr(FINGERPRINT, "python_interface_fingerprint", refuse)
        assert interface_fingerprint(root, PY) == expected

    def test_a_language_without_rules_has_none(
        self, bridge: native_trees.TreeBridge
    ) -> None:
        root = _parse(PY, PY_SAMPLE)
        assert interface_fingerprint(root, cs.SupportedLanguage.GO) is None
        assert bridge.interface_fingerprint(root, cs.SupportedLanguage.GO) is None


@needs_interface_walk
class TestFallback:
    def test_a_node_it_cannot_read_runs_the_python_twin(
        self, bridge: native_trees.TreeBridge
    ) -> None:
        node = MagicMock(spec=Node)
        with pytest.raises(native_trees.NotBorrowed):
            bridge.interface_fingerprint(node, PY)

    def test_a_tree_without_its_source_runs_the_python_twin(
        self, bridge: native_trees.TreeBridge
    ) -> None:
        source = b"def f(a):\n    relay(a, key=b.run)\n"
        parser = PARSERS[PY]
        tree = parser.parse(lambda byte, _point: source[byte : byte + 4])
        with pytest.raises(native_trees.NotBorrowed):
            bridge.interface_fingerprint(tree.root_node, PY)
        assert interface_fingerprint(tree.root_node, PY) == _python_twin(
            tree.root_node, PY
        )

    def test_switched_off_it_is_not_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(native_trees, "ENABLED", False)
        assert native_trees.interface_bridge() is None

    def test_a_build_without_the_walk_leaves_it_to_python(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        older = MagicMock(spec=["anchor_hash", "ast_fingerprint", "scope_facts"])
        monkeypatch.setattr(native_trees, "_bridge", lambda: older)
        assert native_trees.scope_bridge() is older
        assert native_trees.interface_bridge() is None

    def test_rules_without_the_interface_turn_it_off(self) -> None:
        bridge_type = getattr(RUST, cs.EG_RUST_TREE_BRIDGE)
        without_interface = native_trees.rules()
        del without_interface["interface"]
        with pytest.raises(KeyError):
            bridge_type(without_interface)

    @pytest.mark.parametrize(
        ("key", "value"),
        [("length_bytes", 8), ("byte_order", "little")],
    )
    def test_a_length_prefix_it_does_not_write_turns_it_off(
        self, key: str, value: int | str
    ) -> None:
        bridge_type = getattr(RUST, cs.EG_RUST_TREE_BRIDGE)
        rules = native_trees.rules()
        interface = dict(rules["interface"])
        interface[key] = value
        rules["interface"] = interface
        with pytest.raises(ValueError):
            bridge_type(rules)

    def test_a_language_branch_it_does_not_know_turns_it_off(self) -> None:
        bridge_type = getattr(RUST, cs.EG_RUST_TREE_BRIDGE)
        rules = native_trees.rules()
        languages = dict(rules["interface"]["languages"])
        languages[PY] = {**languages[PY], "flavor": "cobol"}
        rules["interface"] = {**rules["interface"], "languages": languages}
        with pytest.raises(ValueError):
            bridge_type(rules)

    def test_a_disagreeing_probe_turns_it_off(
        self, bridge: native_trees.TreeBridge, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            FINGERPRINT, "python_interface_fingerprint", lambda _root, _language: ""
        )
        found = native_trees._disagreement(bridge)
        assert found is not None
        assert found[0] == cs.TreeBridgeCheck.INTERFACE_FINGERPRINT

    def test_the_probe_compares_with_python_all_the_way_down(
        self, bridge: native_trees.TreeBridge, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The twin asks the scope walks for the bridge; while the probe runs,
        # the bridge under test must not be what they get.
        seen: list[native_trees.TreeBridge | None] = []
        twin = FINGERPRINT.python_interface_fingerprint

        def recording(root: Node, language: cs.SupportedLanguage) -> str | None:
            seen.append(native_trees.tree_bridge())
            return twin(root, language)

        monkeypatch.setattr(FINGERPRINT, "python_interface_fingerprint", recording)
        assert native_trees._disagreement(bridge) is None
        assert seen == [None]
        assert native_trees.tree_bridge() is not None

    def test_the_rules_build_a_bridge(self) -> None:
        bridge_type = getattr(RUST, cs.EG_RUST_TREE_BRIDGE)
        rules = native_trees.rules()
        assert rules["interface"] == bridge_rules()
        assert bridge_type(rules) is not None
