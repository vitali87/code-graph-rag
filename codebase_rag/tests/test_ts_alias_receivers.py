"""TS receivers typed through aliases, `readonly` arrays and element access.

`h: Box` and `h: Box | null` bound `h.size()` exactly, but a type alias
(`type Handler = Box`, `type MaybeBox = Box | undefined`, the generic
`type Maybe<T> = T | null`) and an indexed element (`xs[0].size()` on
`Box[]` / `readonly Box[]`) were bound by name only, `heuristic`, so a
rename refused them; with only the alias imported and another class
defining `size`, there was no edge at all (issue #3276).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.call_resolver import CallResolver
from evals.cgr_graph import _StatefulIngestor

TYPES = (
    "export class Box {\n  size(): number {\n    return 1;\n  }\n}\n\n"
    "export class Bag {\n  size(): number {\n    return 0;\n  }\n}\n\n"
    "export type Handler = Box;\nexport type MaybeBox = Box | undefined;\n"
    "export type Maybe<T> = T | null;\nexport type Pair<A, B> = B;\n"
    "export type Twice = Handler;\n"
)
SHAPES = (
    "export class Base {\n  size(): number {\n    return 0;\n  }\n}\n\n"
    "export class Over extends Base {\n  size(): number {\n    return 1;\n  }\n}\n\n"
    "export class Plain extends Base {}\n\n"
    "export type Overriding = Over;\nexport type Inheriting = Plain;\n"
)
USE = """import { Handler, MaybeBox, Maybe, Pair, Twice, Box } from "./types";

type Local = Box;

export function direct(h: Box): number {
  return h.size();
}

export function viaAlias(h: Handler): number {
  return h.size();
}

export function viaUnionAlias(h: MaybeBox): number {
  return h ? h.size() : 0;
}

export function generic(h: Maybe<Box>): number {
  return h ? h.size() : 0;
}

export function secondArgument(h: Pair<number, Box>): number {
  return h.size();
}

export function chained(h: Twice): number {
  return h.size();
}

export function local(h: Local): number {
  return h.size();
}

export function first(xs: Box[]): number {
  return xs[0].size();
}

export function readonlyFirst(xs: readonly Box[]): number {
  return xs[0].size();
}

