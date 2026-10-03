# A TypeScript heritage clause produced an edge only when it named a bare
# identifier (issue #2560). `class TrieRouter<T> implements Router<T>` names a
# `generic_type`, `interface CachedStore<K, V> extends Store<K, V>` likewise,
# and `implements r.Plain` after `import * as r` names a
# `nested_type_identifier`: all three were skipped, so the graph held no
# IMPLEMENTS/INHERITS edge for them and `cgr graph implementors` on a generic
# interface (hono's `Router<T>`, six implementors) answered [].
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _capture, _StatefulIngestor

_PROJECT = "proj"
_IMPLEMENTS = cs.RelationshipType.IMPLEMENTS.value
_INHERITS = cs.RelationshipType.INHERITS.value
_CLASS = cs.NodeLabel.CLASS.value
_INTERFACE = cs.NodeLabel.INTERFACE.value
_EXTERNAL = cs.NodeLabel.EXTERNAL_MODULE.value

ROUTER_TS = """\
export interface Router<T> { add(path: string, handler: T): void }
export interface Plain { run(): void }
export class Base { go(): void {} }
export class GBase<T> { go(): void {} }
"""

TYPE_IMPORT_TS = """\
import type { Router, Plain } from '../router'
export class TypeImportGeneric<T> implements Router<T> { add(p: string, h: T): void {} }
export class TypeImportPlain implements Plain { run(): void {} }
"""

VALUE_IMPORT_TS = """\
import { Router, Plain } from '../router'
export class ValueImportGeneric<T> implements Router<T> { add(p: string, h: T): void {} }
export class ValueImportPlain implements Plain { run(): void {} }
"""

INLINE_TYPE_IMPORT_TS = """\
import { type Router } from '../router'
export class InlineTypeImportGeneric<T> implements Router<T> { add(p: string, h: T): void {} }
"""

NAMESPACE_IMPORT_TS = """\
import * as r from '../router'
export class NamespacedPlain implements r.Plain { run(): void {} }
export class NamespacedGeneric<T> implements r.Router<T> { add(p: string, h: T): void {} }
export class Both<T> implements r.Plain, r.Router<T> { run(): void {} add(p: string, h: T): void {} }
export interface NamespacedChild extends r.Plain { more(): void }
export interface NamespacedGenericChild<T> extends r.Router<T> { more(): void }
export class NsExtends extends r.Base { go(): void {} }
export class NsGenericExtends extends r.GBase<string> { go(): void {} }
"""

BASE_TS = """\
export class Base<T> { describe(): string { return 'base' } }
export class GenericChild extends Base<string> { describe(): string { return 'child' } }
export interface Store<K, V> { get(k: K): V | undefined }
export interface CachedStore<K, V> extends Store<K, V> { clear(): void }
export interface Named { name(): string }
export interface Labeled extends Named { label(): string }
export interface Mixed<K> extends Named, Store<K, string> { mixed(): void }
"""

LOCAL_NAMESPACE_TS = """\
namespace ns {
  export interface Shape { area(): number }
  export interface Box<T> { value(): T }
}
export class Square implements ns.Shape { area(): number { return 1 } }
export class NumberBox implements ns.Box<number> { value(): number { return 1 } }
export interface Solid extends ns.Shape { volume(): number }
"""


def _heritage(root: Path) -> set[tuple[str, str, str, str, str]]:
    prefix = f"{_PROJECT}{cs.SEPARATOR_DOT}"
    return {
        (
            from_label,
            str(from_qn).removeprefix(prefix),
            rel,
            to_label,
            str(to_qn).removeprefix(prefix),
        )
        for from_label, from_qn, rel, to_label, to_qn in _capture(root, _PROJECT).rels
        if rel in (_IMPLEMENTS, _INHERITS)
    }


