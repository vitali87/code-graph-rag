# TypeScript overload signatures (issue #2457). An overloaded function is ONE
# function: bodiless signatures, then the implementation. Each signature was
# indexed as a definition of its own through the `@line` duplicate marker
# (`parse`, `parse@2`, `parse@3`), so dead code reported one unused function
# three times, every call fanned out to all of them as `overload` (which made
# `rename` refuse without `--allow-heuristic`), and `rename` rewrote one
# declaration and left the others under the old name, which `tsc` rejects
# with TS2389. The signatures are now folded into the implementation the way
# Python's `@overload` stubs are (issue #2590); a set with no implementation
# (a `.d.ts`) keeps its first signature as the one node.
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.editing.rename import RenameRefused, rename
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.js_ts.overloads import (
    implementation_signatures,
    overload_signatures,
)
from codebase_rag.structural_check import run_check
from codebase_rag.structural_delta import has_findings
from codebase_rag.types_defs import PropertyValue
from evals.cgr_graph import _StatefulIngestor
from evals.incremental import run_neutral_edit_scenario

PROJECT = "ovl"
_FUNCTION = cs.NodeLabel.FUNCTION.value
_METHOD = cs.NodeLabel.METHOD.value
_EXACT = cs.EdgeResolution.EXACT.value

# The issue's reproduction.
INTERNAL_TS = """\
function parse(x: string): number;
function parse(x: number): number;
function parse(x: string | number): number {
  return Number(x);
}

function used(): number {
  return 1;
}

export const answer = used();
"""

FMT_TS = """\
export function fmt(x: string): string;
export function fmt(x: number): string;
export function fmt(x: string | number): string {
  return String(x);
}
"""

USE_FMT_TS = """\
import { fmt } from "./fmt";

export function show(): string {
  return fmt(1) + fmt("a");
}
"""

DEFAULT_TS = """\
export default function dflt(x: string): string;
export default function dflt(x: number): string;
export default function dflt(x: string | number): string {
  return String(x);
}
"""

GENERATOR_TS = """\
export function gen(): Generator<number>;
export function* gen(): Generator<number> {
  yield 1;
}
"""

NESTED_TS = """\
export function outer(): number {
  function inner(x: string): number;
  function inner(x: number): number;
  function inner(x: string | number): number {
    return Number(x);
  }
  return inner(1);
}
"""

NAMESPACE_TS = """\
export namespace Codec {
  export function decode(x: string): number;
  export function decode(x: number): number;
  export function decode(x: string | number): number {
    return Number(x);
  }
}
"""

METHOD_TS = """\
export class Box {
  get(k: string): string;
  get(k: number): number;
  get(k: string | number): string | number {
    return k;
  }
}

export function useBox(): string | number {
  return new Box().get("a");
}
"""

CONSTRUCTOR_TS = """\
export class Box {
  constructor(a: string);
  constructor(a: number);
  constructor(a: string | number) {}
}

export function make(): Box {
  return new Box(1);
}
"""

NESTED_IN_METHOD_TS = """\
export class Box {
  run(): number {
    function inner(x: string): number;
    function inner(x: number): number;
    function inner(x: string | number): number {
      return Number(x);
    }
    return inner(1);
  }
}
"""

DECLARE_DTS = """\
declare function ext(x: string): number;
declare function ext(x: number): number;
"""

EXPORT_DECLARE_DTS = """\
export declare function ext(x: string): number;
export declare function ext(x: number): number;
"""

DECLARE_MODULE_DTS = """\
declare module "codec" {
  export function encode(x: string): string;
  export function encode(x: number): string;
}
"""

DECLARE_NAMESPACE_DTS = """\
declare namespace Codec {
  function encode(x: string): string;
  function encode(x: number): string;
}
"""

INTERFACE_TS = """\
export interface Repo {
  find(id: string): string;
  find(id: number): string;
}

export function use(r: Repo): string {
  return r.find("x");
}
"""