export function genericFirst(xs: Array<Box>, i: number): number {
  return xs[i].size();
}
"""


def _index(files: dict[str, str], root: Path) -> tuple[_StatefulIngestor, GraphUpdater]:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )
    updater.run(force=True)
    return store, updater


def _size_calls(files: dict[str, str], root: Path) -> dict[str, set[tuple[str, str]]]:
    return _size_edges(_index(files, root)[0])


def _size_edges(store: _StatefulIngestor) -> dict[str, set[tuple[str, str]]]:
    calls: dict[str, set[tuple[str, str]]] = {}
    for edge, props in store.edge_props.items():
        callee = str(edge[4])
        if edge[2] != cs.RelationshipType.CALLS.value or not callee.endswith(".size"):
            continue
        caller = str(edge[1]).rsplit(cs.SEPARATOR_DOT, 1)[-1]
        calls.setdefault(caller, set()).add(
            (callee.rsplit(cs.SEPARATOR_DOT, 2)[-2], str(props.get(cs.KEY_RESOLUTION)))
        )
    return calls


@pytest.mark.parametrize(
    "caller",
    [
        "direct",
        "viaAlias",
        "viaUnionAlias",
        "generic",
        "secondArgument",
        "chained",
        "local",
        "first",
        "readonlyFirst",
        "genericFirst",
    ],
)
def test_the_receiver_is_typed_through_the_alias_or_element(
    temp_repo: Path, caller: str
) -> None:
    calls = _size_calls({"src/types.ts": TYPES, "src/use.ts": USE}, temp_repo / "proj")

    assert calls.get(caller) == {("Box", cs.EdgeResolution.EXACT)}, calls


@pytest.mark.parametrize("alias", ["Handler", "MaybeBox", "Twice"])
def test_only_the_alias_imported_still_binds_exactly(
    temp_repo: Path, alias: str
) -> None:
    # `Bag.size` also exists and `Box` is never imported: the alias (plain,
    # a union with `undefined`, or an alias of an alias) is the only route
    # to the receiver's class.
    use = (
        f'import {{ {alias} }} from "./types";\n\n'
        f"export function viaAlias(h: {alias}): number {{\n"
        "  return h ? h.size() : 0;\n}\n"
    )
    calls = _size_calls({"src/types.ts": TYPES, "src/use.ts": use}, temp_repo / "proj")

    assert calls.get("viaAlias") == {("Box", cs.EdgeResolution.EXACT)}, calls


def test_an_alias_given_another_class_is_typed_afresh_on_reingest(
    temp_repo: Path,
) -> None:
    # Once `type Handler = Box` became `= Bag`, the reused updater must read
    # the alias afresh, not keep `Box.size`.
    root = temp_repo / "proj"
    use = (
        'import { Handler } from "./types";\n\n'
        "export function viaAlias(h: Handler): number {\n  return h.size();\n}\n"
    )
    store, updater = _index({"src/types.ts": TYPES, "src/use.ts": use}, root)
    (root / "src/types.ts").write_text(
        TYPES.replace("export type Handler = Box;", "export type Handler = Bag;"),
        encoding="utf-8",
    )

    updater.reingest(["src/types.ts"])

    calls = _size_edges(store)
    assert calls.get("viaAlias") == {("Bag", cs.EdgeResolution.EXACT)}, calls


@pytest.mark.parametrize(
    ("alias", "call"),
    [
        pytest.param("Readonly<Box>", "h.size()", id="readonly-utility"),
        pytest.param("Box[]", "h[0].size()", id="array"),
        pytest.param("readonly Box[]", "h[0].size()", id="readonly-array"),
        pytest.param("Array<Box>", "h[0].size()", id="generic-array"),
    ],
)
def test_only_an_alias_of_a_wrapped_class_imported_still_binds_exactly(
    temp_repo: Path, alias: str, call: str
) -> None:
    # Only the alias is imported, so neither `Box.size` nor `Bag.size` is in
    # view by name: the receiver's declaration, read through the wrapper,
    # is what binds the call.
    types = TYPES + f"export type Wrapped = {alias};\n"
    use = (
        'import { Wrapped } from "./types";\n\n'
        f"export function viaAlias(h: Wrapped): number {{\n  return {call};\n}}\n"
    )
    calls = _size_calls({"src/types.ts": types, "src/use.ts": use}, temp_repo / "proj")

    assert calls.get("viaAlias") == {("Box", cs.EdgeResolution.EXACT)}, calls


def test_an_alias_imported_beside_a_class_types_only_its_own_receivers(
    temp_repo: Path,
) -> None:
    # `Shadow` names `Bag`: bringing `Bag` into view for the whole module
    # made `b.size()` on a `Box` ambiguous too, so it lost its exact edge.
    types = TYPES + "export type Shadow = Bag;\n"
    use = (
        'import { Box, Shadow } from "./types";\n\n'
        "export function boxed(b: Box): number {\n  return b.size();\n}\n\n"
        "export function shadowed(s: Shadow): number {\n  return s.size();\n}\n"
    )
    calls = _size_calls({"src/types.ts": types, "src/use.ts": use}, temp_repo / "proj")

    assert calls.get("boxed") == {("Box", cs.EdgeResolution.EXACT)}, calls
    # For `s`, `Bag` is in view beside the imported `Box`: two classes
    # define `size`, which binds nothing, as `import { Box, Bag }` does; in
    # particular not the `Box.size` the alias does not name.
    assert "shadowed" not in calls, calls


def test_an_alias_type_parameter_is_no_class_it_names(temp_repo: Path) -> None:
    # `Box` in `type Wrap<Box> = Box` is the parameter, so `h: Wrap<Bag>` is
    # a `Bag`, not the class `Box`.
    types = TYPES + "export type Wrap<Box> = Box;\n"
    use = (
        'import { Wrap, Bag } from "./types";\n\n'
        "export function wrapped(h: Wrap<Bag>): number {\n  return h.size();\n}\n"
    )
    calls = _size_calls({"src/types.ts": types, "src/use.ts": use}, temp_repo / "proj")

    assert calls.get("wrapped") == {("Bag", cs.EdgeResolution.EXACT)}, calls


def test_a_local_array_type_is_not_the_platform_array(temp_repo: Path) -> None:
    # Negative: a module's own `Array<T>` indexes to what it declares, not to
    # `T`, so `xs[0].size()` is no exact `Box.size`.
    use = (
        'import { Box } from "./types";\n\n'
        "interface Other {\n  size(): number;\n}\n\n"
        "type Array<T> = { [n: number]: Other };\n\n"
        "export function shadowed(xs: Array<Box>): number {\n"
        "  return xs[0].size();\n}\n"
    )
    calls = _size_calls({"src/types.ts": TYPES, "src/use.ts": use}, temp_repo / "proj")

    assert ("Box", cs.EdgeResolution.EXACT) not in calls.get("shadowed", set())


def test_an_element_of_an_aliased_array_is_typed(temp_repo: Path) -> None:
    types = TYPES + "export type Boxes = Box[];\nexport type List<T> = readonly T[];\n"
    use = (
        'import { Boxes, List, Box } from "./types";\n\n'
        "export function plain(xs: Boxes): number {\n  return xs[0].size();\n}\n\n"
        "export function generic(xs: List<Box>): number {\n  return xs[0].size();\n}\n"
    )
    calls = _size_calls({"src/types.ts": types, "src/use.ts": use}, temp_repo / "proj")

    assert calls.get("plain") == {("Box", cs.EdgeResolution.EXACT)}, calls
    assert calls.get("generic") == {("Box", cs.EdgeResolution.EXACT)}, calls


def test_an_alias_of_a_wide_union_types_nothing(temp_repo: Path) -> None:
    # Negative: `Box | Bag` names two classes; no single receiver type.
    types = TYPES + "export type Either = Box | Bag;\n"
    use = (
        'import { Either } from "./types";\n\n'
        "export function either(h: Either): number {\n  return h.size();\n}\n"
    )
    calls = _size_calls({"src/types.ts": types, "src/use.ts": use}, temp_repo / "proj")

    assert (("Box", cs.EdgeResolution.EXACT)) not in calls.get("either", set())
    assert (("Bag", cs.EdgeResolution.EXACT)) not in calls.get("either", set())


def test_a_recursive_alias_terminates(temp_repo: Path) -> None:
    # Negative: `type Loop = Loop2; type Loop2 = Loop` names no class, and
    # resolving it must not recurse forever.
    types = TYPES + "export type Loop = Loop2;\nexport type Loop2 = Loop;\n"
    use = (
        'import { Loop, Box } from "./types";\n\n'
        "export function loop(h: Loop): number {\n  return h.size();\n}\n\n"
        "export function loopFirst(xs: Loop): number {\n  return xs[0].size();\n}\n\n"
        "export function direct(h: Box): number {\n  return h.size();\n}\n"
    )
    calls = _size_calls({"src/types.ts": types, "src/use.ts": use}, temp_repo / "proj")

    assert all(r != cs.EdgeResolution.EXACT for _c, r in calls.get("loop", set()))
    # The rest of the file is still resolved.
    assert calls.get("direct") == {("Box", cs.EdgeResolution.EXACT)}, calls


def test_a_subscript_of_a_non_array_is_not_an_element(temp_repo: Path) -> None:
    # Negative: `m: Box` indexed (`m[0]`) is not a `Box`, so its call is no
    # exact `Box.size`.
    use = (
        'import { Box } from "./types";\n\n'
        "export function notArray(m: Box): number {\n"
        "  return (m as any)[0].size();\n}\n"
    )
    calls = _size_calls({"src/types.ts": TYPES, "src/use.ts": use}, temp_repo / "proj")

    assert ("Box", cs.EdgeResolution.EXACT) not in calls.get("notArray", set())


def test_a_method_on_a_readonly_array_is_no_element_method(temp_repo: Path) -> None:
    # Negative: `xs.size()` on `readonly Box[]` calls the array, not a `Box`,
    # so it takes no edge to `Box.size`, as on a plain `Box[]`.
    use = (
        'import { Box } from "./types";\n\n'
        "export function plain(xs: Box[]): number {\n  return (xs as any).size();\n}\n\n"
        "export function ro(xs: readonly Box[]): number {\n  return xs.size();\n}\n"
    )
    calls = _size_calls({"src/types.ts": TYPES, "src/use.ts": use}, temp_repo / "proj")

    assert "ro" not in calls, calls


@pytest.mark.parametrize(
    "index", ['"length"', "`length`"], ids=["string", "template-string"]
)
def test_a_named_property_of_an_array_is_not_an_element(
    temp_repo: Path, index: str
) -> None:
    # Negative: `xs["length"]` on `Box[]` is the array's own property, a
    # number, not an element, so its call is no exact `Box.size`.
    use = (
        'import { Box } from "./types";\n\n'
        "export function named(xs: Box[]): number {\n"
        f"  return xs[{index}].size();\n}}\n"
    )
    calls = _size_calls({"src/types.ts": TYPES, "src/use.ts": use}, temp_repo / "proj")

    assert ("Box", cs.EdgeResolution.EXACT) not in calls.get("named", set()), calls


def test_an_alias_of_an_overriding_class_takes_no_base_method(
    temp_repo: Path,
) -> None:
    # `Over` overrides `Base.size`. With `Base` imported by name, the pick
    # is `Base.size`, which an `Over` never runs, so it is no exact edge:
    # with `Over` in view beside `Base`, two classes define `size`, which
    # binds nothing, as importing `Over` itself does. `Plain` inherits
    # `Base.size`, which stays exact.
    use = (
        'import { Base, Overriding, Inheriting } from "./shapes";\n\n'
        "export function overriding(h: Overriding): number {\n"
        "  return h.size();\n}\n\n"
        "export function inheriting(h: Inheriting): number {\n"
        "  return h.size();\n}\n"
    )
    direct = (
        'import { Base, Over } from "./shapes";\n\n'
        "export function overDirect(h: Over): number {\n  return h.size();\n}\n"
    )
    calls = _size_calls(
        {"src/shapes.ts": SHAPES, "src/use.ts": use, "src/direct.ts": direct},
        temp_repo / "proj",
    )

    assert "overriding" not in calls, calls
    assert "overDirect" not in calls, calls
    assert calls.get("inheriting") == {("Base", cs.EdgeResolution.EXACT)}, calls


def test_aliases_referring_to_each_other_through_namespaces_terminate(
    temp_repo: Path,
) -> None:
    # Negative: `A = b.B` and `B = a.A`, each through a namespace import,
    # name no class. A dotted name restarted the depth limit, so reading it
    # recursed until the stack ran out and `loop` lost every call.
    files = {
        "src/types.ts": TYPES,
        "src/a.ts": 'import * as b from "./b";\n\nexport type A = b.B;\n',
        "src/b.ts": 'import * as a from "./a";\n\nexport type B = a.A;\n',
        "src/use.ts": (
            'import { A } from "./a";\nimport { Box } from "./types";\n\n'
            "export function loop(h: A): number {\n  return h.size();\n}\n"
        ),
    }
    calls = _size_calls(files, temp_repo / "proj")

    # The alias declares nothing, so the pick by name stays a heuristic.
    assert calls.get("loop") == {("Box", cs.EdgeResolution.HEURISTIC)}, calls


def test_aliases_referring_to_each_other_through_unions_are_read_once_per_hop(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: `W = V | V | V | V` and `V = W | W | W | W` name no class.
    # Every member was read at every level, so this one call read the two
    # aliases 87,381 times; the first member that is not foreign settles a
    # union, so each alias is read once per hop until the depth limit.
    reads: list[str] = []
    read_alias = CallResolver.ts_type_alias

    def counting(resolver: CallResolver, module_qn: str, name: str) -> Node | None:
        reads.append(name)
        return read_alias(resolver, module_qn, name)

    monkeypatch.setattr(CallResolver, "ts_type_alias", counting)
    types = TYPES + "export type W = V | V | V | V;\nexport type V = W | W | W | W;\n"
    use = (
        'import { W, Box } from "./types";\n\n'
        "export function loop(h: W): number {\n  return h.size();\n}\n"
    )
    calls = _size_calls({"src/types.ts": types, "src/use.ts": use}, temp_repo / "proj")

    assert calls.get("loop") == {("Box", cs.EdgeResolution.HEURISTIC)}, calls
    assert len(reads) <= cs.TS_ALIAS_MAX_DEPTH + 1, len(reads)
