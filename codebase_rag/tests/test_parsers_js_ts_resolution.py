"""`JsTsResolver` and its helpers: the class value a hop names, a `this`
return bound to the receiver, a builtin a call names, a prototype sibling, a
package-imported head, a call through a re-export, a static member up the
chain, and a member call only one visible definition answers."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.js_ts.resolution import (
    JsTsResolver,
    bind_receiver,
    class_value_of,
    resolve_builtin_call,
)
from codebase_rag.tests.collaborator_support import index
from evals.recorded_graph import RecordingIngestor

_JS = cs.SupportedLanguage.JS
_TS = cs.SupportedLanguage.TS
_FILES = {
    "src/impl.ts": "export function greet() { return 1; }\n",
    "src/barrel.ts": "export { greet } from './impl';\n",
    "src/base.ts": (
        "export class Base {\n  static make() { return new Base(); }\n}\n"
        "export class Child extends Base {}\n"
        "export class Plain {\n  run() {}\n}\n"
    ),
    "src/app.ts": (
        "import * as Prim from '@radix-ui/react-accordion';\n"
        "import { greet } from './barrel';\n"
        "import { Plain } from './base';\n"
        "export function main() { greet(); return Prim.Item; }\n"
    ),
    "src/proto.js": (
        "function Ctor() {}\n"
        "Ctor.prototype.helper = function () { return 1; };\n"
        "Ctor.prototype.run = function () { return this.helper(); };\n"
    ),
}


@pytest.fixture
def resolver(temp_repo: Path, mock_ingestor: RecordingIngestor) -> JsTsResolver:
    updater = index(temp_repo / "tp", mock_ingestor, _FILES)
    js = updater.factory.call_processor._resolver.js_ts
    assert isinstance(js, JsTsResolver)
    return js


def test_a_class_value_type_names_its_class() -> None:
    assert class_value_of("typeof p.Base") == "p.Base"
    assert class_value_of("p.Base") is None


def test_a_this_return_binds_to_the_receiver_it_went_through() -> None:
    assert bind_receiver("this", "p.Base") == "p.Base"
    assert bind_receiver("typeof this", "p.Base") == "typeof p.Base"
    assert bind_receiver("typeof this", None) is None
    assert bind_receiver("p.Other", "p.Base") == "p.Other"


@pytest.mark.parametrize(
    ("call_name", "expected"),
    [
        ("Object.keys", ("Function", "builtin.Object.keys")),
        ("handler.bind", ("Function", "builtin.Function.prototype.bind")),
        ("handler.call", ("Function", "builtin.Function.prototype.call")),
        ("project.helper", None),
    ],
)
def test_a_builtin_call_is_named_under_the_builtin_prefix(
    call_name: str, expected: tuple[str, str] | None
) -> None:
    assert resolve_builtin_call(call_name) == expected


@pytest.mark.xfail(
    strict=True,
    reason=(
        "resolve_builtin_call's `.prototype.` branch, which names the method a "
        "`Base.prototype.run.call(this)` runs, is unreachable: the loop over "
        "JS_FUNCTION_PROTOTYPE_SUFFIXES before it matches every `.call` and "
        "`.apply` name first and answers builtin Function.prototype.call"
    ),
)
def test_a_prototype_methods_call_names_the_method() -> None:
    assert resolve_builtin_call("Base.prototype.run.call") == (
        "Function",
        "Base.prototype.run",
    )


def test_this_in_a_prototype_method_reaches_its_sibling(resolver: JsTsResolver) -> None:
    assert resolver.resolve_js_prototype_sibling(
        "this.helper", "tp.src.proto.Ctor.run", _JS
    ) == (cs.NodeLabel.FUNCTION, "tp.src.proto.Ctor.helper")
    assert (
        resolver.resolve_js_prototype_sibling(
            "this.helper", "tp.src.proto.Ctor.run", cs.SupportedLanguage.PYTHON
        )
        is None
    )
    assert (
        resolver.resolve_js_prototype_sibling("helper", "tp.src.proto.Ctor.run", _JS)
        is None
    )


def test_a_head_imported_from_a_package_is_external(resolver: JsTsResolver) -> None:
    assert resolver.js_member_head_is_external("Prim.Item", "tp.src.app")
    assert not resolver.js_member_head_is_external("Plain.run", "tp.src.app")
    assert not resolver.js_member_head_is_external("Item", "tp.src.app")


def test_a_call_through_a_barrel_reaches_the_declaration(
    resolver: JsTsResolver,
) -> None:
    import_map = {"greet": "tp.src.barrel.greet"}
    assert resolver.try_resolve_js_reexport("greet", import_map, _TS, "tp.src.app") == (
        cs.NodeLabel.FUNCTION,
        "tp.src.impl.greet",
    )
    # A name the map does not hold, or another language, is no re-export.
    assert (
        resolver.try_resolve_js_reexport("other", import_map, _TS, "tp.src.app") is None
    )


def test_a_static_member_is_found_up_the_class_chain(resolver: JsTsResolver) -> None:
    found = resolver.js_static_member("tp.src.base.Child", "make")
    assert found is not None
    assert found[0] == "tp.src.base.Base.make"
    # An instance method answers no static lookup.
    assert resolver.js_static_member("tp.src.base.Plain", "run") is None


def test_a_member_call_binds_when_one_visible_definition_answers(
    resolver: JsTsResolver,
) -> None:
    assert resolver.resolve_js_member_call_unique("p.run", "tp.src.app") == (
        cs.NodeLabel.METHOD,
        "tp.src.base.Plain.run",
    )
    assert resolver.resolve_js_member_call_unique("p.missing", "tp.src.app") is None
