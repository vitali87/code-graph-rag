# A JS/TS member call whose receiver the resolver cannot type is bound to the
# ONE visible class defining that method (issue #2609). That pick is a guess
# by name, so it is labelled `heuristic`; it is `exact` only when what the
# call site declares about the receiver (an annotation, JSDoc, a construction
# or the enclosing class for `this`) names a class owning the method, and it
# is dropped when that declaration names a type the project does not define
# (`Map`, `URLSearchParams`, `any`, `unknown`).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater

EXACT = cs.EdgeResolution.EXACT
HEURISTIC = cs.EdgeResolution.HEURISTIC

MODELS_JS = (
    "class A { count(x) { return 1 } }\n"
    "class B { count(x) { return 2 } }\n"
    "class Payload { encode() { return '' } }\n"
    "module.exports = { A, B, Payload }\n"
)
MODELS_TS = (
    "export class A {\n"
    "  count(x: number): number { return 1 }\n"
    "  static make(): A { return new A() }\n"
    "}\n"
    "export class B { count(x: number): number { return 2 } }\n"
)
CACHE_TS = "export class Cache { get(key: string): string { return key; } }\n"


def _calls(mock: MagicMock, caller_suffix: str) -> dict[str, set[str]]:
    # callee qn (project prefix dropped) -> the labels its CALLS edges carry.
    found: dict[str, set[str]] = {}
    for c in mock.ensure_relationship_batch.call_args_list:
        if str(c.args[1]) != cs.RelationshipType.CALLS:
            continue
        src = str(c.args[0][2])
        if not src.endswith(caller_suffix):
            continue
        props = (
            c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {}) or {}
        )
        dst = str(c.args[2][2]).split(".", 1)[1]
        found.setdefault(dst, set()).add(str(props.get(cs.KEY_RESOLUTION)))
    return found


def _index(repo: Path, mock: MagicMock, files: dict[str, str]) -> None:
    for name, source in files.items():
        (repo / name).write_text(source)
    create_and_run_updater(repo, mock)


# --- the pick is a guess: heuristic, or nothing when the declaration rules it out


