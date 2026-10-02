# TypeScript interface `method_signature`s and `abstract` method declarations
# got no Method node (issue #2524): the function query only captured bodied
# definitions, so `Repo.find` did not exist, a call on a `Repo`-typed receiver
# had nothing to bind to (no CALLS edge at all once a second implementer made
# the sole-implementer fallback ambiguous), and implementations had no
# OVERRIDES target. `abstract class X implements I` also skipped its
# implements clause, so `implementors I` lost every abstract base.
from __future__ import annotations

from pathlib import Path

import pytest

from evals.cgr_graph import _capture
from evals.dead_code import cgr_dead_code, default_dead_code_config

_METHOD = "Method"
_FUNCTION = "Function"

REPO_TS = """\
export interface Repo {
  find(id: string): string;
  save(v: string): void;
}
export class MemRepo implements Repo {
  find(id: string) { return id; }
  save(v: string) {}
}
export class CachedRepo extends MemRepo implements Repo {
  find(id: string) { return "c" + id; }
}
export function use(r: Repo): string { return r.find("x"); }
"""

SHAPE_TS = """\
export interface Shape { area(): number; }
export abstract class Base implements Shape {
  abstract area(): number;
  describe(): string { return "area=" + this.area(); }
}
export class Sq extends Base { area(): number { return 1; } }
"""


class _Graph:
    def __init__(self, root: Path) -> None:
        ingestor = _capture(root, "proj")
        self.nodes = ingestor.nodes
        self.rels = {(str(f), rel, str(t)) for _fl, f, rel, _tl, t in ingestor.rels}

    def labelled(self, label: str) -> set[str]:
        return {str(uid) for (lbl, uid) in self.nodes if lbl == label}

    def edges(self, rel: str) -> set[tuple[str, str]]:
        return {(f, t) for f, r, t in self.rels if r == rel}


def _graph(tmp_path: Path, files: dict[str, str]) -> _Graph:
    root = tmp_path / "proj"
    root.mkdir()
    for name, body in files.items():
        (root / name).write_text(body, encoding="utf-8")
    return _Graph(root)


@pytest.mark.parametrize("ext", [".ts", ".tsx"])
def test_interface_method_signatures_become_method_nodes(
    tmp_path: Path, ext: str
) -> None:
    graph = _graph(tmp_path, {f"i{ext}": REPO_TS})
    methods = graph.labelled(_METHOD)
    assert {"proj.i.Repo.find", "proj.i.Repo.save"} <= methods, sorted(methods)
    defines = graph.edges("DEFINES_METHOD")
    assert ("proj.i.Repo", "proj.i.Repo.find") in defines, sorted(defines)
    assert ("proj.i.Repo", "proj.i.Repo.save") in defines, sorted(defines)


def test_interface_method_node_carries_location(tmp_path: Path) -> None:
    graph = _graph(tmp_path, {"i.ts": REPO_TS})
    props = graph.nodes[(_METHOD, "proj.i.Repo.find")]
    assert props["name"] == "find"
    assert props["start_line"] == 2
    assert props["end_line"] == 2
    assert props["path"] == "i.ts"


@pytest.mark.parametrize("ext", [".ts", ".tsx"])
def test_implementation_overrides_interface_method(tmp_path: Path, ext: str) -> None:
    graph = _graph(tmp_path, {f"i{ext}": REPO_TS})
    overrides = graph.edges("OVERRIDES")
    assert ("proj.i.MemRepo.find", "proj.i.Repo.find") in overrides, sorted(overrides)
    assert ("proj.i.MemRepo.save", "proj.i.Repo.save") in overrides, sorted(overrides)


@pytest.mark.parametrize("ext", [".ts", ".tsx"])
def test_call_on_interface_typed_receiver_binds_interface_method(
    tmp_path: Path, ext: str
) -> None:
    # Two implementers: the sole-implementer fallback cannot pick one, so the
    # call must bind to the interface method it statically targets.
    graph = _graph(tmp_path, {f"i{ext}": REPO_TS})
    calls = graph.edges("CALLS")
    assert ("proj.i.use", "proj.i.Repo.find") in calls, sorted(calls)


def test_sole_implementer_also_gets_the_call(tmp_path: Path) -> None:
    # One implementer: the interface method AND the concrete method, as in Java.
    src = (
        "export interface Repo { find(id: string): string; }\n"
        "export class MemRepo implements Repo { find(id: string) { return id; } }\n"
        "export function use(r: Repo): string { return r.find('x'); }\n"
    )
    graph = _graph(tmp_path, {"i.ts": src})
    calls = graph.edges("CALLS")
    assert ("proj.i.use", "proj.i.Repo.find") in calls, sorted(calls)
    assert ("proj.i.use", "proj.i.MemRepo.find") in calls, sorted(calls)