def _index(root: Path, files: dict[str, str]) -> tuple[_StatefulIngestor, GraphUpdater]:
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        (root / rel).write_text(text, encoding="utf-8")
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)
    return store, updater


def _spans(
    store: _StatefulIngestor, stem: str
) -> dict[tuple[str, str], tuple[PropertyValue, PropertyValue]]:
    # Every Function/Method node for `stem`, its `@line` variants included.
    return {
        (label, str(qn)): (props.get(cs.KEY_START_LINE), props.get(cs.KEY_END_LINE))
        for (label, qn), props in store.nodes.items()
        if label in (_FUNCTION, _METHOD)
        and (str(qn) == stem or str(qn).startswith(f"{stem}{cs.DUP_QN_MARKER}"))
    }


def _calls_from(store: _StatefulIngestor, caller: str) -> set[tuple[str, str | None]]:
    out: set[tuple[str, str | None]] = set()
    for (_fl, source, rel, _tl, target, *_site), props in store.edge_props.items():
        if str(source) == caller and rel == cs.RelationshipType.CALLS.value:
            resolution = props.get(cs.KEY_RESOLUTION)
            out.add((str(target), None if resolution is None else str(resolution)))
    return out


# --- one node per overloaded function -----------------------------------------


ONE_NODE_CASES = [
    pytest.param("internal.ts", INTERNAL_TS, "internal.parse", _FUNCTION, (3, 5), id="function"),
    pytest.param("internal.tsx", INTERNAL_TS, "internal.parse", _FUNCTION, (3, 5), id="function-tsx"),
    pytest.param("fmt.ts", FMT_TS, "fmt.fmt", _FUNCTION, (3, 5), id="exported"),
    pytest.param("dflt.ts", DEFAULT_TS, "dflt.dflt", _FUNCTION, (3, 5), id="export-default"),
    pytest.param("gen.ts", GENERATOR_TS, "gen.gen", _FUNCTION, (2, 4), id="generator-implementation"),
    pytest.param("nested.ts", NESTED_TS, "nested.outer.inner", _FUNCTION, (4, 6), id="nested-in-function"),
    pytest.param("ns.ts", NAMESPACE_TS, "ns.Codec.decode", _METHOD, (4, 6), id="namespace-member"),
    pytest.param("box.ts", METHOD_TS, "box.Box.get", _METHOD, (4, 6), id="class-method"),
    pytest.param("box.tsx", METHOD_TS, "box.Box.get", _METHOD, (4, 6), id="class-method-tsx"),
    pytest.param("box.ts", CONSTRUCTOR_TS, "box.Box.constructor", _METHOD, (4, 4), id="constructor"),
    pytest.param("box.ts", NESTED_IN_METHOD_TS, "box.Box.run.inner", _FUNCTION, (5, 7), id="nested-in-method"),
    # No implementation: the first signature is the function's one node.
    pytest.param("lib.d.ts", DECLARE_DTS, "lib.ext", _FUNCTION, (1, 1), id="declare-function-dts"),
    pytest.param("lib.d.ts", EXPORT_DECLARE_DTS, "lib.ext", _FUNCTION, (1, 1), id="export-declare-dts"),
    pytest.param("lib.d.ts", DECLARE_MODULE_DTS, "lib.encode", _FUNCTION, (2, 2), id="declare-module-dts"),
    pytest.param("lib.d.ts", DECLARE_NAMESPACE_DTS, "lib.Codec.encode", _METHOD, (2, 2), id="declare-namespace-dts"),
]  # fmt: skip


@pytest.mark.parametrize(("name", "text", "stem", "label", "span"), ONE_NODE_CASES)
def test_an_overloaded_function_is_one_node(
    tmp_path: Path, name: str, text: str, stem: str, label: str, span: tuple[int, int]
) -> None:
    store, _updater = _index(tmp_path / PROJECT, {name: text})
    qn = f"{PROJECT}.{stem}"
    assert _spans(store, qn) == {(label, qn): span}