def test_untyped_param_receivers_bind_heuristically(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The issue's repro: neither `xs` nor `payload` says what it is.
    _index(
        temp_repo,
        mock_ingestor,
        {
            "models.js": MODELS_JS,
            "svc.js": (
                "const { A, Payload } = require('./models')\n"
                "function handle(xs, payload) {\n"
                "  payload.encode()\n"
                "  return xs.count(1)\n"
                "}\n"
                "module.exports = { handle }\n"
            ),
        },
    )
    calls = _calls(mock_ingestor, ".svc.handle")
    assert calls["models.A.count"] == {HEURISTIC}
    assert calls["models.Payload.encode"] == {HEURISTIC}


def test_ts_params_declared_as_types_the_project_does_not_define_take_no_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The issue comment's repro: the annotation names a built-in, so the
    # first-party `Cache.get` contradicts it. `any` and `unknown` say nothing
    # first-party either (Python leaves `xs: Any` unbound too).
    _index(
        temp_repo,
        mock_ingestor,
        {
            "cache.ts": CACHE_TS,
            "handlers.ts": (
                "import { Cache } from './cache';\n"
                "export function header(h: Map<string, string>) "
                "{ return h.get('accept'); }\n"
                "export function query(u: URLSearchParams) { return u.get('q'); }\n"
                "export function element(e: HTMLElement) { return e.get('x'); }\n"
                "export function loose(m: any) { return m.get('x'); }\n"
                "export function opaque(m: unknown) { return m.get('x'); }\n"
                "export function listed(m: Cache[]) { return m.get('x'); }\n"
            ),
        },
    )
    for caller in ("header", "query", "element", "loose", "opaque", "listed"):
        assert "cache.Cache.get" not in _calls(mock_ingestor, f".handlers.{caller}")


def test_untyped_ts_param_binds_heuristically(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(
        temp_repo,
        mock_ingestor,
        {
            "cache.ts": CACHE_TS,
            "handlers.ts": (
                "import { Cache } from './cache';\n"
                "export function bare(m) { return m.get('x'); }\n"
            ),
        },
    )
    assert _calls(mock_ingestor, ".handlers.bare")["cache.Cache.get"] == {HEURISTIC}


def test_a_first_party_interface_param_does_not_confirm_an_implementor(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `Repo<Shape>` has two implementors; the file importing one of them is
    # no evidence that `repo` is that one.
    _index(
        temp_repo,
        mock_ingestor,
        {
            "repo.ts": "export interface Repo<T> { find(id: string): T }\n",
            "impls.ts": (
                "import { Repo } from './repo'\n"
                "export class UserRepo implements Repo<string> "
                "{ find(id: string): string { return id } }\n"
                "export class OrderRepo implements Repo<number> "
                "{ find(id: string): number { return 1 } }\n"
            ),
            "use.ts": (
                "import { Repo } from './repo'\n"
                "import { UserRepo } from './impls'\n"
                "type Shape = string\n"
                "export function load(repo: Repo<Shape>) { return repo.find('x') }\n"
            ),
        },
    )
    assert _calls(mock_ingestor, ".use.load")["impls.UserRepo.find"] == {HEURISTIC}


def test_untyped_fields_locals_and_callbacks_bind_heuristically(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(
        temp_repo,
        mock_ingestor,
        {
            "models.js": MODELS_JS,
            "svc.js": (
                "const { A } = require('./models')\n"
                "class Holder {\n"
                "  constructor(x) { this.x = x }\n"
                "  run() { return this.x.count(1) }\n"
                "}\n"
                "function load() { return null }\n"
                "function viaLocal() { const xs = load(); return xs.count(1) }\n"
                "function viaCallback(ys) { return ys.map((y) => y.count(1)) }\n"
                "module.exports = { Holder, viaLocal, viaCallback }\n"
            ),
        },
    )
    assert _calls(mock_ingestor, ".svc.Holder.run")["models.A.count"] == {HEURISTIC}
    assert _calls(mock_ingestor, ".svc.viaLocal")["models.A.count"] == {HEURISTIC}
    callback = {
        dst: labels
        for dst, labels in _calls(mock_ingestor, "").items()
        if dst == "models.A.count"
    }
    assert callback == {"models.A.count": {HEURISTIC}}


def test_this_call_in_a_class_without_that_method_binds_heuristically(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(
        temp_repo,
        mock_ingestor,
        {
            "models.js": MODELS_JS,
            "svc.js": (
                "const { A } = require('./models')\n"
                "class Other { run() { return this.count(1) } }\n"
                "module.exports = { Other }\n"
            ),
        },
    )
    assert _calls(mock_ingestor, ".svc.Other.run")["models.A.count"] == {HEURISTIC}


def test_ts_fields_declared_loosely_bind_heuristically_or_not_at_all(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(
        temp_repo,
        mock_ingestor,
        {
            "models.ts": MODELS_TS,
            "svc.ts": (
                "import { A } from './models'\n"
                "export class Bare { x; run() { return this.x.count(1) } }\n"
                "export class Loose { x: any; run() { return this.x.count(1) } }\n"
            ),
        },
    )
    assert _calls(mock_ingestor, ".svc.Bare.run")["models.A.count"] == {HEURISTIC}
    assert "models.A.count" not in _calls(mock_ingestor, ".svc.Loose.run")


def test_jsdoc_naming_a_builtin_takes_no_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(
        temp_repo,
        mock_ingestor,
        {
            "models.js": MODELS_JS,
            "svc.js": (
                "const { A } = require('./models')\n"
                "/**\n * @param {Map} m\n */\n"
                "function viaMap(m) { return m.count(1) }\n"
                "/** @param {*} m */\n"
                "function viaAny(m) { return m.count(1) }\n"
                "module.exports = { viaMap, viaAny }\n"
            ),
        },
    )
    assert "models.A.count" not in _calls(mock_ingestor, ".svc.viaMap")
    assert "models.A.count" not in _calls(mock_ingestor, ".svc.viaAny")


# --- negative: a receiver whose declaration names the owner stays exact ---------


def test_constructed_and_static_receivers_stay_exact(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(
        temp_repo,
        mock_ingestor,
        {
            "models.ts": MODELS_TS,
            "svc.ts": (
                "import { A } from './models'\n"
                "export function local() { const c = new A(); return c.count(1) }\n"
                "export function stat() { return A.make() }\n"
                "export const shared = new A()\n"
                "export function viaShared() { return shared.count(1) }\n"
            ),
        },
    )
    assert _calls(mock_ingestor, ".svc.local")["models.A.count"] == {EXACT}
    assert _calls(mock_ingestor, ".svc.stat")["models.A.make"] == {EXACT}
    assert _calls(mock_ingestor, ".svc.viaShared")["models.A.count"] == {EXACT}


def test_annotated_ts_params_stay_exact(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(
        temp_repo,
        mock_ingestor,
        {
            "models.ts": MODELS_TS,
            "svc.ts": (
                "import { A } from './models'\n"
                "export function typed(a: A) { return a.count(1) }\n"
                "export function optional(a?: A) { return a!.count(1) }\n"
                "export function nullable(a: A | null) { return a!.count(1) }\n"
                "export const arrow = (a: A) => a.count(1)\n"
                "export function frozen(a: Readonly<A>) { return a.count(1) }\n"
            ),
        },
    )
    for caller in ("typed", "optional", "nullable", "arrow", "frozen"):
        assert _calls(mock_ingestor, f".svc.{caller}")["models.A.count"] == {EXACT}


def test_declared_field_receivers_stay_exact(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(
        temp_repo,
        mock_ingestor,
        {
            "models.ts": MODELS_TS,
            "svc.ts": (
                "import { A } from './models'\n"
                "export class Annotated { x: A; run() { return this.x.count(1) } }\n"
                "export class Prop {\n"
                "  constructor(private readonly x: A) {}\n"
                "  run() { return this.x.count(1) }\n"
                "}\n"
                "export class Init { x = new A(); run() { return this.x.count(1) } }\n"
                "export class Ctor {\n"
                "  constructor() { this.x = new A() }\n"
                "  run() { return this.x.count(1) }\n"
                "}\n"
            ),
        },
    )
    for owner in ("Annotated", "Prop", "Init", "Ctor"):
        assert _calls(mock_ingestor, f".svc.{owner}.run")["models.A.count"] == {EXACT}


def test_js_constructor_assigned_field_stays_exact(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(
        temp_repo,
        mock_ingestor,
        {
            "models.js": MODELS_JS,
            "svc.js": (
                "const { A } = require('./models')\n"
                "class K {\n"
                "  constructor() { this.x = new A() }\n"
                "  run() { return this.x.count(1) }\n"
                "}\n"
                "module.exports = { K }\n"
            ),
        },
    )
    assert _calls(mock_ingestor, ".svc.K.run")["models.A.count"] == {EXACT}


def test_this_call_inherited_from_an_imported_base_stays_exact(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(
        temp_repo,
        mock_ingestor,
        {
            "models.js": MODELS_JS,
            "svc.js": (
                "const { A } = require('./models')\n"
                "class Sub extends A { run() { return this.count(1) } }\n"
                "module.exports = { Sub }\n"
            ),
        },
    )
    assert _calls(mock_ingestor, ".svc.Sub.run")["models.A.count"] == {EXACT}


def test_jsdoc_typed_param_stays_exact(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(
        temp_repo,
        mock_ingestor,
        {
            "models.js": MODELS_JS,
            "svc.js": (
                "const { A } = require('./models')\n"
                "/** Count. @param {A} xs the counter */\n"
                "function handle(xs) { return xs.count(1) }\n"
                "module.exports = { handle }\n"
            ),
        },
    )
    assert _calls(mock_ingestor, ".svc.handle")["models.A.count"] == {EXACT}


def test_a_type_parameter_keeps_the_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `T` is no global type: it is the caller's own type parameter (TS, or a
    # JSDoc `@template`), so the pick is not contradicted, only unconfirmed.
    _index(
        temp_repo,
        mock_ingestor,
        {
            "models.ts": MODELS_TS,
            "svc.ts": (
                "import { A } from './models'\n"
                "export function generic<T extends A>(x: T) { return x.count(1) }\n"
            ),
            "jsmodels.js": MODELS_JS,
            "tpl.js": (
                "const { A } = require('./jsmodels')\n"
                "/**\n * @template T\n * @param {T} x\n */\n"
                "function templated(x) { return x.count(1) }\n"
                "module.exports = { templated }\n"
            ),
        },
    )
    assert _calls(mock_ingestor, ".svc.generic")["models.A.count"] == {HEURISTIC}
    assert _calls(mock_ingestor, ".tpl.templated")["jsmodels.A.count"] == {HEURISTIC}


# --- negative: no edge appears or vanishes beyond the label --------------------


def test_ambiguous_or_invisible_picks_still_take_no_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Importing A and B leaves two candidates and importing neither leaves
    # none: no edge, even for a receiver annotated as A. The label change
    # never adds an edge the pick did not make.
    _index(
        temp_repo,
        mock_ingestor,
        {
            "models.ts": MODELS_TS,
            "both.ts": (
                "import { A, B } from './models'\n"
                "export function untyped(xs) { return xs.count(1) }\n"
                "export function typed(a: A) { return a.count(1) }\n"
            ),
            "none.ts": "export function untyped(xs) { return xs.count(1) }\n",
        },
    )
    assert _calls(mock_ingestor, ".both.untyped") == {}
    assert _calls(mock_ingestor, ".both.typed") == {}
    assert _calls(mock_ingestor, ".none.untyped") == {}


def test_a_local_builtin_receiver_stays_unbound(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(
        temp_repo,
        mock_ingestor,
        {
            "cache.ts": CACHE_TS,
            "handlers.ts": (
                "import { Cache } from './cache';\n"
                "export function local() { const m = new Map(); return m.get('x'); }\n"
            ),
        },
    )
    assert "cache.Cache.get" not in _calls(mock_ingestor, ".handlers.local")


def test_python_untyped_receiver_stays_heuristic(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(
        temp_repo,
        mock_ingestor,
        {
            "models.py": "class A:\n    def count(self, x):\n        return 1\n",
            "svc.py": "from models import A\n\n\ndef handle(xs):\n    return xs.count(1)\n",
        },
    )
    assert _calls(mock_ingestor, ".svc.handle")["models.A.count"] == {HEURISTIC}