def test_cross_file_interface_call_binds_interface_method(tmp_path: Path) -> None:
    graph = _graph(
        tmp_path,
        {
            "repo.ts": "export interface Repo { find(id: string): string; }\n",
            "impls.ts": (
                "import { Repo } from './repo';\n"
                "export class A implements Repo { find(id: string) { return id; } }\n"
                "export class B implements Repo { find(id: string) { return id; } }\n"
            ),
            "use.ts": (
                "import { Repo } from './repo';\n"
                "export function use(r: Repo) { return r.find('x'); }\n"
            ),
        },
    )
    assert ("proj.use.use", "proj.repo.Repo.find") in graph.edges("CALLS")
    overrides = graph.edges("OVERRIDES")
    assert ("proj.impls.A.find", "proj.repo.Repo.find") in overrides, sorted(overrides)
    assert ("proj.impls.B.find", "proj.repo.Repo.find") in overrides, sorted(overrides)


def test_generic_and_optional_interface_parameters_bind(tmp_path: Path) -> None:
    src = (
        "export interface Repo<T> { find(id: string): T; }\n"
        "export class A implements Repo<string> { find(id: string) { return id; } }\n"
        "export class B implements Repo<string> { find(id: string) { return id; } }\n"
        "export function gen(r: Repo<string>) { return r.find('x'); }\n"
        "export function opt(r: Repo<string> | undefined) {\n"
        "  if (r) { return r.find('y'); }\n"
        "}\n"
    )
    calls = _graph(tmp_path, {"g.ts": src}).edges("CALLS")
    assert ("proj.g.gen", "proj.g.Repo.find") in calls, sorted(calls)
    assert ("proj.g.opt", "proj.g.Repo.find") in calls, sorted(calls)


def test_interface_extending_interface_overrides_chain(tmp_path: Path) -> None:
    src = (
        "export interface Reader { read(): string; }\n"
        "export interface Stream extends Reader { read(): string; close(): void; }\n"
        "export class File implements Stream { read() { return ''; } close() {} }\n"
    )
    overrides = _graph(tmp_path, {"s.ts": src}).edges("OVERRIDES")
    assert ("proj.s.Stream.read", "proj.s.Reader.read") in overrides, sorted(overrides)
    assert ("proj.s.File.read", "proj.s.Stream.read") in overrides, sorted(overrides)
    assert ("proj.s.File.close", "proj.s.Stream.close") in overrides, sorted(overrides)


@pytest.mark.parametrize("ext", [".ts", ".tsx"])
def test_abstract_method_becomes_method_node(tmp_path: Path, ext: str) -> None:
    graph = _graph(tmp_path, {f"abs{ext}": SHAPE_TS})
    methods = graph.labelled(_METHOD)
    assert {"proj.abs.Base.area", "proj.abs.Shape.area"} <= methods, sorted(methods)
    assert ("proj.abs.Base", "proj.abs.Base.area") in graph.edges("DEFINES_METHOD")
    assert "abstract" in graph.nodes[(_METHOD, "proj.abs.Base.area")]["modifiers"]


@pytest.mark.parametrize("ext", [".ts", ".tsx"])
def test_abstract_class_implements_clause_records_implements(
    tmp_path: Path, ext: str
) -> None:
    graph = _graph(tmp_path, {f"abs{ext}": SHAPE_TS})
    implements = graph.edges("IMPLEMENTS")
    assert ("proj.abs.Base", "proj.abs.Shape") in implements, sorted(implements)


def test_abstract_class_extends_and_implements(tmp_path: Path) -> None:
    src = SHAPE_TS + (
        "export abstract class Mid extends Base implements Shape {\n"
        "  abstract extra(): void;\n"
        "}\n"
    )
    graph = _graph(tmp_path, {"abs.ts": src})
    assert ("proj.abs.Mid", "proj.abs.Base") in graph.edges("INHERITS")
    assert ("proj.abs.Mid", "proj.abs.Shape") in graph.edges("IMPLEMENTS")


def test_abstract_chain_overrides(tmp_path: Path) -> None:
    graph = _graph(tmp_path, {"abs.ts": SHAPE_TS})
    overrides = graph.edges("OVERRIDES")
    assert ("proj.abs.Sq.area", "proj.abs.Base.area") in overrides, sorted(overrides)
    assert ("proj.abs.Base.area", "proj.abs.Shape.area") in overrides, sorted(overrides)


