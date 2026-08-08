# A JS/TS getter read as a property invokes the getter, so it must record an
# edge to the getter method regardless of the expression position of the read.
# Reads in direct return/assignment position already linked; reads nested in a
# ternary condition, a member chain, an `if` condition, or a binary expression
# did not, so the getter reported as dead. Found dogfooding TypeORM
# (PlainObjectToDatabaseEntityTransformer's `loadMap.mainLoadMapItem` in a
# ternary+chain, and JoinAttribute's `relation` getter read via `this.relation`
# in `if`/chain positions). Issue #984. The edge is emitted only when the read's
# property is a getter ON THE RECEIVER'S OWN CLASS, so unrelated same-named
# symbols are never revived.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "p"


class _Capture:
    def __init__(self) -> None:
        self.rels: list[tuple[PropertyValue, str, PropertyValue]] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        return None

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        self.rels.append((from_spec[2], str(rel_type), to_spec[2]))

    def flush_all(self) -> None:
        return None

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        return []

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        return None


def _edge_to(cap: _Capture, leaf: str, rel: str) -> bool:
    return any(
        r == rel and str(to).rsplit(".", 1)[-1] == leaf for _frm, r, to in cap.rels
    )


_REFERENCES = str(cs.RelationshipType.REFERENCES)
_CALLS = str(cs.RelationshipType.CALLS)


