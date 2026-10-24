"""Issue #2894: a TS/JS class extending an export-renamed base inherits from it.

`class Animal {}` + `export { Animal as AnimalBase }` publishes the class
under a name nothing is registered under. `import { AnimalBase }` maps the
name to `base.AnimalBase`, and the parent lookup searched `base` for a class
literally called `AnimalBase`: no INHERITS edge, so every inherited-method
call on a subclass instance resolved to nothing. hono's `Hono extends
HonoBase` (`export { Hono as HonoBase }`) lost its whole base-class surface.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

INHERITS = cs.RelationshipType.INHERITS.value
CALLS = cs.RelationshipType.CALLS.value

# The issue's minimal repro, verbatim.
ISSUE_FILES = {
    "base.ts": (
        'export class Vehicle { move(): string { return "go"; } }\n'
        'class Animal { speak(): string { return "..."; } }\n'
        "export { Animal as AnimalBase };\n"
    ),
    "app.ts": (
        'import { Vehicle } from "./base";\n'
        'import { AnimalBase } from "./base";\n'
        "class Car extends Vehicle { }\n"
        "class Dog extends AnimalBase { }\n"
        "const c = new Car(); c.move();\n"
        "const d = new Dog(); d.speak();\n"
    ),
}

# hono's shape: the subclass takes the base's declared name.
HONO_FILES = {
    "src/hono-base.ts": (
        "class Hono {\n"
        "  onError(handler: string): Hono { return this; }\n"
        "  mount(path: string): Hono { return this; }\n"
        "}\n"
        "export { Hono as HonoBase };\n"
    ),
    "src/hono.ts": (
        'import { HonoBase } from "./hono-base";\n'
        "export class Hono extends HonoBase { }\n"
    ),
    "src/app.ts": (
        'import { Hono } from "./hono";\n'
        "export function build() {\n"
        "  const app = new Hono();\n"
        '  app.onError("e");\n'
        '  return app.mount("/v1");\n'
        "}\n"
    ),
}

# The alias reached through a barrel that renames it again, and the plain
# JavaScript spelling.
CHAIN_FILES = {
    "zoo/base.js": (
        "class Animal { speak() { return 1; } }\nexport { Animal as AnimalBase };\n"
    ),
    "zoo/index.js": 'export { AnimalBase as Beast } from "./base.js";\n',
    "cat.js": (
        'import { Beast } from "./zoo/index.js";\n'
        "export class Cat extends Beast { }\n"
        "export function purr() { return new Cat().speak(); }\n"
    ),
}

NEGATIVE_FILES = {
    "lib.ts": (
        "const LIMIT = 3;\n"
        "export { LIMIT as Limit };\n"
        "class Engine { start(): number { return 1; } }\n"
        "export { Engine };\n"
    ),
    "use.ts": (
        'import { Limit, Engine as EngineBase } from "./lib";\n'
        'import { Missing } from "./lib";\n'
        "export class NotAClass extends Limit { }\n"
        "export class Car extends EngineBase { }\n"
        "export class Ghost extends Missing { }\n"
    ),
}


def _index(root: Path, files: dict[str, str]) -> _StatefulIngestor:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.TS not in parsers or cs.SupportedLanguage.JS not in parsers:
        pytest.skip("javascript/typescript parsers not available")
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=root.name,
    ).run(force=True)
    return store


def _edges(store: _StatefulIngestor, rel_type: str) -> set[tuple[str, str]]:
    return {
        (str(edge[1]), str(edge[4]))
        for edge in store.keyed_edges
        if edge[2] == rel_type
    }


def test_the_issue_subclass_inherits_from_the_aliased_class(tmp_path: Path) -> None:
    root = tmp_path / "zoo"
    store = _index(root, ISSUE_FILES)
    inherits = _edges(store, INHERITS)
    assert ("zoo.app.Dog", "zoo.base.Animal") in inherits, inherits
    # The control the issue's table starts from.
    assert ("zoo.app.Car", "zoo.base.Vehicle") in inherits, inherits


def test_the_inherited_call_reaches_the_aliased_class(tmp_path: Path) -> None:
    root = tmp_path / "zoo"
    calls = _edges(_index(root, ISSUE_FILES), CALLS)
    assert ("zoo.app", "zoo.base.Animal.speak") in calls, calls
    assert ("zoo.app", "zoo.base.Vehicle.move") in calls, calls


def test_hono_keeps_its_base_class_surface(tmp_path: Path) -> None:
    root = tmp_path / "hono"
    store = _index(root, HONO_FILES)
    assert ("hono.src.hono.Hono", "hono.src.hono-base.Hono") in _edges(store, INHERITS)
    calls = _edges(store, CALLS)
    for method in ("onError", "mount"):
        target = f"hono.src.hono-base.Hono.{method}"
        assert ("hono.src.app.build", target) in calls, (method, calls)


def test_an_alias_renamed_again_by_a_barrel_is_followed(tmp_path: Path) -> None:
    root = tmp_path / "pets"
    store = _index(root, CHAIN_FILES)
    assert ("pets.cat.Cat", "pets.zoo.base.Animal") in _edges(store, INHERITS)
    calls = _edges(store, CALLS)
    assert ("pets.cat.purr", "pets.zoo.base.Animal.speak") in calls, calls


def test_only_a_class_behind_the_alias_is_a_base(tmp_path: Path) -> None:
    # Negatives: an aliased constant is no base (an aliased function would
    # be: JS classes extend constructor functions), and a name the module
    # never exports binds nothing first-party; an import-side rename still
    # works.
    root = tmp_path / "neg"
    inherits = _edges(_index(root, NEGATIVE_FILES), INHERITS)
    assert not {
        p for c, p in inherits if c == "neg.use.NotAClass" and p.startswith("neg.")
    }, inherits
    assert not {
        p for c, p in inherits if c == "neg.use.Ghost" and p.startswith("neg.")
    }, inherits
    assert ("neg.use.Car", "neg.lib.Engine") in inherits, inherits
