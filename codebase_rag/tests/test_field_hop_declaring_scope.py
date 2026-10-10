"""A field hop types its field where the field is declared (issue #3286).

`c.engine.start()` with `c: &Car` takes `Engine` from `Car`'s field, but
that name was written in `car.rs`, which says `use crate::motor::Engine`.
The resolver read it in the CALLER's module instead, which imports no
`Engine`, so a project-wide pick could bind `exact` to another `Engine`
(ripgrep: 14 `into_bytes()` calls went to the wrong type). Dart does the
same through `shop.dart`'s imports, which bring in libraries, not names.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater

_ENGINE = "pub struct Engine;\n\nimpl Engine {\n    pub fn start(&self) -> u8 {\n        {n}\n    }\n}\n"

_BASE = {
    "Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\nedition = "2021"\n',
    # Sorts before `motor`, so a name-only pick lands here first.
    "src/a_toy.rs": _ENGINE.replace("{n}", "1"),
    "src/motor.rs": _ENGINE.replace("{n}", "2"),
}


def _calls(repo: Path, mock: MagicMock, caller: str) -> set[tuple[str, str]]:
    prefix = f"{repo.name}."
    out: set[tuple[str, str]] = set()
    for c in mock.ensure_relationship_batch.call_args_list:
        if c.args[1] != cs.RelationshipType.CALLS.value:
            continue
        if str(c.args[0][2]).removeprefix(prefix) != caller:
            continue
        props = (
            c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {}) or {}
        )
        out.add(
            (
                str(c.args[2][2]).removeprefix(prefix),
                str(props.get(cs.KEY_RESOLUTION)),
            )
        )
    return out


def _index(repo: Path, mock: MagicMock, files: dict[str, str]) -> None:
    for rel, text in {**_BASE, **files}.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    create_and_run_updater(repo, mock)


_LIB = (
    "mod a_toy;\nmod car;\nmod motor;\n\nuse car::Car;\n\n"
    "pub fn run(c: &Car) -> u8 {\n    c.engine.start()\n}\n"
)


@pytest.mark.parametrize(
    "car",
    [
        "use crate::motor::Engine;\n\npub struct Car {\n    pub engine: Engine,\n}\n",
        (
            "use crate::motor::Engine as Motor;\n\n"
            "pub struct Car {\n    pub engine: Motor,\n}\n"
        ),
    ],
    ids=["use", "use-as-alias"],
)
def test_the_field_type_resolves_in_the_declaring_module(
    temp_repo: Path, mock_ingestor: MagicMock, car: str
) -> None:
    _index(temp_repo, mock_ingestor, {"src/car.rs": car, "src/lib.rs": _LIB})

    calls = _calls(temp_repo, mock_ingestor, "src.lib.run")
    assert calls == {("src.motor.Engine.start", cs.EdgeResolution.EXACT)}, calls


def test_the_callers_own_import_of_the_name_does_not_retype_the_field(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The caller brings in ANOTHER `Engine` under the same name; the field
    # still holds `car.rs`'s.
    lib = _LIB.replace("use car::Car;\n", "use car::Car;\nuse a_toy::Engine;\n") + (
        "\npub fn toy(e: &Engine) -> u8 {\n    e.start()\n}\n"
    )
    car = "use crate::motor::Engine;\n\npub struct Car {\n    pub engine: Engine,\n}\n"
    _index(temp_repo, mock_ingestor, {"src/car.rs": car, "src/lib.rs": lib})

    assert _calls(temp_repo, mock_ingestor, "src.lib.run") == {
        ("src.motor.Engine.start", cs.EdgeResolution.EXACT)
    }
    # Negative: the caller's own `Engine` is still the one it imports.
    assert _calls(temp_repo, mock_ingestor, "src.lib.toy") == {
        ("src.a_toy.Engine.start", cs.EdgeResolution.EXACT)
    }


def test_a_field_type_declared_beside_its_struct_still_resolves(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: a field whose type the declaring file defines itself.
    car = (
        "pub struct Gear;\n\nimpl Gear {\n    pub fn shift(&self) -> u8 {\n        3\n    }\n}\n\n"
        "pub struct Car {\n    pub gear: Gear,\n}\n"
    )
    lib = (
        "mod a_toy;\nmod car;\nmod motor;\n\nuse car::Car;\n\n"
        "pub fn run(c: &Car) -> u8 {\n    c.gear.shift()\n}\n"
    )
    _index(temp_repo, mock_ingestor, {"src/car.rs": car, "src/lib.rs": lib})

    assert _calls(temp_repo, mock_ingestor, "src.lib.run") == {
        ("src.car.Gear.shift", cs.EdgeResolution.EXACT)
    }


_DART = {
    "pubspec.yaml": "name: shop\n",
    # Sorts before `cart.dart`, so a name-only pick lands here first.
    "lib/a_toy/cart.dart": "class Cart {\n  int total() => 1;\n}\n",
    "lib/cart.dart": "class Cart {\n  int total() => 2;\n}\n",
    "lib/shop.dart": "import 'cart.dart';\n\nclass Shop {\n  Cart cart = Cart();\n}\n",
    "lib/main.dart": "import 'shop.dart';\n\nint run(Shop s) => s.cart.total();\n",
}


def test_a_dart_field_type_is_the_one_its_library_imports(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    for rel, text in _DART.items():
        (temp_repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (temp_repo / rel).write_text(text, encoding="utf-8")
    create_and_run_updater(temp_repo, mock_ingestor)

    assert _calls(temp_repo, mock_ingestor, "lib.main.run") == {
        ("lib.cart.Cart.total", cs.EdgeResolution.EXACT)
    }


def test_a_prefixed_dart_import_does_not_name_the_bare_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `import 'a_toy/toy_cart.dart' as toy;` makes that `Cart` visible only as
    # `toy.Cart`, so the bare `Cart` field is still `cart.dart`'s.
    files = {
        **_DART,
        "lib/shop.dart": (
            "import 'cart.dart';\nimport 'a_toy/toy_cart.dart' as toy;\n\n"
            "class Shop {\n  Cart cart = Cart();\n}\n"
        ),
        "lib/a_toy/toy_cart.dart": "class Cart {\n  int total() => 3;\n}\n",
    }
    for rel, text in files.items():
        (temp_repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (temp_repo / rel).write_text(text, encoding="utf-8")
    create_and_run_updater(temp_repo, mock_ingestor)

    assert _calls(temp_repo, mock_ingestor, "lib.main.run") == {
        ("lib.cart.Cart.total", cs.EdgeResolution.EXACT)
    }


def test_a_dart_name_two_visible_libraries_declare_is_not_picked_by_scope(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: with both `Cart`s imported bare, the declaring library
    # cannot say which one the field is, so the scoped lookup steps aside.
    files = {
        **_DART,
        "lib/shop.dart": (
            "import 'cart.dart';\nimport 'a_toy/toy_cart.dart';\n\n"
            "class Shop {\n  Cart cart = Cart();\n}\n"
        ),
        "lib/a_toy/toy_cart.dart": "class Cart {\n  int total() => 3;\n}\n",
    }
    for rel, text in files.items():
        (temp_repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (temp_repo / rel).write_text(text, encoding="utf-8")
    updater = create_and_run_updater(temp_repo, mock_ingestor)

    resolver = updater.factory.call_processor._resolver
    assert resolver._dart_visible_type("Cart", f"{temp_repo.name}.lib.shop") is None