def test_a_call_binds_exact_to_the_implementation(tmp_path: Path) -> None:
    # Was one `overload` edge per signature: `fmt`, `fmt@2` and `fmt@3`.
    store, _updater = _index(
        tmp_path / PROJECT, {"fmt.ts": FMT_TS, "use.ts": USE_FMT_TS}
    )
    assert _calls_from(store, f"{PROJECT}.use.show") == {(f"{PROJECT}.fmt.fmt", _EXACT)}


@pytest.mark.parametrize(
    ("name", "text", "caller", "callee"),
    [
        pytest.param("nested.ts", NESTED_TS, "nested.outer", "nested.outer.inner", id="nested"),
        pytest.param("box.ts", METHOD_TS, "box.useBox", "box.Box.get", id="class-method"),
        pytest.param("box.ts", CONSTRUCTOR_TS, "box.make", "box.Box.constructor", id="constructor"),
        pytest.param("box.ts", NESTED_IN_METHOD_TS, "box.Box.run", "box.Box.run.inner", id="nested-in-method"),
    ],
)  # fmt: skip
def test_a_local_call_binds_exact_to_the_implementation(
    tmp_path: Path, name: str, text: str, caller: str, callee: str
) -> None:
    store, _updater = _index(tmp_path / PROJECT, {name: text})
    assert _calls_from(store, f"{PROJECT}.{caller}") == {
        (f"{PROJECT}.{callee}", _EXACT)
    }


DOCUMENTED_TS = """\
/** Parses a value. */
export function parse(x: string): number;
/** The numeric form. */
export function parse(x: number): number;
export function parse(x: string | number): number {
  return Number(x);
}

/** Formats a value. */
export function fmt(x: string): string;
export function fmt(x: number): string;
/** The implementation's own. */
export function fmt(x: string | number): string {
  return String(x);
}

export class Box {
  /** Reads one entry. */
  get(k: string): string;
  get(k: number): number;
  get(k: string | number): string | number {
    return k;
  }
}
"""


@pytest.mark.parametrize(
    ("stem", "doc"),
    [
        pytest.param("doc.parse", "Parses a value.", id="first-signature"),
        pytest.param("doc.fmt", "The implementation's own.", id="implementation-own"),
        pytest.param("doc.Box.get", "Reads one entry.", id="class-method"),
    ],
)
def test_the_folded_function_keeps_the_signatures_doc(
    tmp_path: Path, stem: str, doc: str
) -> None:
    # The doc comment of an overload set sits on its signatures, the part a
    # caller sees; folding them must not drop it.
    store, _updater = _index(tmp_path / PROJECT, {"doc.ts": DOCUMENTED_TS})
    qn = f"{PROJECT}.{stem}"
    nodes = {str(uid): props for (_label, uid), props in store.nodes.items()}
    assert nodes[qn].get(cs.KEY_DOCSTRING) == doc


def test_an_unused_overloaded_function_is_one_dead_candidate(tmp_path: Path) -> None:
    store, _updater = _index(tmp_path / PROJECT, {"internal.ts": INTERNAL_TS})
    config = default_dead_code_config(include_tests=False, include_classes=False)
    dead = {
        str(row[cs.KEY_QUALIFIED_NAME]): row.get(cs.KEY_START_LINE)
        for row in collect_dead_code(store, PROJECT, config)
    }
    assert dead == {f"{PROJECT}.internal.parse": 3}, dead


# --- rename rewrites every signature with the implementation --------------------


def _renamed(
    tmp_path: Path, files: dict[str, str], qn: str, new_name: str
) -> tuple[Path, _StatefulIngestor]:
    root = tmp_path / PROJECT
    store, updater = _index(root, files)
    report = rename(
        root, store.fetch_all, PROJECT, qn, new_name, reingest=updater.reingest
    )
    assert report.applied, report.message
    assert report.ambiguous == ()
    assert report.verdict is not None, report.message
    assert report.verdict.ok, report.verdict
    return root, store