def test_this_call_to_abstract_method_reaches_declaration_and_override(
    tmp_path: Path,
) -> None:
    graph = _graph(tmp_path, {"abs.ts": SHAPE_TS})
    calls = graph.edges("CALLS")
    assert ("proj.abs.Base.describe", "proj.abs.Base.area") in calls, sorted(calls)
    assert ("proj.abs.Base.describe", "proj.abs.Sq.area") in calls, sorted(calls)


# Non-exported definitions, so only reachability (not `export`) keeps anything
# off the report. `main` is rooted as an entry point below.
DEAD_REPO_TS = """\
interface Repo {
  find(id: string): string;
  unused(): void;
}
class MemRepo implements Repo {
  find(id: string) { return id; }
  unused() {}
}
class DiskRepo implements Repo {
  find(id: string) { return "d" + id; }
  unused() {}
}
function use(r: Repo): string { return r.find("x"); }
export function main() { use(new MemRepo()); use(new DiskRepo()); }
"""


def test_implementations_reached_only_through_interface_are_live(
    tmp_path: Path,
) -> None:
    root = tmp_path / "dead"
    root.mkdir()
    (root / "m.ts").write_text(DEAD_REPO_TS, encoding="utf-8")
    dead = cgr_dead_code(root, "proj", default_dead_code_config(False, False))
    for live in ("proj.m.Repo.find", "proj.m.MemRepo.find", "proj.m.DiskRepo.find"):
        assert live not in dead, sorted(dead)


def test_interface_method_nobody_calls_is_a_dead_code_candidate(
    tmp_path: Path,
) -> None:
    # A bodiless declaration is a candidate like any method, matching Java
    # interface and Rust trait methods: no call goes through `Repo.unused`, so
    # it and the implementations it would dispatch to are reported.
    root = tmp_path / "dead"
    root.mkdir()
    (root / "m.ts").write_text(DEAD_REPO_TS, encoding="utf-8")
    dead = cgr_dead_code(root, "proj", default_dead_code_config(False, False))
    assert {
        "proj.m.Repo.unused",
        "proj.m.MemRepo.unused",
        "proj.m.DiskRepo.unused",
    } <= dead, sorted(dead)


def test_abstract_method_called_through_this_is_live(tmp_path: Path) -> None:
    src = (
        "abstract class Base {\n"
        "  abstract area(): number;\n"
        "  abstract never(): number;\n"
        "  describe(): string { return 'a=' + this.area(); }\n"
        "}\n"
        "class Sq extends Base {\n"
        "  area(): number { return 1; }\n"
        "  never(): number { return 2; }\n"
        "}\n"
        "export function main() { const s = new Sq(); return s.describe(); }\n"
    )
    root = tmp_path / "dead"
    root.mkdir()
    (root / "m.ts").write_text(src, encoding="utf-8")
    dead = cgr_dead_code(root, "proj", default_dead_code_config(False, False))
    assert "proj.m.Base.area" not in dead, sorted(dead)
    assert "proj.m.Sq.area" not in dead, sorted(dead)
    assert {"proj.m.Base.never", "proj.m.Sq.never"} <= dead, sorted(dead)


# --- negative tests: neighbouring shapes that must not change ---------------


def test_concrete_class_methods_keep_nodes_and_edges(tmp_path: Path) -> None:
    graph = _graph(tmp_path, {"i.ts": REPO_TS, "abs.ts": SHAPE_TS})
    methods = graph.labelled(_METHOD)
    for qn in (
        "proj.i.MemRepo.find",
        "proj.i.MemRepo.save",
        "proj.i.CachedRepo.find",
        "proj.abs.Base.describe",
        "proj.abs.Sq.area",
    ):
        assert qn in methods, sorted(methods)
    defines = graph.edges("DEFINES_METHOD")
    assert ("proj.i.MemRepo", "proj.i.MemRepo.find") in defines
    assert ("proj.abs.Base", "proj.abs.Base.describe") in defines
    assert ("proj.i.CachedRepo.find", "proj.i.MemRepo.find") in graph.edges("OVERRIDES")
    assert ("proj.i.MemRepo", "proj.i.Repo") in graph.edges("IMPLEMENTS")
    assert ("proj.abs.Sq", "proj.abs.Base") in graph.edges("INHERITS")
    # A concrete receiver still dispatches to the concrete method.
    src = (
        "import { MemRepo } from './i';\n"
        "export function direct(m: MemRepo) { return m.find('x'); }\n"
    )
    root = tmp_path / "proj"
    (root / "direct.ts").write_text(src, encoding="utf-8")
    calls = _Graph(root).edges("CALLS")
    assert ("proj.direct.direct", "proj.i.MemRepo.find") in calls, sorted(calls)
    assert ("proj.direct.direct", "proj.i.Repo.find") not in calls, sorted(calls)


