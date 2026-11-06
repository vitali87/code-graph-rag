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


def test_a_struct_in_an_inline_mod_types_its_field_through_the_mods_use(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `garage`'s own `use` names the field's `Engine`, not the file's.
    lib = (
        "mod a_toy;\nmod motor;\n\nuse a_toy::Engine;\n\n"
        "mod garage {\n    use crate::motor::Engine;\n\n"
        "    pub struct Car {\n        pub engine: Engine,\n    }\n}\n\n"
        "use garage::Car;\n\n"
        "pub fn run(c: &Car) -> u8 {\n    c.engine.start()\n}\n\n"
        "pub fn toy(e: &Engine) -> u8 {\n    e.start()\n}\n"
    )
    _index(temp_repo, mock_ingestor, {"src/lib.rs": lib})

    assert _calls(temp_repo, mock_ingestor, "src.lib.run") == {
        ("src.motor.Engine.start", cs.EdgeResolution.EXACT)
    }
    # Negative: the file's own `Engine` is still `a_toy`'s.
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


@pytest.mark.parametrize(
    "imports",
    [
        "import 'cart.dart';\nimport 'cart.dart' as p;\n",
        "import 'cart.dart' as p;\nimport 'cart.dart';\n",
    ],
    ids=["bare-first", "prefixed-first"],
)
def test_a_dart_library_imported_bare_and_prefixed_still_names_the_bare_type(
    temp_repo: Path, mock_ingestor: MagicMock, imports: str
) -> None:
    # The bare import keeps `Cart` visible whatever the prefixed one adds.
    files = {
        **_DART,
        "lib/shop.dart": f"{imports}\nclass Shop {{\n  Cart cart = Cart();\n}}\n",
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


def test_a_dart_field_typed_from_a_package_takes_no_project_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: `Cart` is the package's, and no project library `shop.dart`
    # sees declares one, so the project's unimported `Cart` is not it: the
    # name-only fallback bound `s.cart.total()` to it, `exact`.
    files = {
        "lib/main.dart": _DART["lib/main.dart"],
        "lib/shop.dart": (
            "import 'package:other/cart.dart';\n\n"
            "class Shop {\n  Cart cart = Cart();\n}\n"
        ),
        "lib/a_toy/toy_cart.dart": "class Cart {\n  int total() => 3;\n}\n",
    }
    for rel, text in files.items():
        (temp_repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (temp_repo / rel).write_text(text, encoding="utf-8")
    create_and_run_updater(temp_repo, mock_ingestor)

    assert ("lib.a_toy.toy_cart.Cart.total", cs.EdgeResolution.EXACT) not in _calls(
        temp_repo, mock_ingestor, "lib.main.run"
    )


def _index_dart(
    repo: Path, mock: MagicMock, shop: str, main: str = _DART["lib/main.dart"]
) -> set[tuple[str, str]]:
    files = {
        **_DART,
        "lib/main.dart": main,
        "lib/shop.dart": f"{shop}\nclass Shop {{\n  Cart cart = Cart();\n}}\n",
    }
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    create_and_run_updater(repo, mock)
    return _calls(repo, mock, "lib.main.run")


def test_a_dart_library_s_own_class_shadows_the_one_it_imports(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `shop.dart`'s own `Cart` is the bare name there, whatever `cart.dart`
    # brings in: counting both as candidates left the hop untyped, and with
    # `cart.dart` imported beside it the caller has no unique `total()` either.
    shop = "import 'cart.dart';\n\nclass Cart {\n  int total() => 4;\n}\n"
    main = f"import 'cart.dart';\n{_DART['lib/main.dart']}"

    assert _index_dart(temp_repo, mock_ingestor, shop, main) == {
        ("lib.shop.Cart.total", cs.EdgeResolution.EXACT)
    }


@pytest.mark.parametrize(
    "shop",
    [
        "import 'cart.dart';\nimport 'a_toy/cart.dart' hide Cart;\n",
        "import 'cart.dart';\nimport 'a_toy/cart.dart' show Other;\n",
        "import 'cart.dart' show Cart;\nimport 'a_toy/cart.dart' show Cart hide Cart;\n",
        "import 'cart.dart';\nexport 'a_toy/cart.dart';\n",
    ],
    ids=["hide", "show-other", "show-then-hide", "export"],
)
def test_a_dart_library_kept_out_of_scope_does_not_contest_the_bare_type(
    temp_repo: Path, mock_ingestor: MagicMock, shop: str
) -> None:
    # `a_toy/cart.dart`'s `Cart` never reaches `shop.dart`'s scope: a
    # `show` / `hide` list keeps it out, and an `export` hands it to
    # `shop.dart`'s importers without bringing it in.
    assert _index_dart(temp_repo, mock_ingestor, shop) == {
        ("lib.cart.Cart.total", cs.EdgeResolution.EXACT)
    }


@pytest.mark.parametrize(
    "directive",
    [
        "import 'cart.dart' hide Cart;",
        "import 'cart.dart' show Other;",
        "export 'cart.dart';",
    ],
    ids=["hide", "show-other", "export"],
)
def test_a_dart_field_typed_from_a_package_takes_no_out_of_scope_class(
    temp_repo: Path, mock_ingestor: MagicMock, directive: str
) -> None:
    # Negative: `Cart` is the package's, and `cart.dart`'s is out of
    # `shop.dart`'s scope, so the one project `Cart` the directives name is
    # not the field's type.
    shop = f"import 'package:other/cart.dart';\n{directive}\n"

    assert ("lib.cart.Cart.total", cs.EdgeResolution.EXACT) not in _index_dart(
        temp_repo, mock_ingestor, shop
    )


@pytest.mark.parametrize(
    ("shop", "part_of", "main", "target"),
    [
        (
            "import 'cart.dart';\n\npart 'shop_model.dart';\n",
            "part of 'shop.dart';",
            _DART["lib/main.dart"],
            "lib.cart.Cart.total",
        ),
        (
            "import 'cart.dart';\n\npart 'shop_model.dart';\n\n"
            "class Cart {\n  int total() => 4;\n}\n",
            "part of 'shop.dart';",
            f"import 'cart.dart';\n{_DART['lib/main.dart']}",
            "lib.shop.Cart.total",
        ),
        (
            "library shop;\n\nimport 'cart.dart';\n\npart 'shop_model.dart';\n",
            "part of shop;",
            _DART["lib/main.dart"],
            "lib.cart.Cart.total",
        ),
    ],
    ids=["library-import", "library-own-class", "part-of-library-name"],
)
def test_a_dart_field_declared_in_a_part_is_typed_in_its_library_s_scope(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    shop: str,
    part_of: str,
    main: str,
    target: str,
) -> None:
    # `shop_model.dart` is part of `shop.dart`'s library, so its `Cart` is
    # written in that library's scope: `shop.dart`'s imports and its own
    # classes. Read as a file of its own the part imports nothing, and the
    # `cart.dart` field was left untyped. `part of shop;` names the library
    # by its name, not a file, so the link is `shop.dart`'s `part` directive.
    files = {
        **_DART,
        "lib/main.dart": main,
        "lib/shop.dart": shop,
        "lib/shop_model.dart": f"{part_of}\n\nclass Shop {{\n  Cart cart = Cart();\n}}\n",
    }
    for rel, text in files.items():
        (temp_repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (temp_repo / rel).write_text(text, encoding="utf-8")
    create_and_run_updater(temp_repo, mock_ingestor)

    assert _calls(temp_repo, mock_ingestor, "lib.main.run") == {
        (target, cs.EdgeResolution.EXACT)
    }


@pytest.mark.parametrize(
    "shop_in",
    ["lib/shop_model.dart", "lib/shop.dart"],
    ids=["sibling-part", "defining-file"],
)
def test_a_dart_class_declared_in_a_part_shadows_the_one_its_library_imports(
    temp_repo: Path, mock_ingestor: MagicMock, shop_in: str
) -> None:
    # `shop.dart` and its parts are one library, so `shop_cart.dart`'s `Cart`
    # is the library's own and shadows `cart.dart`'s wherever `Shop` sits:
    # only the declaring file and `shop.dart` were checked, so both `Cart`s
    # contested the bare name, the hop was left untyped and `run`'s own
    # `cart.dart` import bound `total()` to that `Cart`.
    files = {
        **_DART,
        "lib/main.dart": f"import 'cart.dart';\n{_DART['lib/main.dart']}",
        "lib/shop.dart": (
            "import 'cart.dart';\n\npart 'shop_model.dart';\npart 'shop_cart.dart';\n"
        ),
        "lib/shop_model.dart": "part of 'shop.dart';\n",
        "lib/shop_cart.dart": (
            "part of 'shop.dart';\n\nclass Cart {\n  int total() => 5;\n}\n"
        ),
    }
    files[shop_in] += "\nclass Shop {\n  Cart cart = Cart();\n}\n"
    for rel, text in files.items():
        (temp_repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (temp_repo / rel).write_text(text, encoding="utf-8")
    create_and_run_updater(temp_repo, mock_ingestor)

    assert _calls(temp_repo, mock_ingestor, "lib.main.run") == {
        ("lib.shop_cart.Cart.total", cs.EdgeResolution.EXACT)
    }