def test_rename_rewrites_the_signatures_and_the_implementation(tmp_path: Path) -> None:
    root, store = _renamed(
        tmp_path,
        {"internal.ts": INTERNAL_TS},
        f"{PROJECT}.internal.parse",
        "parseValue",
    )
    text = (root / "internal.ts").read_text(encoding="utf-8")
    assert text == INTERNAL_TS.replace("function parse(", "function parseValue(")
    assert _spans(store, f"{PROJECT}.internal.parseValue") == {
        (_FUNCTION, f"{PROJECT}.internal.parseValue"): (3, 5)
    }
    assert _spans(store, f"{PROJECT}.internal.parse") == {}


def test_rename_of_an_exported_overload_needs_no_heuristic_opt_in(
    tmp_path: Path,
) -> None:
    # The calls fanned out as `overload`, so this refused without
    # `--allow-heuristic`; the import and both calls move with the signatures.
    root, _store = _renamed(
        tmp_path,
        {"fmt.ts": FMT_TS, "use.ts": USE_FMT_TS},
        f"{PROJECT}.fmt.fmt",
        "format",
    )
    assert (root / "fmt.ts").read_text(encoding="utf-8") == FMT_TS.replace(
        "function fmt(", "function format("
    )
    # The module path keeps its name: only the symbol was renamed.
    assert (root / "use.ts").read_text(encoding="utf-8") == USE_FMT_TS.replace(
        "{ fmt }", "{ format }"
    ).replace("fmt(", "format(")


@pytest.mark.parametrize(
    ("name", "text", "stem", "old", "new"),
    [
        pytest.param("box.ts", METHOD_TS, "box.Box.get", "get(", "fetch(", id="class-method"),
        pytest.param("ns.ts", NAMESPACE_TS, "ns.Codec.decode", "decode(", "parse(", id="namespace-member"),
        pytest.param("nested.ts", NESTED_TS, "nested.outer.inner", "inner(", "step(", id="nested"),
        pytest.param("repo.ts", INTERFACE_TS, "repo.Repo.find", "find(", "lookup(", id="interface-member"),
        pytest.param("lib.d.ts", DECLARE_DTS, "lib.ext", "ext(", "external(", id="declare-function-dts"),
    ],
)  # fmt: skip
def test_rename_rewrites_every_signature_of_the_set(
    tmp_path: Path, name: str, text: str, stem: str, old: str, new: str
) -> None:
    root, _store = _renamed(
        tmp_path, {name: text}, f"{PROJECT}.{stem}", new.rstrip("(")
    )
    assert (root / name).read_text(encoding="utf-8") == text.replace(old, new)


def _tsc() -> str | None:
    return shutil.which("tsc")


@pytest.mark.skipif(_tsc() is None, reason="tsc is unavailable")
def test_the_renamed_file_type_checks(tmp_path: Path) -> None:
    # The issue's failure: TS2389, "Function implementation name must be 'parse'".
    root, _store = _renamed(
        tmp_path,
        {"internal.ts": INTERNAL_TS},
        f"{PROJECT}.internal.parse",
        "parseValue",
    )
    _assert_type_checks(root / "internal.ts")