def _write(root: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


@pytest.fixture(scope="module", params=[".ts", ".tsx"])
def issue_edges(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> set[tuple[str, str, str, str, str]]:
    ext = request.param
    root = _write(
        tmp_path_factory.mktemp("issue2560"),
        {
            f"src/router{ext}": ROUTER_TS,
            f"src/impl/a{ext}": TYPE_IMPORT_TS,
            f"src/impl/b{ext}": VALUE_IMPORT_TS,
            f"src/impl/c{ext}": INLINE_TYPE_IMPORT_TS,
            f"src/impl/d{ext}": NAMESPACE_IMPORT_TS,
            f"src/base{ext}": BASE_TS,
            f"src/shapes{ext}": LOCAL_NAMESPACE_TS,
        },
    )
    return _heritage(root)


@pytest.mark.parametrize(
    "child",
    [
        "src.impl.a.TypeImportGeneric",
        "src.impl.b.ValueImportGeneric",
        "src.impl.c.InlineTypeImportGeneric",
    ],
)
def test_class_implementing_a_generic_interface_records_implements(
    issue_edges: set[tuple[str, str, str, str, str]], child: str
) -> None:
    edge = (_CLASS, child, _IMPLEMENTS, _INTERFACE, "src.router.Router")
    assert edge in issue_edges, sorted(issue_edges)


def test_interface_extending_a_generic_interface_records_inherits(
    issue_edges: set[tuple[str, str, str, str, str]],
) -> None:
    edge = (_INTERFACE, "src.base.CachedStore", _INHERITS, _INTERFACE, "src.base.Store")
    assert edge in issue_edges, sorted(issue_edges)


def test_every_base_of_a_mixed_extends_list_is_kept(
    issue_edges: set[tuple[str, str, str, str, str]],
) -> None:
    mixed = {(to, rel) for _fl, f, rel, _tl, to in issue_edges if f == "src.base.Mixed"}
    assert mixed == {("src.base.Named", _INHERITS), ("src.base.Store", _INHERITS)}


@pytest.mark.parametrize(
    ("child", "interface"),
    [
        ("src.impl.d.NamespacedPlain", "src.router.Plain"),
        ("src.impl.d.NamespacedGeneric", "src.router.Router"),
        ("src.impl.d.Both", "src.router.Plain"),
        ("src.impl.d.Both", "src.router.Router"),
    ],
)
def test_namespace_qualified_interface_records_implements(
    issue_edges: set[tuple[str, str, str, str, str]], child: str, interface: str
) -> None:
    edge = (_CLASS, child, _IMPLEMENTS, _INTERFACE, interface)
    assert edge in issue_edges, sorted(issue_edges)


@pytest.mark.parametrize(
    "child", ["src.impl.d.NamespacedChild", "src.impl.d.NamespacedGenericChild"]
)
def test_interface_extending_a_namespace_qualified_interface_records_inherits(
    issue_edges: set[tuple[str, str, str, str, str]], child: str
) -> None:
    targets = {
        (tl, to)
        for _fl, f, rel, tl, to in issue_edges
        if f == child and rel == _INHERITS
    }
    assert targets == {
        (
            _INTERFACE,
            "src.router.Router" if "Generic" in child else "src.router.Plain",
        )
    }


@pytest.mark.parametrize(
    ("child", "base"),
    [
        ("src.impl.d.NsExtends", "src.router.Base"),
        ("src.impl.d.NsGenericExtends", "src.router.GBase"),
    ],
)
def test_class_extending_through_a_namespace_import_binds_the_first_party_class(
    issue_edges: set[tuple[str, str, str, str, str]], child: str, base: str
) -> None:
    # The same namespace binding the type positions use: `extends r.Base` was
    # externalized as an `r.Base` ExternalModule.
    targets = {
        (tl, to)
        for _fl, f, rel, tl, to in issue_edges
        if f == child and rel == _INHERITS
    }
    assert targets == {(_CLASS, base)}


@pytest.mark.parametrize(
    ("child", "rel", "target"),
    [
        ("src.shapes.Square", _IMPLEMENTS, "src.shapes.ns.Shape"),
        ("src.shapes.NumberBox", _IMPLEMENTS, "src.shapes.ns.Box"),
        ("src.shapes.Solid", _INHERITS, "src.shapes.ns.Shape"),
    ],
)
def test_local_namespace_member_heritage_records_an_edge(
    issue_edges: set[tuple[str, str, str, str, str]],
    child: str,
    rel: str,
    target: str,
) -> None:
    targets = {(r, to) for _fl, f, r, _tl, to in issue_edges if f == child}
    assert targets == {(rel, target)}


# --- negative tests: what must not change ------------------------------------


@pytest.mark.parametrize(
    "edge",
    [
        (
            _CLASS,
            "src.impl.a.TypeImportPlain",
            _IMPLEMENTS,
            _INTERFACE,
            "src.router.Plain",
        ),
        (
            _CLASS,
            "src.impl.b.ValueImportPlain",
            _IMPLEMENTS,
            _INTERFACE,
            "src.router.Plain",
        ),
        (_CLASS, "src.base.GenericChild", _INHERITS, _CLASS, "src.base.Base"),
        (_INTERFACE, "src.base.Labeled", _INHERITS, _INTERFACE, "src.base.Named"),
    ],
)
def test_bare_and_class_extends_heritage_is_unchanged(
    issue_edges: set[tuple[str, str, str, str, str]],
    edge: tuple[str, str, str, str, str],
) -> None:
    assert edge in issue_edges, sorted(issue_edges)


TYPE_ARGUMENT_TS = """\
export interface Named { name(): string }
export interface Store<K, V> { get(k: K): V | undefined }
export interface Holder<T> { held(): T }
export class Keeper implements Holder<Named> { held(): Named { return { name: () => '' } } }
export interface Shelf extends Store<string, Named> { shelve(): void }
export class Deep implements Holder<Store<string, Named>> { held(): Store<string, Named> { return { get: () => undefined } } }
"""


def test_type_arguments_are_not_heritage(tmp_path: Path) -> None:
    # `implements Holder<Named>` implements Holder only: the type argument is
    # a use of Named, not a supertype, at any nesting depth.
    edges = _heritage(_write(tmp_path, {"t.ts": TYPE_ARGUMENT_TS}))
    assert edges == {
        (_CLASS, "t.Keeper", _IMPLEMENTS, _INTERFACE, "t.Holder"),
        (_INTERFACE, "t.Shelf", _INHERITS, _INTERFACE, "t.Store"),
        (_CLASS, "t.Deep", _IMPLEMENTS, _INTERFACE, "t.Holder"),
    }, sorted(edges)


DECOY_TS = """\
export interface Other { run(): void }
"""

NAMESPACE_MISS_TS = """\
import * as r from './router'
export class Miss implements r.Other { run(): void {} }
export interface MissChild extends r.Other { more(): void }
"""


def test_namespace_member_the_module_lacks_does_not_bind_elsewhere(
    tmp_path: Path,
) -> None:
    # `r` is ./router, which declares no `Other`: a same-named interface in
    # another module is not what `r.Other` names, so no first-party edge.
    edges = _heritage(
        _write(
            tmp_path,
            {
                "router.ts": ROUTER_TS,
                "decoy.ts": DECOY_TS,
                "miss.ts": NAMESPACE_MISS_TS,
            },
        )
    )
    assert not {e for e in edges if e[4] == "decoy.Other"}, sorted(edges)
    assert not {
        e for e in edges if e[3] in (_CLASS, _INTERFACE) and e[1].startswith("miss.")
    }


REACT_TSX = """\
import React from 'react'
export interface CardProps extends React.HTMLAttributes<HTMLDivElement> { title: string }
export class Card extends React.Component<CardProps> { render() { return null } }
"""


def test_default_import_heritage_keeps_its_written_external_name(
    tmp_path: Path,
) -> None:
    # A default import binds `React` to `react.default`; nothing first-party
    # lives under it, so the base keeps the written name it was externalized
    # under. Dead-code roots a React class component by that `React.Component`.
    edges = _heritage(_write(tmp_path, {"card.tsx": REACT_TSX}))
    assert (_CLASS, "card.Card", _INHERITS, _EXTERNAL, "React.Component") in edges, (
        sorted(edges)
    )
    assert (
        _INTERFACE,
        "card.CardProps",
        _INHERITS,
        _EXTERNAL,
        "React.HTMLAttributes",
    ) in edges, sorted(edges)


# A namespace import of a barrel (review of #2560): `r` names `index`, which
# only re-exports the declarations, so the member must be followed through the
# re-export to its declaring module before it is judged, or the project's own
# interface is externalized as `r.Router`.
BARREL_INDEX_TS = """\
export { Router, Plain } from './router'
export { Plain as Basic } from './router'
export { Base } from './api'
"""

BARREL_API_TS = """\
export { Base } from './router'
"""

BARREL_USER_TS = """\
import * as r from '../index'
export class Generic<T> implements r.Router<T> { add(p: string, h: T): void {} }
export class Bare implements r.Plain { run(): void {} }
export class Aliased implements r.Basic { run(): void {} }
export interface Child extends r.Plain { more(): void }
export class Chained extends r.Base { go(): void {} }
"""


@pytest.fixture(scope="module")
def barrel_edges(
    tmp_path_factory: pytest.TempPathFactory,
) -> set[tuple[str, str, str, str, str]]:
    return _heritage(
        _write(
            tmp_path_factory.mktemp("issue2560barrel"),
            {
                "src/router.ts": ROUTER_TS,
                "src/api.ts": BARREL_API_TS,
                "src/index.ts": BARREL_INDEX_TS,
                "src/impl/user.ts": BARREL_USER_TS,
            },
        )
    )


@pytest.mark.parametrize(
    ("child", "rel", "target"),
    [
        ("Generic", _IMPLEMENTS, (_INTERFACE, "src.router.Router")),
        ("Bare", _IMPLEMENTS, (_INTERFACE, "src.router.Plain")),
        ("Aliased", _IMPLEMENTS, (_INTERFACE, "src.router.Plain")),
        ("Child", _INHERITS, (_INTERFACE, "src.router.Plain")),
        ("Chained", _INHERITS, (_CLASS, "src.router.Base")),
    ],
)
def test_namespace_member_of_a_barrel_follows_the_re_export(
    barrel_edges: set[tuple[str, str, str, str, str]],
    child: str,
    rel: str,
    target: tuple[str, str],
) -> None:
    targets = {
        (tl, to)
        for _fl, f, r, tl, to in barrel_edges
        if f == f"src.impl.user.{child}" and r == rel
    }
    assert targets == {target}, sorted(barrel_edges)


BARREL_WITHOUT_ROUTER_TS = """\
export { Plain } from './router'
"""

BARREL_MISS_USER_TS = """\
import * as r from './index'
export class Missing<T> implements r.Router<T> { add(p: string, h: T): void {} }
"""


def test_name_the_barrel_does_not_re_export_stays_external(tmp_path: Path) -> None:
    # `router.ts` declares `Router`, but `index` exports only `Plain`, so
    # `r.Router` names nothing first-party and keeps today's written external.
    edges = _heritage(
        _write(
            tmp_path,
            {
                "router.ts": ROUTER_TS,
                "index.ts": BARREL_WITHOUT_ROUTER_TS,
                "user.ts": BARREL_MISS_USER_TS,
            },
        )
    )
    assert {e for e in edges if e[1] == "user.Missing"} == {
        (_CLASS, "user.Missing", _IMPLEMENTS, _EXTERNAL, "r.Router")
    }, sorted(edges)


# `export * from './router'` binds no name of its own (CodeRabbit review of
# #2560): the member is whichever star source declares it, through any number
# of star hops, and only when exactly one source does.
STAR_INDEX_TS = """\
export * from './router'
"""

STAR_USER_TS = """\
import * as r from '../index'
export class StarGeneric<T> implements r.Router<T> { add(p: string, h: T): void {} }
export interface StarChild extends r.Plain { more(): void }
export class StarBase extends r.Base { go(): void {} }
"""


@pytest.mark.parametrize(
    ("index_files", "label"),
    [
        ({"src/index.ts": STAR_INDEX_TS}, "one-hop"),
        (
            {
                "src/index.ts": "export * from './api'\n",
                "src/api.ts": "export * from './router'\n",
            },
            "two-hop",
        ),
    ],
    ids=["one-hop", "two-hop"],
)
def test_namespace_member_of_a_star_barrel_binds_its_declaration(
    tmp_path: Path, index_files: dict[str, str], label: str
) -> None:
    edges = _heritage(
        _write(
            tmp_path,
            {
                "src/router.ts": ROUTER_TS,
                "src/impl/user.ts": STAR_USER_TS,
                **index_files,
            },
        )
    )
    user = {(f, r, tl, to) for _fl, f, r, tl, to in edges if f.startswith("src.impl.")}
    assert user == {
        ("src.impl.user.StarGeneric", _IMPLEMENTS, _INTERFACE, "src.router.Router"),
        ("src.impl.user.StarChild", _INHERITS, _INTERFACE, "src.router.Plain"),
        ("src.impl.user.StarBase", _INHERITS, _CLASS, "src.router.Base"),
    }, (label, sorted(edges))


STAR_MISS_USER_TS = """\
import * as r from './index'
export class Missing<T> implements r.Router<T> { add(p: string, h: T): void {} }
"""


def test_name_no_star_source_declares_stays_external(tmp_path: Path) -> None:
    # `index` star-exports `other`, which has no `Router`; `router.ts` does,
    # but `index` never exports it, so nothing first-party is named.
    edges = _heritage(
        _write(
            tmp_path,
            {
                "router.ts": ROUTER_TS,
                "other.ts": DECOY_TS,
                "index.ts": "export * from './other'\n",
                "user.ts": STAR_MISS_USER_TS,
            },
        )
    )
    assert {e for e in edges if e[1] == "user.Missing"} == {
        (_CLASS, "user.Missing", _IMPLEMENTS, _EXTERNAL, "r.Router")
    }, sorted(edges)


def test_name_two_star_sources_declare_is_not_guessed(tmp_path: Path) -> None:
    # Both star sources declare `Router` (TypeScript exports neither), so no
    # first-party edge is chosen between them.
    edges = _heritage(
        _write(
            tmp_path,
            {
                "a.ts": ROUTER_TS,
                "b.ts": ROUTER_TS,
                "index.ts": "export * from './a'\nexport * from './b'\n",
                "user.ts": STAR_MISS_USER_TS,
            },
        )
    )
    assert {e for e in edges if e[1] == "user.Missing"} == {
        (_CLASS, "user.Missing", _IMPLEMENTS, _EXTERNAL, "r.Router")
    }, sorted(edges)


def test_explicit_re_export_outranks_a_star_source(tmp_path: Path) -> None:
    edges = _heritage(
        _write(
            tmp_path,
            {
                "a.ts": ROUTER_TS,
                "b.ts": ROUTER_TS,
                "index.ts": "export * from './a'\nexport { Router } from './b'\n",
                "user.ts": STAR_MISS_USER_TS,
            },
        )
    )
    assert {e for e in edges if e[1] == "user.Missing"} == {
        (_CLASS, "user.Missing", _IMPLEMENTS, _INTERFACE, "b.Router")
    }, sorted(edges)


# `export *` re-exports only what the source module exports (Greptile review
# of #2560): a private `Router` in one star source is not a second candidate,
# so it must not make the exported one ambiguous, nor stand in for it alone.
PRIVATE_ROUTER_TS = """\
interface Router<T> { add(path: string, handler: T): void }
class Base { go(): void {} }
export interface Other { run(): void }
"""

EXPORTED_ROUTER_TS = """\
export interface Router<T> { add(path: string, handler: T): void }
class Base { go(): void {} }
export { Base }
"""

PRIVATE_STAR_USER_TS = """\
import * as r from './index'
export class Impl<T> implements r.Router<T> { add(p: string, h: T): void {} }
export class Sub extends r.Base { go(): void {} }
"""

TWO_STARS_INDEX_TS = "export * from './a'\nexport * from './b'\n"


def _private_star_files() -> dict[str, str]:
    return {
        "a.ts": PRIVATE_ROUTER_TS,
        "b.ts": EXPORTED_ROUTER_TS,
        "index.ts": TWO_STARS_INDEX_TS,
        "user.ts": PRIVATE_STAR_USER_TS,
    }


def _user_heritage(edges: set[tuple[str, str, str, str, str]]) -> set[tuple[str, ...]]:
    return {(f, r, tl, to) for _fl, f, r, tl, to in edges if f.startswith("user.")}


_EXPORTED_SOURCE_EDGES = {
    ("user.Impl", _IMPLEMENTS, _INTERFACE, "b.Router"),
    ("user.Sub", _INHERITS, _CLASS, "b.Base"),
}


def test_a_private_declaration_in_one_star_source_is_not_exported(
    tmp_path: Path,
) -> None:
    # `a` declares `Router` and `Base` without exporting them; `b` exports
    # both (`export interface`, and `export { Base }` after the declaration).
    edges = _heritage(_write(tmp_path, _private_star_files()))
    assert _user_heritage(edges) == _EXPORTED_SOURCE_EDGES, sorted(edges)


def test_a_private_declaration_stays_private_on_an_incremental_run(
    tmp_path: Path,
) -> None:
    # The barrel and its user change, so `a` and `b` come back from the
    # store; their export flags must come back with them or `a.Router` turns
    # ambiguous. (An unchanged barrel's own import map is not rebuilt on an
    # incremental run, so the barrel is touched too.)
    root = _write(tmp_path, _private_star_files())
    store = _StatefulIngestor()
    parsers, queries = load_parsers()

    def run(force: bool) -> None:
        GraphUpdater(
            ingestor=store,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name=_PROJECT,
        ).run(force=force)

    run(True)
    (root / "index.ts").write_text(f"{TWO_STARS_INDEX_TS}// touched\n")
    (root / "user.ts").write_text(f"{PRIVATE_STAR_USER_TS}// touched\n")
    run(False)
    prefix = f"{_PROJECT}{cs.SEPARATOR_DOT}"
    edges = {
        (fl, str(f).removeprefix(prefix), r, tl, str(t).removeprefix(prefix))
        for fl, f, r, tl, t in store.edges
        if r in (_IMPLEMENTS, _INHERITS)
    }
    assert _user_heritage(edges) == _EXPORTED_SOURCE_EDGES, sorted(edges)


def test_a_star_source_declaring_the_name_privately_exposes_nothing(
    tmp_path: Path,
) -> None:
    edges = _heritage(
        _write(
            tmp_path,
            {
                "a.ts": PRIVATE_ROUTER_TS,
                "index.ts": "export * from './a'\n",
                "user.ts": PRIVATE_STAR_USER_TS,
            },
        )
    )
    assert _user_heritage(edges) == {
        ("user.Impl", _IMPLEMENTS, _EXTERNAL, "r.Router"),
        ("user.Sub", _INHERITS, _EXTERNAL, "r.Base"),
    }, sorted(edges)
