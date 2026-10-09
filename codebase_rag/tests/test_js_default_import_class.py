"""A default-imported JS/TS class types its instances as a named import does.

`import Store from "./Store.js"` maps `Store` to `<module>.default`, which
nothing is registered under for a named `export default class Store`. Its
instances stayed untyped, the class was invisible to the unique-method
fallback, and the calling module's own same-named method took the call:
`s.fetch()` bound `Cache.fetch`, and a delegating `Cache.fetch` called
itself (issue #3179; three.js: 491 self-loop CALLS, mostly such delegates).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

_JS = {
    "src/Store.js": "export default class Store {\n  fetch(k) { return k; }\n}\n",
    "src/use.js": """\
import Store from "./Store.js";
export function viaLocal() {
  const s = new Store();
  return s.fetch("k");
}
export function viaNew() {
  return new Store().fetch("k");
}
export class Cache {
  constructor() { this.store = new Store(); }
  fetch(k) { return this.store.fetch(k); }
}
""",
    "src/Utils.js": (
        "class Utils {\n  createDefaultTexture(t) { return t; }\n}\nexport default Utils;\n"
    ),
    "src/Backend.js": """\
import Utils from "./Utils.js";
export class Backend {
  constructor() { this.textureUtils = new Utils(); }
  createDefaultTexture(t) { return this.textureUtils.createDefaultTexture(t); }
}
export function make() {
  const u = new Utils();
  return u.createDefaultTexture(1);
}
""",
    "src/Anon.js": "export default class {\n  ping() { return 1; }\n}\n",
    "src/anon_use.js": (
        'import Anon from "./Anon.js";\n'
        "export function pinged() { return new Anon().ping(); }\n"
    ),
    "src/Named.js": "export class Named {\n  fetch(k) { return k; }\n}\n",
    "src/named_use.js": (
        'import { Named } from "./Named.js";\n'
        'export function viaNamed() { return new Named().fetch("k"); }\n'
    ),
}
_TS = {
    "src/Store.ts": (
        "export default class Store {\n  lookup(k: string): string { return k; }\n}\n"
    ),
    "src/Other.ts": (
        "export class Other {\n  lookup(k: string): string { return k; }\n}\n"
    ),
    "src/param.ts": (
        'import Store from "./Store";\n'
        'export function viaParam(s: Store) {\n  return s.lookup("k");\n}\n'
    ),
    "src/both.ts": (
        'import Store from "./Store";\nimport { Other } from "./Other";\n'
        'export function viaBoth(s: Store, o: Other) {\n  return s.lookup("k");\n}\n'
    ),
    "src/use.ts": """\
import Store from "./Store";
export function viaLocal() {
  const s = new Store();
  return s.lookup("k");
}
export class Cache {
  private store: Store = new Store();
  lookup(k: string): string { return this.store.lookup(k); }
}
""",
}

_Calls = dict[tuple[str, str], str]


def _calls(root: Path, files: dict[str, str], grammar: str) -> _Calls:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    mock = MagicMock()
    create_and_run_updater(root, mock, skip_if_missing=grammar)
    return {
        (str(c.args[0][2]).split(".", 1)[1], str(c.args[2][2]).split(".", 1)[1]): str(
            (c.kwargs.get("properties") or {}).get(cs.KEY_RESOLUTION)
        )
        for c in get_relationships(mock, cs.RelationshipType.CALLS)
    }


@pytest.fixture(scope="module")
def js(tmp_path_factory: pytest.TempPathFactory) -> _Calls:
    return _calls(tmp_path_factory.mktemp("js3179") / "jsdefcls", _JS, "javascript")


@pytest.fixture(scope="module")
def ts(tmp_path_factory: pytest.TempPathFactory) -> _Calls:
    return _calls(tmp_path_factory.mktemp("ts3179") / "tsdefcls", _TS, "typescript")


def _callees(calls: _Calls, caller: str) -> dict[str, str]:
    return {to: res for (src, to), res in calls.items() if src == caller}


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("src.use.viaLocal", "src.Store.Store.fetch"),
        ("src.use.viaNew", "src.Store.Store.fetch"),
        ("src.Backend.make", "src.Utils.Utils.createDefaultTexture"),
    ],
    ids=["local", "new-expression", "export-default-identifier"],
)
def test_a_default_imported_class_types_its_instances(
    js: _Calls, caller: str, callee: str
) -> None:
    assert _callees(js, caller) == {callee: "exact"}, js


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("src.param.viaParam", "src.Store.Store.lookup"),
        ("src.use.viaLocal", "src.Store.Store.lookup"),
    ],
    ids=["annotated-parameter", "local"],
)
def test_a_default_imported_ts_class_types_its_instances(
    ts: _Calls, caller: str, callee: str
) -> None:
    assert _callees(ts, caller) == {callee: "exact"}, ts


@pytest.mark.parametrize(
    ("calls", "method"),
    [
        ("js", "src.use.Cache.fetch"),
        ("js", "src.Backend.Backend.createDefaultTexture"),
        ("ts", "src.use.Cache.lookup"),
    ],
    ids=["js-delegate", "js-export-default-identifier", "ts-delegate"],
)
def test_a_delegating_method_does_not_call_itself(
    request: pytest.FixtureRequest, calls: str, method: str
) -> None:
    edges: _Calls = request.getfixturevalue(calls)
    assert (method, method) not in edges, edges


def test_the_wrong_visible_method_is_not_the_unique_pick(ts: _Calls) -> None:
    # `s: Store` (default) and `o: Other` (named) both define `lookup`; the
    # untyped fallback saw only Other's and bound `s.lookup()` to it.
    assert ("src.both.viaBoth", "src.Other.Other.lookup") not in ts, ts


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("src.named_use.viaNamed", "src.Named.Named.fetch"),
        ("src.anon_use.pinged", "src.Anon.default.ping"),
    ],
    ids=["named-import", "anonymous-default-class"],
)
def test_named_and_anonymous_default_classes_resolve_as_before(
    js: _Calls, caller: str, callee: str
) -> None:
    # Negatives: a named import and an anonymous `export default class {}`
    # (registered under `<module>.default` itself) are unchanged.
    assert _callees(js, caller) == {callee: "exact"}, js
