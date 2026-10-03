# Scala imports of the project's own packages, `new C()` and parameterless
# method selections (issue #2450).
#
# The layout is the one the issue indexed for Java and Scala side by side:
# `src/main/scala/shop/Cart.scala` declaring `package shop`, imported from
# `src/main/scala/app/App.scala`. Java bound the import to the project's own
# Module; Scala minted an ExternalModule named `shop`, and everything resolved
# through the import (the cross-package `Item("x")`) went with it.
#
# Each assertion names the exact target qn and label. A test that only counted
# IMPORTS edges would pass on the broken build, which emitted two of them --
# both to the phantom ExternalModule.
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import get_relationships, run_updater
from evals.cgr_graph import _StatefulIngestor

SKIP = "scala"
PROJECT = "scalaimp"
SRC = f"{PROJECT}.src.main.scala"

CART_SCALA = """package shop
class Cart { def total(): Double = 0; def size: Int = 0; val count: Int = 0 }
case class Item(name: String)
"""


@pytest.fixture
def project(temp_repo: Path) -> Path:
    root = temp_repo / PROJECT
    root.mkdir()
    return root


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _edges(mock_ingestor: MagicMock, rel_type: str) -> list[tuple[str, str, str]]:
    return [
        (str(c.args[0][2]), str(c.args[2][0]), str(c.args[2][2]))
        for c in get_relationships(mock_ingestor, rel_type)
    ]


def _targets_from(
    mock_ingestor: MagicMock, rel_type: str, source_qn: str
) -> set[tuple[str, str]]:
    return {
        (label, target)
        for source, label, target in _edges(mock_ingestor, rel_type)
        if source == source_qn
    }


def _index(root: Path, mock_ingestor: MagicMock, files: dict[str, str]) -> None:
    _write(root, files)
    run_updater(root, mock_ingestor, skip_if_missing=SKIP)


# --- imports of the project's own packages ---------------------------------


def test_a_selector_import_of_a_project_package_lands_on_its_module(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """`import shop.{Cart, Item}` targets the Module declaring package shop.

    Item lives in Cart.scala, so no file is named after it: the target has to
    come from the package declaration, not from probing `shop/Item.scala`.
    """
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/shop/Cart.scala": CART_SCALA,
            "src/main/scala/app/App.scala": """package app
import shop.{Cart, Item}
object App { def run(): Double = { val c = new Cart(); val i = Item("x"); c.total() } }
""",
        },
    )

    imports = _targets_from(mock_ingestor, "IMPORTS", f"{SRC}.app.App")

    assert imports == {("Module", f"{SRC}.shop.Cart")}, imports


def test_a_cross_package_case_class_construction_instantiates_the_class(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """`Item("x")` in package app INSTANTIATES shop's Item through the import.

    Inside package shop the same construction already resolved; across the
    package boundary the import named an external `shop.Item`, so the
    resolver treated the name as external and dropped it.
    """
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/shop/Cart.scala": CART_SCALA,
            "src/main/scala/app/App.scala": """package app
import shop.Item
object App { def run(): Item = Item("x") }
""",
        },
    )

    instantiates = _targets_from(
        mock_ingestor, "INSTANTIATES", f"{SRC}.app.App.App.run"
    )

    assert ("Class", f"{SRC}.shop.Cart.Item") in instantiates, instantiates


