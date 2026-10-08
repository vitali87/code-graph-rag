"""`new (X)(...)` and `new (X as T)(...)` construct `X`, as `new X(...)` does.

The `new` handler read the constructor sub-expression as written, so a
parenthesised or cast constructor (`new (Internals as any)(def)` in zod's
core) named nothing: no CALLS or INSTANTIATES edge, and `Internals` was
reported as dead code (issue #2874).
"""

from __future__ import annotations

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_LINKS = {cs.RelationshipType.CALLS.value, cs.RelationshipType.INSTANTIATES.value}

_M_TS = """\
function Ctor(this: any, def: any) { this.def = def; }
class Klass { run(): number { return 1; } }
class Decoy { run(): number { return 2; } }
namespace ns { export class Inner {} }
function getCtor(): any { return Ctor; }
function A(this: any) {}
function B(this: any) {}

export function plainNew(def: any) { return new Ctor(def); }
export function parenNew(def: any) { return new (Ctor)(def); }
export function castNew(def: any) { return new (Ctor as any)(def); }
export function satisfiesNew(def: any) { return new (Ctor satisfies any)(def); }
export function nonNullNew(def: any) { return new (Ctor!)(def); }
export function nestedNew(def: any) { return new ((Ctor as any))(def); }
export function memberNew() { return new (ns.Inner as any)(); }
export function typedNew() { const k = new (Klass as any)(); return k.run(); }
export function callNew() { return new (getCtor())(); }
export function pickNew(flag: boolean) { return new (flag ? A : B)(); }
"""

_M_JS = """\
function Legacy(def) { this.def = def; }
export function jsParen(def) { return new (Legacy)(def); }
"""


@pytest.fixture(scope="module")
def edges(tmp_path_factory: pytest.TempPathFactory) -> set[tuple[str, str]]:
    root = tmp_path_factory.mktemp("tsnew") / "tsnew"
    root.mkdir()
    (root / "m.ts").write_text(_M_TS, encoding="utf-8")
    (root / "legacy.js").write_text(_M_JS, encoding="utf-8")
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.TS not in parsers:
        pytest.skip("typescript parser not available")
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="tsnew",
    ).run(force=True)
    return {(str(e[1]), str(e[4])) for e in store.keyed_edges if e[2] in _LINKS}


@pytest.mark.parametrize(
    "caller",
    ["plainNew", "parenNew", "castNew", "satisfiesNew", "nonNullNew", "nestedNew"],
)
def test_every_spelling_constructs_ctor(
    edges: set[tuple[str, str]], caller: str
) -> None:
    assert (f"tsnew.m.{caller}", "tsnew.m.Ctor") in edges, sorted(edges)


def test_a_cast_member_constructor_is_reached(edges: set[tuple[str, str]]) -> None:
    targets = {t for c, t in edges if c == "tsnew.m.memberNew"}
    assert any(t.endswith("Inner") for t in targets), sorted(edges)


def test_a_cast_construction_types_its_receiver(edges: set[tuple[str, str]]) -> None:
    # Two classes define `run`, so only the receiver's type can pick Klass's.
    runs = {t for c, t in edges if c == "tsnew.m.typedNew" and t.endswith(".run")}
    assert runs == {"tsnew.m.Klass.run"}, sorted(edges)


def test_plain_javascript_parentheses_construct_too(
    edges: set[tuple[str, str]],
) -> None:
    assert ("tsnew.legacy.jsParen", "tsnew.legacy.Legacy") in edges, sorted(edges)


def test_a_computed_constructor_is_not_bound_to_a_name(
    edges: set[tuple[str, str]],
) -> None:
    # Negatives: `new (getCtor())()` constructs what getCtor returns, and
    # `new (flag ? A : B)()` one of two; neither is an instantiation of a name
    # written inside the parentheses.
    assert ("tsnew.m.callNew", "tsnew.m.Ctor") not in edges
    picks = {t for c, t in edges if c == "tsnew.m.pickNew"}
    assert not picks & {"tsnew.m.flag"}, picks