def test_property_signatures_and_type_aliases_get_no_method_node(
    tmp_path: Path,
) -> None:
    src = (
        "export interface Opts {\n"
        "  name: string;\n"
        "  cb: (x: number) => void;\n"
        "  nested: { inner(): void };\n"
        "  readonly [key: string]: unknown;\n"
        "  new (x: string): Opts;\n"
        "  (x: number): void;\n"
        "}\n"
        "export type Fn = (x: number) => void;\n"
        "export type Shape = { area(): number; label: string };\n"
        "export function take(o: { run(): void }): void { o.run(); }\n"
        "export let holder: { tick(): void };\n"
    )
    graph = _graph(tmp_path, {"t.ts": src})
    callables = graph.labelled(_METHOD) | graph.labelled(_FUNCTION)
    for leaf in ("name", "cb", "nested", "inner", "area", "label", "run", "tick", "Fn"):
        assert not any(qn.rsplit(".", 1)[-1] == leaf for qn in callables), sorted(
            callables
        )
    assert "proj.t.take" in callables


def test_class_overload_signatures_do_not_add_nodes(tmp_path: Path) -> None:
    # Class overload signatures denote the one implementation below them, so
    # they must not mint `@line` duplicates that nothing calls (they would be
    # reported dead); the implementation keeps the node.
    src = (
        "export class C {\n"
        "  pick(a: string): string;\n"
        "  pick(a: number): string;\n"
        "  pick(a: unknown): string { return String(a); }\n"
        "}\n"
    )
    graph = _graph(tmp_path, {"o.ts": src})
    picks = {qn for qn in graph.labelled(_METHOD) if ".pick" in qn}
    assert picks == {"proj.o.C.pick"}, sorted(picks)
    assert graph.nodes[(_METHOD, "proj.o.C.pick")]["start_line"] == 4


def test_overloaded_signatures_make_one_member(tmp_path: Path) -> None:
    # Interface and abstract overloads declare one member each: one node at the
    # first signature, no `@line` duplicate nothing could call.
    src = (
        "export interface I {\n"
        "  pick(a: string): string;\n"
        "  pick(a: number): string;\n"
        "}\n"
        "export abstract class A {\n"
        "  abstract pick(a: string): string;\n"
        "  abstract pick(a: number): string;\n"
        "}\n"
    )
    graph = _graph(tmp_path, {"o.ts": src})
    picks = {qn for qn in graph.labelled(_METHOD) if ".pick" in qn}
    assert picks == {"proj.o.I.pick", "proj.o.A.pick"}, sorted(picks)
    assert graph.nodes[(_METHOD, "proj.o.I.pick")]["start_line"] == 2


def test_untyped_receiver_still_binds_the_sole_implementation(
    tmp_path: Path,
) -> None:
    # The name-only member gate bound `r.find()` to the one implementation
    # before the interface had a node; the declaration it implements must not
    # make that ambiguous.
    src = (
        "export interface Repo { find(id: string): string; }\n"
        "export class MemRepo implements Repo { find(id: string) { return id; } }\n"
        "export function use(r) { return r.find('x'); }\n"
    )
    calls = _graph(tmp_path, {"i.ts": src}).edges("CALLS")
    assert ("proj.i.use", "proj.i.MemRepo.find") in calls, sorted(calls)
    assert ("proj.i.use", "proj.i.Repo.find") not in calls, sorted(calls)


def test_untyped_receiver_with_two_implementations_stays_unbound(
    tmp_path: Path,
) -> None:
    src = (
        "export interface Repo { find(id: string): string; }\n"
        "export class A implements Repo { find(id: string) { return id; } }\n"
        "export class B implements Repo { find(id: string) { return id; } }\n"
        "export function use(r) { return r.find('x'); }\n"
    )
    calls = _graph(tmp_path, {"i.ts": src}).edges("CALLS")
    assert not {c for c in calls if c[0] == "proj.i.use"}, sorted(calls)


def test_plain_javascript_class_methods_unchanged(tmp_path: Path) -> None:
    src = (
        "export class MemRepo { find(id) { return id; } }\n"
        "export function use() { const r = new MemRepo(); return r.find('x'); }\n"
    )
    graph = _graph(tmp_path, {"i.js": src})
    assert graph.labelled(_METHOD) == {"proj.i.MemRepo.find"}
    assert ("proj.i.use", "proj.i.MemRepo.find") in graph.edges("CALLS")