def test_a_package_not_mirroring_its_directory_still_resolves(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """The package clause decides, not the directory the file sits in.

    Scala does not tie packages to directories, and flattening the common
    prefix (`com/acme` omitted) is an accepted layout.
    """
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/flat/Models.scala": """package com.acme.shop
class Cart { def total(): Double = 0 }
""",
            "src/main/scala/app/App.scala": """package com.acme.app
import com.acme.shop.Cart
object App { def run(): Cart = new Cart() }
""",
        },
    )

    imports = _targets_from(mock_ingestor, "IMPORTS", f"{SRC}.app.App")
    instantiates = _targets_from(
        mock_ingestor, "INSTANTIATES", f"{SRC}.app.App.App.run"
    )

    assert imports == {("Module", f"{SRC}.flat.Models")}, imports
    assert ("Class", f"{SRC}.flat.Models.Cart") in instantiates, instantiates


def test_chained_package_clauses_make_an_import_relative(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """`package com.acme` then `package app` opens com.acme for imports.

    `import shop.Cart` there names `com.acme.shop.Cart`; reading it as the
    root-level package `shop` misses the project's own class.
    """
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/shop/Cart.scala": """package com.acme.shop
class Cart { def total(): Double = 0 }
""",
            "src/main/scala/app/App.scala": """package com.acme
package app
import shop.Cart
object App { def run(): Cart = new Cart() }
""",
        },
    )

    imports = _targets_from(mock_ingestor, "IMPORTS", f"{SRC}.app.App")

    assert imports == {("Module", f"{SRC}.shop.Cart")}, imports


def test_a_wildcard_import_targets_the_modules_the_file_uses(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """`import shop._` lands on the package's modules whose names are used.

    The package spans two files; App uses Cart only, so Pricing.scala gets no
    edge. Calls through the wildcard still bind to the project's class.
    """
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/shop/Cart.scala": CART_SCALA,
            "src/main/scala/shop/Pricing.scala": """package shop
object Pricing { def rate(): Double = 1 }
""",
            "src/main/scala/app/App.scala": """package app
import shop._
object App { def run(): Cart = new Cart() }
""",
        },
    )

    imports = _targets_from(mock_ingestor, "IMPORTS", f"{SRC}.app.App")
    instantiates = _targets_from(
        mock_ingestor, "INSTANTIATES", f"{SRC}.app.App.App.run"
    )

    assert imports == {("Module", f"{SRC}.shop.Cart")}, imports
    assert ("Class", f"{SRC}.shop.Cart.Cart") in instantiates, instantiates


def test_a_project_base_class_imported_across_packages_is_inherited(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """`class Special extends Cart` binds INHERITS to the imported class."""
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/shop/Cart.scala": CART_SCALA,
            "src/main/scala/app/Special.scala": """package app
import shop.Cart
class Special extends Cart
""",
        },
    )

    inherits = _targets_from(mock_ingestor, "INHERITS", f"{SRC}.app.Special.Special")

    assert ("Class", f"{SRC}.shop.Cart.Cart") in inherits, inherits


# --- `new C()` ----------------------------------------------------------------


def test_new_instantiates_the_class_in_the_same_file(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """`new Cart()`, `new Cart` and `new Box[Int](1)` all INSTANTIATE.

    instance_expression was not a call node, so no construction through
    `new` emitted anything, even beside the class it names.
    """
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/shop/Cart.scala": """package shop
class Cart { def total(): Double = 0 }
class Bag
class Box[T](v: T)
object Use { def a() = { val c = new Cart(); val b = new Bag; val x = new Box[Int](1); c } }
""",
        },
    )

    instantiates = _targets_from(
        mock_ingestor, "INSTANTIATES", f"{SRC}.shop.Cart.Use.a"
    )

    assert instantiates == {
        ("Class", f"{SRC}.shop.Cart.Cart"),
        ("Class", f"{SRC}.shop.Cart.Bag"),
        ("Class", f"{SRC}.shop.Cart.Box"),
    }, instantiates


def test_new_also_calls_the_auxiliary_constructors(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """A `def this(...)` runs on `new`, as a Java constructor does."""
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/shop/Cart.scala": """package shop
class Cart(n: Int) { def this() = this(0) }
object Use { def a(): Cart = new Cart() }
""",
        },
    )

    calls = _targets_from(mock_ingestor, "CALLS", f"{SRC}.shop.Cart.Use.a")

    assert ("Method", f"{SRC}.shop.Cart.Cart.this") in calls, calls


def test_new_reaches_a_path_qualified_or_same_package_class(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """`new shop.Cart()` needs no import; `new Cart()` in package shop none either.

    Neither spelling goes through the import map, so both are looked up in
    the packages the project declares.
    """
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/shop/Cart.scala": CART_SCALA,
            "src/main/scala/shop/Use.scala": """package shop
object Use { def a(): Int = { val c: Cart = new Cart(); c.size } }
""",
            "src/main/scala/app/App.scala": """package app
object App { def run(): Int = { val c = new shop.Cart(); c.size } }
""",
        },
    )

    for caller in (f"{SRC}.shop.Use.Use.a", f"{SRC}.app.App.App.run"):
        instantiates = _targets_from(mock_ingestor, "INSTANTIATES", caller)
        calls = _targets_from(mock_ingestor, "CALLS", caller)
        assert ("Class", f"{SRC}.shop.Cart.Cart") in instantiates, (
            caller,
            instantiates,
        )
        assert ("Method", f"{SRC}.shop.Cart.Cart.size") in calls, (caller, calls)


# --- parameterless method selections ------------------------------------------


def test_a_parameterless_selection_on_a_typed_receiver_calls_the_method(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """`c.size` and `println(c.total)` CALLS the method once c's type is known.

    Calling a parameterless def without parentheses is the idiomatic form, and
    the only legal one for a def declared without `()`.
    """
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/shop/Cart.scala": CART_SCALA,
            "src/main/scala/app/App.scala": """package app
import shop.Cart
object App {
  def run(): Int = { val c = new Cart(); println(c.total); c.size }
  def typed(c: Cart): Int = c.size
  def annotated(): Int = { val c: Cart = make(); c.size }
  def make(): Cart = new Cart()
}
""",
        },
    )

    run_calls = _targets_from(mock_ingestor, "CALLS", f"{SRC}.app.App.App.run")
    typed_calls = _targets_from(mock_ingestor, "CALLS", f"{SRC}.app.App.App.typed")
    annotated_calls = _targets_from(
        mock_ingestor, "CALLS", f"{SRC}.app.App.App.annotated"
    )

    assert ("Method", f"{SRC}.shop.Cart.Cart.size") in run_calls, run_calls
    assert ("Method", f"{SRC}.shop.Cart.Cart.total") in run_calls, run_calls
    assert ("Method", f"{SRC}.shop.Cart.Cart.size") in typed_calls, typed_calls
    assert ("Method", f"{SRC}.shop.Cart.Cart.size") in annotated_calls, annotated_calls


def test_a_selection_on_this_and_on_an_object_calls_the_member(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """`this.size` and `Registry.default` name their receiver's type outright."""
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/shop/Cart.scala": """package shop
class Cart { def size: Int = 0; def twice: Int = this.size * 2 }
object Registry { def default: Cart = new Cart() }
object Use { def a(): Cart = Registry.default }
""",
        },
    )

    twice_calls = _targets_from(mock_ingestor, "CALLS", f"{SRC}.shop.Cart.Cart.twice")
    use_calls = _targets_from(mock_ingestor, "CALLS", f"{SRC}.shop.Cart.Use.a")

    assert ("Method", f"{SRC}.shop.Cart.Cart.size") in twice_calls, twice_calls
    assert ("Method", f"{SRC}.shop.Cart.Registry.default") in use_calls, use_calls


# --- what must NOT change -----------------------------------------------------


def test_an_external_import_still_targets_an_external_module(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """A library import keeps its ExternalModule, even beside a namesake class.

    The project declares its own `List` in package shop; `java.util.List`
    must not be captured by it.
    """
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/shop/List.scala": """package shop
class List
""",
            "src/main/scala/app/App.scala": """package app
import java.util.List
import scala.collection.mutable.{Queue, Stack}
object App { def run(): Int = 1 }
""",
        },
    )

    imports = _targets_from(mock_ingestor, "IMPORTS", f"{SRC}.app.App")

    assert imports == {
        ("ExternalModule", "java.util"),
        ("ExternalModule", "scala.collection.mutable"),
    }, imports


def test_a_member_the_project_package_does_not_declare_stays_external(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """A name the project's package does not declare stays the library's.

    Scala lets a project add files to a package a library also fills, so
    `shop.Discount` with no `Discount` in the repo comes from elsewhere and
    must not be pinned to the project's module.
    """
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/shop/Cart.scala": CART_SCALA,
            "src/main/scala/app/App.scala": """package app
import shop.Discount
object App { def run(): Int = 1 }
""",
        },
    )

    imports = _targets_from(mock_ingestor, "IMPORTS", f"{SRC}.app.App")

    assert imports == {("ExternalModule", "shop")}, imports


def test_a_wildcard_import_of_the_files_own_package_is_no_self_import(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """`import shop._` inside package shop targets the OTHER files only."""
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/shop/Cart.scala": """package shop
import shop._
class Cart { def p(): Double = Pricing.rate() }
""",
            "src/main/scala/shop/Pricing.scala": """package shop
object Pricing { def rate(): Double = 1 }
""",
        },
    )

    imports = _targets_from(mock_ingestor, "IMPORTS", f"{SRC}.shop.Cart")

    assert imports == {("Module", f"{SRC}.shop.Pricing")}, imports


def test_a_selection_on_an_untyped_receiver_emits_no_call(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """With the receiver's type unknown, `x.size` binds nothing.

    A `val` read and a parameterless call are spelled the same, so a guess by
    the member's name alone would turn any `.size` into a CALLS edge to
    whichever class happens to define a `size` method.
    """
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/shop/Cart.scala": CART_SCALA
            + """object Use {
  def a(xs: Seq[Int]): Int = xs.size
  def b(): Int = { val x = load(); x.size }
  def load(): Seq[Int] = Seq(1)
}
""",
        },
    )

    a_calls = _targets_from(mock_ingestor, "CALLS", f"{SRC}.shop.Cart.Use.a")
    b_calls = _targets_from(mock_ingestor, "CALLS", f"{SRC}.shop.Cart.Use.b")

    assert ("Method", f"{SRC}.shop.Cart.Cart.size") not in a_calls, a_calls
    assert ("Method", f"{SRC}.shop.Cart.Cart.size") not in b_calls, b_calls


def test_a_selection_of_a_val_or_a_missing_member_emits_no_call(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """`c.count` reads a val; `c.weight` names nothing on Cart. No edges.

    The typed receiver decides: a same-named method on another class must
    not answer for a member Cart does not have.
    """
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/shop/Cart.scala": CART_SCALA
            + """class Crate { def weight: Int = 1; def count: Int = 2 }
object Use { def a(): Int = { val c = new Cart(); c.count + c.weight } }
""",
        },
    )

    calls = _targets_from(mock_ingestor, "CALLS", f"{SRC}.shop.Cart.Use.a")

    assert not {qn for _, qn in calls if qn.startswith(f"{SRC}.shop.Cart.Crate")}, calls


def test_a_call_with_parentheses_is_emitted_once(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """`c.total()` keeps one CALLS emission: its callee is not a second site."""
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/shop/Cart.scala": CART_SCALA
            + """object Use { def a(): Double = { val c = new Cart(); c.total() } }
""",
        },
    )

    emitted = [
        edge
        for edge in _edges(mock_ingestor, "CALLS")
        if edge[0] == f"{SRC}.shop.Cart.Use.a"
        and edge[2] == f"{SRC}.shop.Cart.Cart.total"
    ]

    assert len(emitted) == 1, emitted


# --- incremental runs -----------------------------------------------------------


def _stateful_index(store: _StatefulIngestor, root: Path, force: bool) -> None:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.SCALA not in parsers:
        pytest.skip("scala parser not available")
    GraphUpdater(ingestor=store, repo_path=root, parsers=parsers, queries=queries).run(
        force=force
    )


def _stateful_edges(
    store: _StatefulIngestor, rel_type: str
) -> set[tuple[str, str, str]]:
    return {
        (str(source), str(label), str(target))
        for (_fl, source, rel, label, target) in store.edges
        if rel == rel_type
    }


def test_an_incremental_run_keeps_imports_into_an_unchanged_provider(
    project: Path,
) -> None:
    """Editing only App.scala keeps its imports on shop's Module.

    A fresh updater parses only the changed file, so Cart.scala's package
    clause has to be recovered from disk; without it the import would fall
    back to the phantom ExternalModule a clean index never emits.
    """
    _write(
        project,
        {
            "src/main/scala/shop/Cart.scala": CART_SCALA,
            "src/main/scala/app/App.scala": """package app
import shop.{Cart, Item}
object App { def run(): Item = { val c = new Cart(); println(c.size); Item("x") } }
""",
        },
    )
    store = _StatefulIngestor()
    _stateful_index(store, project, force=True)
    clean_imports = _stateful_edges(store, "IMPORTS")

    app = project / "src/main/scala/app/App.scala"
    cache_mtime = (project / cs.HASH_CACHE_FILENAME).stat().st_mtime
    app.write_text(app.read_text() + "// touched\n")
    os.utime(app, (cache_mtime + 1, cache_mtime + 1))
    _stateful_index(store, project, force=False)

    run_qn = f"{SRC}.app.App.App.run"
    assert (
        _stateful_edges(store, "IMPORTS")
        == clean_imports
        == {(f"{SRC}.app.App", "Module", f"{SRC}.shop.Cart")}
    ), _stateful_edges(store, "IMPORTS")
    assert (run_qn, "Class", f"{SRC}.shop.Cart.Item") in _stateful_edges(
        store, "INSTANTIATES"
    )
    assert (run_qn, "Method", f"{SRC}.shop.Cart.Cart.size") in _stateful_edges(
        store, "CALLS"
    )


def test_package_blocks_declare_and_use_like_package_clauses(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """`package shop { ... }` declares its body's names; a body's uses count.

    The block form holds the whole file under the clause, so the scan must
    read the body for both what it declares and what it mentions.
    """
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/shop/Cart.scala": """package shop {
  class Cart { def total(): Double = 0 }
}
""",
            "src/main/scala/app/App.scala": """package app {
  import shop._
  object App { def run(): Cart = new Cart() }
}
""",
        },
    )

    imports = _targets_from(mock_ingestor, "IMPORTS", f"{SRC}.app.App")
    instantiates = _targets_from(
        mock_ingestor, "INSTANTIATES", f"{SRC}.app.App.App.run"
    )

    assert imports == {("Module", f"{SRC}.shop.Cart")}, imports
    assert ("Class", f"{SRC}.shop.Cart.Cart") in instantiates, instantiates