def _run(tmp_path: Path, src: str) -> _Capture:
    (tmp_path / "c.ts").write_text(src)
    parsers, queries = load_parsers()
    cap = _Capture()
    GraphUpdater(
        ingestor=cap,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    return cap


# `this` receiver: the getter is on the SAME class the read sits in.
_THIS_TEMPLATE = """export class T {{
  get thing() {{ return 1; }}
  method() {{ {body} }}
}}
"""

# typed-local receiver: the getter is on the local's class.
_LOCAL_TEMPLATE = """class Box {{ get thing() {{ return 1; }} }}
export class T {{
  method() {{
    const box = new Box();
    {body}
  }}
}}
"""

_POSITIONS = [
    "return {r}.thing + 1;",
    "return {r}.thing ? 1 : 2;",
    "return {r}.thing.entity;",
    "if ({r}.thing) return 1; return 2;",
]


@pytest.mark.parametrize("body", _POSITIONS)
def test_this_getter_read_in_compound_expression_is_referenced(
    tmp_path: Path, body: str
) -> None:
    cap = _run(tmp_path, _THIS_TEMPLATE.format(body=body.format(r="this")))
    assert _edge_to(cap, "thing", _REFERENCES)


@pytest.mark.parametrize("body", _POSITIONS)
def test_typed_local_getter_read_in_compound_expression_is_referenced(
    tmp_path: Path, body: str
) -> None:
    cap = _run(tmp_path, _LOCAL_TEMPLATE.format(body=body.format(r="box")))
    assert _edge_to(cap, "thing", _REFERENCES)


def test_inherited_getter_read_is_referenced(tmp_path: Path) -> None:
    # A getter defined on a base class and read through a subclass instance must
    # link to the base getter (CodeRabbit review of #984).
    src = """class Base { get thing() { return 1; } }
export class Sub extends Base {
  method() { return this.thing ? 1 : 2; }
}
"""
    assert _edge_to(_run(tmp_path, src), "thing", _REFERENCES)


def test_cast_receiver_read_is_referenced(tmp_path: Path) -> None:
    # An `as` cast asserts the receiver's type, so `(box as Base).thing` links to
    # Base's getter through the cast target type (even for an untyped receiver).
    src = """class Base { get thing() { return 1; } }
export class T {
  method(box: unknown) { return (box as Base).thing ? 1 : 0; }
}
"""
    assert _edge_to(_run(tmp_path, src), "thing", _REFERENCES)


def test_non_null_receiver_read_is_referenced(tmp_path: Path) -> None:
    # A non-null-asserted receiver is transparent: `this!.thing` must link.
    src = """class Base { get thing() { return 1; } }
export class T extends Base {
  method() { return this!.thing ? 1 : 0; }
}
"""
    assert _edge_to(_run(tmp_path, src), "thing", _REFERENCES)


def test_static_getter_read_is_referenced(tmp_path: Path) -> None:
    # `static get thing()` carries `static` before `get`; it must still be
    # detected as a getter and referenced through the class name.
    src = """export class Box {
  static get thing() { return 1; }
  static run() { return Box.thing ? 1 : 2; }
}
"""
    assert _edge_to(_run(tmp_path, src), "thing", _REFERENCES)


def test_getter_read_in_inline_arrow_callback_is_referenced(tmp_path: Path) -> None:
    # An arrow callback captures the enclosing `this`, so `this.thing` read
    # inside `a.map(() => ...)` must link (TypeORM reads getters through
    # .map/.find/.forEach constantly). Adversarial review of #984.
    src = """export class T {
  get thing() { return 1; }
  method(a: number[]) { return a.map(() => this.thing + 1); }
}
"""
    assert _edge_to(_run(tmp_path, src), "thing", _REFERENCES)


def test_typed_local_getter_read_in_inline_arrow_is_referenced(tmp_path: Path) -> None:
    src = """class Box { get thing() { return 1; } }
export class T {
  method(a: number[]) {
    const box = new Box();
    return a.map(() => box.thing + 1);
  }
}
"""
    assert _edge_to(_run(tmp_path, src), "thing", _REFERENCES)


def test_augmented_assignment_reads_the_getter(tmp_path: Path) -> None:
    # `this.thing += 1` reads the current value (invokes the getter) before it
    # writes, so it is a genuine read and must link. Adversarial review of #984.
    src = """export class T {
  get thing(): number { return 1; }
  set thing(v: number) {}
  method() { this.thing += 1; }
}
"""
    assert _edge_to(_run(tmp_path, src), "thing", _REFERENCES)


def test_this_in_nested_plain_function_does_not_link(tmp_path: Path) -> None:
    # A nested non-arrow `function` rebinds `this`, so `this.thing` there is NOT
    # the class instance and must not fabricate an edge to the class getter.
    # Adversarial review of #984.
    src = """export class T {
  get thing() { return 1; }
  method() {
    function inner() { return this.thing + 1; }
    return inner;
  }
}
"""
    assert not _edge_to(_run(tmp_path, src), "thing", _REFERENCES)


def test_this_in_object_literal_method_does_not_link(tmp_path: Path) -> None:
    # An object-literal shorthand method rebinds `this` to the object, not the
    # enclosing class, so `this.thing` there must not link. Review of #984.
    src = """export class T {
  get thing() { return 1; }
  method() {
    const o = { foo() { return this.thing + 1; } };
    return o;
  }
}
"""
    assert not _edge_to(_run(tmp_path, src), "thing", _REFERENCES)


@pytest.mark.parametrize(
    "body",
    [
        "this.thing = 1;",
        "[this.thing] = arr;",
        "({ x: this.thing } = o);",
    ],
)
def test_setter_write_target_does_not_link_getter(tmp_path: Path, body: str) -> None:
    # A plain-assignment write target (direct or via destructuring) invokes the
    # SETTER, not the getter, so it must not link. Adversarial review of #984.
    src = f"""export class T {{
  get thing(): number {{ return 1; }}
  set thing(v: number) {{}}
  method(arr: number[], o: any) {{ {body} }}
}}
"""
    assert not _edge_to(_run(tmp_path, src), "thing", _REFERENCES)


def test_plain_field_read_does_not_fabricate_edge(tmp_path: Path) -> None:
    # `field` is a data field, not a getter; reading it must NOT create an edge.
    src = """class Box { field = 1; }
export class T {
  method() {
    const box = new Box();
    return box.field ? box.field.x : 0;
  }
}
"""
    cap = _run(tmp_path, src)
    assert not _edge_to(cap, "field", _REFERENCES)


def test_unrelated_receiver_does_not_revive_getter(tmp_path: Path) -> None:
    # `p.secret` on an untyped parameter must NOT link to a same-named getter on
    # an unrelated class; the receiver type is unknown, so no class owns it
    # (adversarial review of #984 -- the name-global trie must not fire here).
    src = """export class Foo { get secret() { return 1; } }
export function handle(p: any) { return p.secret ? 1 : 2; }
"""
    cap = _run(tmp_path, src)
    assert not _edge_to(cap, "secret", _REFERENCES)
    assert not _edge_to(cap, "secret", _CALLS)


def test_same_named_method_on_other_class_gets_no_spurious_reference(
    tmp_path: Path,
) -> None:
    # A regular method `Other.thing()` shares the name with a getter `Box.thing`;
    # calling `other.thing()` must record only the CALLS edge to Other.thing, not
    # a spurious REFERENCES edge (adversarial review of #984).
    src = """class Box { get thing() { return 1; } }
export class Other {
  thing() { return 2; }
  run() {
    const other = new Other();
    return other.thing();
  }
}
"""
    cap = _run(tmp_path, src)
    # No REFERENCES edge should target a `thing`; only the CALLS edge is correct.
    assert not _edge_to(cap, "thing", _REFERENCES)
    assert _edge_to(cap, "thing", _CALLS)