def _assert_type_checks(path: Path) -> None:
    # Only where a `tsc` is installed; the caller's text assertions stand
    # either way.
    if (tsc := _tsc()) is None:
        return
    checked = subprocess.run(
        [tsc, "--noEmit", "--strict", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=120,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


# --- static and instance members are separate functions -------------------------

# Bot review on PR #3357: an abstract instance `f` beside an overloaded static
# `f`. They live in different declaration spaces, so renaming the abstract
# member must leave the static signatures with their implementation; renaming
# them too is TS2389 on the static implementation.
ABSTRACT_THEN_STATIC_TS = """\
export abstract class A {
  abstract f(): void;
  static f(a: string): string;
  static f(a: number): string;
  static f(a: string | number): string {
    return String(a);
  }
}
"""

# The same members the other way round: the abstract member is the `@line`
# variant, so the rename passes its contract and only the file shows the harm.
STATIC_THEN_ABSTRACT_TS = """\
export abstract class A {
  static f(a: string): string;
  static f(a: number): string;
  static f(a: string | number): string {
    return String(a);
  }
  abstract f(): void;
}
"""

STATIC_OVERLOADS_TS = """\
export class Codec {
  static decode(x: string): number;
  static decode(x: number): number;
  static decode(x: string | number): number {
    return Number(x);
  }
}

export function use(): number {
  return Codec.decode(1);
}
"""

STATIC_BESIDE_INSTANCE_OVERLOADS_TS = """\
export class Box {
  static get(): Box {
    return new Box();
  }
  get(k: string): string;
  get(k: number): number;
  get(k: string | number): string | number {
    return k;
  }
}
"""

BOTH_OVERLOADED_TS = """\
export class Pair {
  /** Builds a pair. */
  static make(a: string): Pair;
  static make(a: number): Pair;
  static make(a: string | number): Pair {
    return new Pair();
  }
  make(a: string): string;
  make(a: number): string;
  make(a: string | number): string {
    return String(a);
  }
}
"""

# An abstract property's getter and setter beside a static method of the
# same name: the accessors are one member, the static set another function.
ABSTRACT_ACCESSORS_TS = """\
export abstract class Shape {
  static size(a: string): number;
  static size(a: number): number;
  static size(a: string | number): number {
    return Number(a);
  }
  abstract get size(): number;
  abstract set size(v: number);
}
"""


def test_renaming_an_abstract_member_leaves_the_static_overloads_alone(
    tmp_path: Path,
) -> None:
    # Applied without the re-ingest: with it the contract refuses this shape
    # for a separate reason, since the static implementation's `@5` becomes
    # the plain name once the abstract member gives it up.
    root = tmp_path / PROJECT
    store, _updater = _index(root, {"shape.ts": ABSTRACT_THEN_STATIC_TS})
    report = rename(root, store.fetch_all, PROJECT, f"{PROJECT}.shape.A.f", "g")
    assert report.applied, report.message
    text = (root / "shape.ts").read_text(encoding="utf-8")
    assert text == ABSTRACT_THEN_STATIC_TS.replace("abstract f(", "abstract g(")
    _assert_type_checks(root / "shape.ts")


def test_a_contract_checked_rename_leaves_the_static_overloads_alone(
    tmp_path: Path,
) -> None:
    root, _store = _renamed(
        tmp_path,
        {"shape.ts": STATIC_THEN_ABSTRACT_TS},
        f"{PROJECT}.shape.A.f{cs.DUP_QN_MARKER}7",
        "g",
    )
    text = (root / "shape.ts").read_text(encoding="utf-8")
    assert text == STATIC_THEN_ABSTRACT_TS.replace("abstract f(", "abstract g(")
    _assert_type_checks(root / "shape.ts")


def test_a_static_overload_set_folds_and_renames_together(tmp_path: Path) -> None:
    store, _updater = _index(tmp_path / "graph", {"codec.ts": STATIC_OVERLOADS_TS})
    qn = f"{PROJECT}.codec.Codec.decode"
    assert _spans(store, qn) == {(_METHOD, qn): (4, 6)}
    assert _calls_from(store, f"{PROJECT}.codec.use") == {(qn, _EXACT)}

    root, _store = _renamed(tmp_path, {"codec.ts": STATIC_OVERLOADS_TS}, qn, "parse")
    text = (root / "codec.ts").read_text(encoding="utf-8")
    assert text == STATIC_OVERLOADS_TS.replace("decode(", "parse(")
    _assert_type_checks(root / "codec.ts")


def test_an_instance_overload_set_beside_a_static_method_stays_its_own(
    tmp_path: Path,
) -> None:
    store, _updater = _index(
        tmp_path / "graph", {"box.ts": STATIC_BESIDE_INSTANCE_OVERLOADS_TS}
    )
    qn = f"{PROJECT}.box.Box.get"
    variant = f"{qn}{cs.DUP_QN_MARKER}7"
    assert _spans(store, qn) == {(_METHOD, qn): (2, 4), (_METHOD, variant): (7, 9)}

    root, _store = _renamed(
        tmp_path, {"box.ts": STATIC_BESIDE_INSTANCE_OVERLOADS_TS}, variant, "fetch"
    )
    text = (root / "box.ts").read_text(encoding="utf-8")
    assert text == STATIC_BESIDE_INSTANCE_OVERLOADS_TS.replace("  get(k:", "  fetch(k:")
    _assert_type_checks(root / "box.ts")


def test_a_static_and_an_instance_overload_set_stay_two_functions(
    tmp_path: Path,
) -> None:
    store, _updater = _index(tmp_path / "graph", {"pair.ts": BOTH_OVERLOADED_TS})
    qn = f"{PROJECT}.pair.Pair.make"
    variant = f"{qn}{cs.DUP_QN_MARKER}10"
    assert _spans(store, qn) == {(_METHOD, qn): (5, 7), (_METHOD, variant): (10, 12)}
    nodes = {str(uid): props for (_label, uid), props in store.nodes.items()}
    # The static set's doc is its own, never the instance implementation's.
    assert nodes[qn].get(cs.KEY_DOCSTRING) == "Builds a pair."
    assert nodes[variant].get(cs.KEY_DOCSTRING) is None

    root, _store = _renamed(tmp_path, {"pair.ts": BOTH_OVERLOADED_TS}, variant, "show")
    assert (root / "pair.ts").read_text(encoding="utf-8") == (
        BOTH_OVERLOADED_TS.replace("  make(a:", "  show(a:")
    )
    _assert_type_checks(root / "pair.ts")


def test_a_static_signature_is_no_overload_of_an_instance_implementation() -> None:
    # Not valid TypeScript (TS2387), but parseable: the key, not the source
    # order, decides what belongs to an implementation.
    parsers, _queries = load_parsers()
    source = b"class C {\n  static f(a: string): void;\n  f(a: string) {}\n}\n"
    root = parsers[cs.SupportedLanguage.TS].parse(source).root_node
    body = root.named_children[0].child_by_field_name(cs.FIELD_BODY)
    assert body is not None
    implementation = body.named_children[-1]
    assert implementation.type == cs.TS_METHOD_DEFINITION
    assert implementation_signatures(implementation) == []
    assert overload_signatures(implementation) == []


def test_an_abstract_accessor_pair_is_renamed_together(tmp_path: Path) -> None:
    # A getter and a setter of one abstract property are one member, folded
    # by the interface/abstract rule (issue #2524), so both names move; the
    # static `size` overloads are another function and keep theirs.
    root, _store = _renamed(
        tmp_path,
        {"shape.ts": ABSTRACT_ACCESSORS_TS},
        f"{PROJECT}.shape.Shape.size{cs.DUP_QN_MARKER}7",
        "extent",
    )
    text = (root / "shape.ts").read_text(encoding="utf-8")
    assert text == ABSTRACT_ACCESSORS_TS.replace("t size(", "t extent(")
    _assert_type_checks(root / "shape.ts")


# --- incremental re-parse equals a clean index ----------------------------------


@pytest.mark.parametrize("edited", ["fmt.ts", "use.ts", "ns.ts", "lib.d.ts"])
def test_an_incremental_run_matches_a_clean_index(tmp_path: Path, edited: str) -> None:
    # Re-parsing the overloaded file, and rehydrating it while only its caller
    # is re-parsed, must both give the graph a clean index gives.
    src = tmp_path / "src"
    src.mkdir()
    for name, text in {
        "fmt.ts": FMT_TS,
        "use.ts": USE_FMT_TS,
        "ns.ts": NAMESPACE_TS,
        "lib.d.ts": DECLARE_DTS,
    }.items():
        (src / name).write_text(text, encoding="utf-8")
    parsers, queries = load_parsers()
    incremental, clean = run_neutral_edit_scenario(
        src, PROJECT, edited, parsers, queries, tmp_path / "work"
    )
    assert incremental == clean
    assert (_FUNCTION, f"{PROJECT}.fmt.fmt") in clean.nodes
    assert not any(
        uid.startswith(f"{PROJECT}.fmt.fmt{cs.DUP_QN_MARKER}")
        for _label, uid in clean.nodes
    )


# --- negative: what must stay as it was ------------------------------------------


PAD_TS = """\
export function pad(s: string): string;
export function pad(s: string, width: number): string;
export function pad(s: string, width?: number): string {
  return s.padStart(width ?? 0);
}
"""

PAD_VIEW_TS = """\
import { pad } from "./pad";

export function show(): string {
  return pad("a", 2);
}
"""


def test_an_overloaded_call_is_not_judged_by_the_implementations_parameters(
    tmp_path: Path,
) -> None:
    # A caller sees only the overloads: `pad("a", 2)` matches the second, and
    # tsc accepts an implementation declaring fewer parameters than an
    # overload. Now that the call binds `exact`, judging it by the
    # implementation's list would make it a `too_many` finding.
    root = tmp_path / PROJECT
    store, _updater = _index(root, {"pad.ts": PAD_TS, "view.ts": PAD_VIEW_TS})
    for args in (("init", "-q"), ("add", "-A")):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    (root / "pad.ts").write_text(
        PAD_TS.replace(
            "(s: string, width?: number): string {", "(s: string): string {"
        ).replace("s.padStart(width ?? 0)", "s"),
        encoding="utf-8",
    )
    parsers, queries = load_parsers()
    delta = run_check(root, "HEAD", PROJECT, store, parsers, queries)
    assert not has_findings(delta), delta["signature_changes"]
    pad = store.nodes[(_FUNCTION, f"{PROJECT}.pad.pad")]
    assert pad.get(cs.KEY_POSITIONAL_PARAMS) is None
    show = store.nodes[(_FUNCTION, f"{PROJECT}.view.show")]
    assert show.get(cs.KEY_POSITIONAL_PARAMS) == []


DISTINCT_SCOPES_TS = """\
export function a(): number {
  function helper(x: string): number;
  function helper(x: number): number;
  function helper(x: string | number): number {
    return Number(x);
  }
  return helper(1);
}

export function b(): number {
  function helper(): number {
    return 2;
  }
  return helper();
}

export class Box {
  helper(x: string): number;
  helper(x: number): number;
  helper(x: string | number): number {
    return Number(x);
  }
}

export function helper(): number {
  return 3;
}
"""


def test_same_named_functions_in_other_scopes_stay_distinct(tmp_path: Path) -> None:
    store, _updater = _index(tmp_path / PROJECT, {"scopes.ts": DISTINCT_SCOPES_TS})
    mod = f"{PROJECT}.scopes"
    assert _spans(store, f"{mod}.a.helper") == {(_FUNCTION, f"{mod}.a.helper"): (4, 6)}
    assert _spans(store, f"{mod}.b.helper") == {
        (_FUNCTION, f"{mod}.b.helper"): (11, 13)
    }
    assert _spans(store, f"{mod}.Box.helper") == {
        (_METHOD, f"{mod}.Box.helper"): (20, 22)
    }
    assert _spans(store, f"{mod}.helper") == {(_FUNCTION, f"{mod}.helper"): (25, 27)}
    assert _calls_from(store, f"{mod}.a") == {(f"{mod}.a.helper", _EXACT)}
    assert _calls_from(store, f"{mod}.b") == {(f"{mod}.b.helper", _EXACT)}


def test_signatures_in_separate_ambient_modules_stay_two_functions(
    tmp_path: Path,
) -> None:
    # Two `declare module` blocks each declare their own `make`; folding is
    # per block, so neither absorbs the other.
    text = (
        'declare module "left" {\n'
        "  export function make(x: string): string;\n"
        "}\n"
        'declare module "right" {\n'
        "  export function make(x: number): number;\n"
        "}\n"
    )
    store, _updater = _index(tmp_path / PROJECT, {"lib.d.ts": text})
    qn = f"{PROJECT}.lib.make"
    assert _spans(store, qn) == {
        (_FUNCTION, qn): (2, 2),
        (_FUNCTION, f"{qn}{cs.DUP_QN_MARKER}5"): (5, 5),
    }


def test_a_javascript_redefinition_keeps_its_line_variant(tmp_path: Path) -> None:
    # JavaScript has no overload signatures: a second `function dup` really
    # rebinds the name, and both definitions stay as they were.
    text = (
        "function dup() {\n"
        "  return 1;\n"
        "}\n"
        "function dup() {\n"
        "  return 2;\n"
        "}\n"
        "export function call() {\n"
        "  return dup();\n"
        "}\n"
    )
    store, _updater = _index(tmp_path / PROJECT, {"dup.js": text})
    qn = f"{PROJECT}.dup.dup"
    variant = f"{qn}{cs.DUP_QN_MARKER}4"
    assert _spans(store, qn) == {(_FUNCTION, qn): (1, 3), (_FUNCTION, variant): (4, 6)}
    assert {target for target, _how in _calls_from(store, f"{PROJECT}.dup.call")} == {
        qn,
        variant,
    }


def test_a_type_twin_keeps_its_own_label_beside_the_folded_function(
    tmp_path: Path,
) -> None:
    # Declaration merging (issue #2520): the interface lives in the type
    # space, so the implementation keeps the plain name for the value.
    text = (
        "export interface Codec {\n"
        "  value: number;\n"
        "}\n"
        "export function Codec(x: string): Codec;\n"
        "export function Codec(x: number): Codec;\n"
        "export function Codec(x: string | number): Codec {\n"
        "  return { value: Number(x) };\n"
        "}\n"
        "export function use(): Codec {\n"
        '  return Codec("1");\n'
        "}\n"
    )
    store, _updater = _index(tmp_path / PROJECT, {"codec.ts": text})
    qn = f"{PROJECT}.codec.Codec"
    assert _spans(store, qn) == {(_FUNCTION, qn): (6, 8)}
    assert (cs.NodeLabel.INTERFACE.value, qn) in store.nodes
    assert _calls_from(store, f"{PROJECT}.codec.use") == {(qn, _EXACT)}


def test_renaming_a_same_named_method_leaves_the_overloads_alone(
    tmp_path: Path,
) -> None:
    text = INTERNAL_TS + (
        "\nexport class Other {\n  parse(): number {\n    return 2;\n  }\n}\n"
    )
    root, _store = _renamed(
        tmp_path, {"internal.ts": text}, f"{PROJECT}.internal.Other.parse", "read"
    )
    assert (root / "internal.ts").read_text(encoding="utf-8") == text.replace(
        "  parse(): number {", "  read(): number {"
    )


def test_a_function_without_overloads_renames_as_before(tmp_path: Path) -> None:
    root, _store = _renamed(
        tmp_path, {"internal.ts": INTERNAL_TS}, f"{PROJECT}.internal.used", "usedOnce"
    )
    assert (root / "internal.ts").read_text(encoding="utf-8") == INTERNAL_TS.replace(
        "used", "usedOnce"
    )


def test_the_heuristic_refusal_still_guards_a_real_fan_out(tmp_path: Path) -> None:
    # Two JavaScript definitions of one name are two candidates for the
    # call, so renaming one still refuses without the opt-in.
    text = (
        "function dup() {}\nfunction dup() {}\nexport function call() {\n  dup();\n}\n"
    )
    root = tmp_path / PROJECT
    store, _updater = _index(root, {"dup.js": text})
    with pytest.raises(RenameRefused):
        rename(root, store.fetch_all, PROJECT, f"{PROJECT}.dup.dup", "once")
    assert (root / "dup.js").read_text(encoding="utf-8") == text
