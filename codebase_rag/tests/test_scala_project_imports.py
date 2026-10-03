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
from typing import cast
from unittest.mock import MagicMock

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.import_processor import ImportProcessor
from codebase_rag.parsers.scala import (
    scala_bindings,
    scala_selection,
    scan_scala_packages,
)
from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_nodes,
    get_relationships,
    run_updater,
)
from codebase_rag.types_defs import ScalaBinding
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


def test_new_without_a_declared_constructor_only_instantiates(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """`new Cart()` with no `def this(...)` INSTANTIATES and CALLS nothing.

    The primary constructor is the class body and has no node, so there is
    nothing to call; Java's `new Foo()` on a class declaring no constructor
    emits the same single INSTANTIATES.
    """
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/shop/Cart.scala": CART_SCALA
            + """object Use { def a(): Cart = new Cart() }
""",
        },
    )

    caller = f"{SRC}.shop.Cart.Use.a"

    assert _targets_from(mock_ingestor, "INSTANTIATES", caller) == {
        ("Class", f"{SRC}.shop.Cart.Cart")
    }
    assert _targets_from(mock_ingestor, "CALLS", caller) == set()


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


def test_an_inner_block_binding_hides_the_outer_one_only_inside_it(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """An inner `val c = new Crate()` does not untype the outer `c: Cart`.

    Each selection reads the binding in effect where it sits: `c.size`
    before the block is a Cart's, `c.weight` inside it a Crate's.
    """
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/shop/Cart.scala": CART_SCALA
            + """class Crate { def weight: Int = 1; def size: Int = 2 }
object Use {
  def run(): Int = {
    val c = new Cart()
    val n = c.size
    { val c = new Crate(); c.weight }
    n
  }
}
""",
        },
    )

    calls = _targets_from(mock_ingestor, "CALLS", f"{SRC}.shop.Cart.Use.run")

    assert {
        ("Method", f"{SRC}.shop.Cart.Cart.size"),
        ("Method", f"{SRC}.shop.Cart.Crate.weight"),
    } <= calls, calls
    assert ("Method", f"{SRC}.shop.Cart.Crate.size") not in calls, calls


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


def test_a_lambda_or_case_binder_hides_the_outer_typed_name(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """`items.map(c => c.size)` inside `def f(c: Cart)` is not Cart's size.

    The lambda's own `c` is untyped and hides the parameter in the lambda;
    a `case c: Crate` pattern types its `c` for its own clause only.
    """
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/shop/Cart.scala": CART_SCALA
            + """class Crate { def weight: Int = 1 }
object Use {
  def f(c: Cart, items: Seq[Item]): Seq[Int] = items.map(c => c.size)
  def g(c: Cart, x: Any): Int = x match { case c: Crate => c.weight }
}
""",
        },
    )

    f_calls = _targets_from(mock_ingestor, "CALLS", f"{SRC}.shop.Cart.Use.f")
    g_calls = _targets_from(mock_ingestor, "CALLS", f"{SRC}.shop.Cart.Use.g")

    assert ("Method", f"{SRC}.shop.Cart.Cart.size") not in f_calls, f_calls
    assert ("Method", f"{SRC}.shop.Cart.Crate.weight") in g_calls, g_calls


def test_a_same_scope_rebinding_to_another_type_stays_untyped(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """Two `c` bindings in ONE block with different types bind neither."""
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/shop/Cart.scala": CART_SCALA
            + """class Crate { def size: Int = 2 }
object Use { def run(): Int = { val c = new Cart(); val c = new Crate(); c.size } }
""",
        },
    )

    calls = _targets_from(mock_ingestor, "CALLS", f"{SRC}.shop.Cart.Use.run")

    assert not {qn for _, qn in calls if qn.endswith(".size")}, calls


def test_a_selector_import_splitting_a_package_keeps_the_library_edge(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """`import shop.{Cart, Discount}`: Cart is the project's, Discount is not.

    The project's name lands on its Module and the undeclared one keeps the
    library's ExternalModule, as Java's `import shop.Cart; import
    shop.Discount;` does for a split package.
    """
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/shop/Cart.scala": CART_SCALA,
            "src/main/scala/app/App.scala": """package app
import shop.{Cart, Discount}
object App { def run(): Cart = new Cart() }
""",
        },
    )

    imports = _targets_from(mock_ingestor, "IMPORTS", f"{SRC}.app.App")

    assert imports == {
        ("Module", f"{SRC}.shop.Cart"),
        ("ExternalModule", "shop"),
    }, imports


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


def test_each_package_block_resolves_its_imports_from_its_own_package(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """Sibling `package left { ... }` and `package right { ... }` blocks each
    resolve a relative import against their OWN package.

    Under `package outer`, `import model.Item` in block `left` names
    `outer.left.model.Item`. Read against the file's top-level package
    alone it named the decoy `outer.model.Item` instead.
    """
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/left/model/Item.scala": """package outer.left.model
class Item
""",
            "src/main/scala/right/model/Part.scala": """package outer.right.model
class Part
""",
            "src/main/scala/model/Decoy.scala": """package outer.model
class Item
class Part
""",
            "src/main/scala/app/Multi.scala": """package outer
package left {
  import model.Item
  object UseLeft { def make(): Item = new Item() }
}
package right {
  import model.Part
  object UseRight { def make(): Part = new Part() }
}
""",
        },
    )

    imports = _targets_from(mock_ingestor, "IMPORTS", f"{SRC}.app.Multi")
    left = _targets_from(mock_ingestor, "INSTANTIATES", f"{SRC}.app.Multi.UseLeft.make")
    right = _targets_from(
        mock_ingestor, "INSTANTIATES", f"{SRC}.app.Multi.UseRight.make"
    )

    assert imports == {
        ("Module", f"{SRC}.left.model.Item"),
        ("Module", f"{SRC}.right.model.Part"),
    }, imports
    assert left == {("Class", f"{SRC}.left.model.Item.Item")}, left
    assert right == {("Class", f"{SRC}.right.model.Part.Part")}, right


def test_an_import_inside_a_package_block_is_relative_to_that_package(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """`package a { import b.C }` names `a.b.C`, as `package a; import b.C` does.

    The block opens `a` for its body just as the bodiless clause opens it for
    the rest of the file; only the bodiless form used to count.
    """
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/a/b/C.scala": """package a.b
class C
""",
            "src/main/scala/app/Flat.scala": """package a
import b.C
object UseFlat { def make(): C = new C() }
""",
            "src/main/scala/app/Block.scala": """package a {
  import b.C
  object UseBlock { def make(): C = new C() }
}
""",
        },
    )

    for importer in (f"{SRC}.app.Flat", f"{SRC}.app.Block"):
        imports = _targets_from(mock_ingestor, "IMPORTS", importer)
        assert imports == {("Module", f"{SRC}.a.b.C")}, (importer, imports)


def test_sibling_blocks_importing_the_same_relative_path_keep_their_own_class(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """`import model.Item` in `package a { }` and in `package b { }` are two
    imports of two classes, `a.model.Item` and `b.model.Item`.

    One file-level map per name kept only the second, so the first block's
    `new Item()` landed on b's class and a's IMPORTS edge was lost.
    """
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/a/model/Item.scala": """package a.model
class Item
""",
            "src/main/scala/b/model/Item.scala": """package b.model
class Item
""",
            "src/main/scala/app/Pair.scala": """package a {
  import model.Item
  object UseA { def make(): Item = new Item() }
}
package b {
  import model.Item
  object UseB { def make(): Item = new Item() }
}
""",
        },
    )

    imports = _targets_from(mock_ingestor, "IMPORTS", f"{SRC}.app.Pair")
    use_a = _targets_from(mock_ingestor, "INSTANTIATES", f"{SRC}.app.Pair.UseA.make")
    use_b = _targets_from(mock_ingestor, "INSTANTIATES", f"{SRC}.app.Pair.UseB.make")

    assert imports == {
        ("Module", f"{SRC}.a.model.Item"),
        ("Module", f"{SRC}.b.model.Item"),
    }, imports
    assert use_a == {("Class", f"{SRC}.a.model.Item.Item")}, use_a
    assert use_b == {("Class", f"{SRC}.b.model.Item.Item")}, use_b


def test_sibling_blocks_binding_one_name_to_two_classes_resolve_apart(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """`import a.Item` in `left` and `import b.Item` in `right`: every use in
    each block (construction, apply, base class, typed receiver) reaches
    that block's own Item.
    """
    item = """case class Item(n: Int) { def size: Int = n }
"""
    block = """  package {side} {{
    import {pkg}.Item
    class Sub{suffix} extends Item(0)
    object Use{suffix} {{
      def make(): Item = new Item(1)
      def apply(): Item = Item(2)
      def size(i: Item): Int = i.size
    }}
  }}
"""
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/a/Item.scala": "package a\n" + item,
            "src/main/scala/b/Item.scala": "package b\n" + item,
            "src/main/scala/app/Sides.scala": "package outer {\n"
            + block.format(side="left", pkg="a", suffix="L")
            + block.format(side="right", pkg="b", suffix="R")
            + "}\n",
        },
    )

    imports = _targets_from(mock_ingestor, "IMPORTS", f"{SRC}.app.Sides")
    assert imports == {
        ("Module", f"{SRC}.a.Item"),
        ("Module", f"{SRC}.b.Item"),
    }, imports
    for suffix, pkg in (("L", "a"), ("R", "b")):
        item_qn = f"{SRC}.{pkg}.Item.Item"
        use = f"{SRC}.app.Sides.Use{suffix}"
        assert _targets_from(mock_ingestor, "INSTANTIATES", f"{use}.make") == {
            ("Class", item_qn)
        }
        assert _targets_from(mock_ingestor, "INSTANTIATES", f"{use}.apply") == {
            ("Class", item_qn)
        }
        assert _targets_from(mock_ingestor, "CALLS", f"{use}.size") == {
            ("Method", f"{item_qn}.size")
        }
        assert _targets_from(
            mock_ingestor, "INHERITS", f"{SRC}.app.Sides.Sub{suffix}"
        ) == {("Class", item_qn)}


def test_a_block_import_does_not_leak_into_its_sibling_block(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """A name only `left` imports is not bound in `right` or at file level.

    `right` writes `i: Item` with nothing in scope naming it, so its typed
    selection binds nothing, as before this change.
    """
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/a/Item.scala": """package a
class Item { def size: Int = 1 }
""",
            "src/main/scala/app/Sides.scala": """package outer {
  package left {
    import a.Item
    object UseL { def size(i: Item): Int = i.size }
  }
  package right {
    object UseR { def size(i: Item): Int = i.size }
  }
}
""",
        },
    )

    left = _targets_from(mock_ingestor, "CALLS", f"{SRC}.app.Sides.UseL.size")
    right = _targets_from(mock_ingestor, "CALLS", f"{SRC}.app.Sides.UseR.size")
    imports = _targets_from(mock_ingestor, "IMPORTS", f"{SRC}.app.Sides")

    assert left == {("Method", f"{SRC}.a.Item.Item.size")}, left
    assert right == set(), right
    assert imports == {("Module", f"{SRC}.a.Item")}, imports


def test_a_root_anchored_import_skips_the_enclosing_packages(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """`_root_.shop.Cart` names the root package `shop`, not `com.acme.shop`.

    The chained clauses open `com.acme`, where a relative `shop.Cart` would
    land; `_root_` exists to say the opposite.
    """
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/shop/Cart.scala": CART_SCALA,
            "src/main/scala/acme/Cart.scala": """package com.acme.shop
class Cart
""",
            "src/main/scala/app/App.scala": """package com.acme
package app
import _root_.shop.Cart
object App { def run(): Cart = new Cart() }
""",
        },
    )

    imports = _targets_from(mock_ingestor, "IMPORTS", f"{SRC}.app.App")

    assert imports == {("Module", f"{SRC}.shop.Cart")}, imports


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


# --- the shapes the scans and lookups skip ----------------------------------------


def _parse_scala(source: str) -> Node:
    parsers, _queries = load_parsers()
    if cs.SupportedLanguage.SCALA not in parsers:
        pytest.skip("scala parser not available")
    return parsers[cs.SupportedLanguage.SCALA].parse(source.encode()).root_node


def _nodes_of(root: Node, node_type: str) -> list[Node]:
    found: list[Node] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == node_type:
            found.append(node)
        stack.extend(reversed(node.children))
    return found


def test_case_and_for_binders_are_untyped_and_scoped_to_their_clause() -> None:
    """`case c =>` binds c in its clause, `for (c <- xs)` in its for.

    Neither binder says a type, so each only hides an outer `c` there.
    """
    root = _parse_scala(
        "object O { def f(): Unit = "
        "{ xs.foreach { case c => c.size }; for (c <- xs) yield c.size } }"
    )
    (case_clause,) = _nodes_of(root, cs.TS_SCALA_CASE_CLAUSE)
    (for_expression,) = _nodes_of(root, cs.TS_SCALA_FOR_EXPRESSION)

    bindings = scala_bindings(root)

    assert set(bindings) == {"c"}
    assert sorted(bindings["c"]) == sorted(
        [
            ScalaBinding("c", None, case_clause.start_byte, case_clause.end_byte),
            ScalaBinding("c", None, for_expression.start_byte, for_expression.end_byte),
        ]
    )


def test_destructuring_binders_bind_no_name_of_their_own() -> None:
    """Tuple and wildcard patterns bind no single name to scope.

    `val (p, q) = ...`, `case _: Cart`, `for ((k, v) <- m)` and the
    `(a, b) =>` parameter list are not one name; only the lambda's own
    parameters bind, each scoped to the lambda.
    """
    root = _parse_scala(
        "object O { def f(): Unit = { val (p, q) = (1, 2); xs.map((a, b) => a); "
        "xs.collect { case _: Cart => 1 }; for ((k, v) <- m) yield k } }"
    )
    (lambda_expression,) = _nodes_of(root, cs.TS_SCALA_LAMBDA_EXPRESSION)
    scope = (lambda_expression.start_byte, lambda_expression.end_byte)

    bindings = scala_bindings(root)

    assert bindings == {
        "a": [ScalaBinding("a", None, *scope)],
        "b": [ScalaBinding("b", None, *scope)],
    }


def test_a_package_object_declares_its_members_in_its_own_package() -> None:
    """`package object util` inside `package shop` holds shop.util's members.

    A package object with no body declares nothing at all.
    """
    with_body = _parse_scala(
        "package shop\npackage object util { def helper(): Int = 1; val rate = 2 }"
    )
    bodiless = _parse_scala("package shop\npackage object util")

    assert scan_scala_packages(with_body).packages == {
        "shop": frozenset(),
        "shop.util": frozenset({"helper", "rate"}),
    }
    assert scan_scala_packages(bodiless).packages == {"shop": frozenset()}


def test_a_package_clause_missing_its_name_opens_no_package() -> None:
    """`package ;` parses with an empty name, so A stays in the root package."""
    root = _parse_scala("package ;\nclass A")

    assert scan_scala_packages(root).packages == {"": frozenset({"A"})}


@pytest.mark.parametrize(
    ("statement", "selections"),
    [
        ("g(c.size)", [("c", "size")]),
        ("x = c.size", [("c", "size")]),
        ("c.size = 3", [None]),
        ("f().size", [None]),
        ("a.b.size", [None, ("a", "b")]),
    ],
)
def test_a_selection_is_a_read_only_on_a_plain_name_and_off_the_assigned_side(
    statement: str, selections: list[tuple[str, str] | None]
) -> None:
    """An argument or a right-hand side reads; an assignment target does not.

    A receiver that is itself a call or a selection names no binding, while
    the inner `a.b` of `a.b.size` still reads `b` from `a`.
    """
    root = _parse_scala(f"object O {{ def f(): Unit = {{ {statement} }} }}")

    found = [
        scala_selection(node) for node in _nodes_of(root, cs.TS_SCALA_FIELD_EXPRESSION)
    ]

    assert found == selections


class _CallOf:
    """A call or generic-function parent whose callee is some other node."""

    def __init__(self, node_type: str, callee: Node) -> None:
        self.type = node_type
        self._callee = callee

    def child_by_field_name(self, name: str) -> Node | None:
        return self._callee if name == cs.TS_FIELD_FUNCTION else None


class _Reparented:
    """A real selection node seen under a parent the grammar never gives it."""

    def __init__(self, node: Node, parent: _CallOf | None) -> None:
        self.id = node.id
        self.parent = parent
        self.child_by_field_name = node.child_by_field_name


@pytest.mark.parametrize(
    "parent_type",
    [None, cs.TS_SCALA_CALL_EXPRESSION, cs.TS_SCALA_GENERIC_FUNCTION],
)
def test_a_selection_that_is_no_callee_reads_its_receiver(
    parent_type: str | None,
) -> None:
    """With no parent, or under a call it is not the callee of, `c.size` reads.

    The grammar always puts a call's arguments under an `arguments` node, so
    neither parent comes from a parse.
    """
    root = _parse_scala("object O { def f(): Int = g(c.size) }")
    (selection,) = _nodes_of(root, cs.TS_SCALA_FIELD_EXPRESSION)
    (call,) = _nodes_of(root, cs.TS_SCALA_CALL_EXPRESSION)
    callee = call.child_by_field_name(cs.TS_FIELD_FUNCTION)
    assert callee is not None
    parent = None if parent_type is None else _CallOf(parent_type, callee)

    reparented = cast(Node, _Reparented(selection, parent))

    assert scala_selection(reparented) == ("c", "size")


def test_an_external_wildcard_before_a_project_one_does_not_hide_the_class(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """`import scala.collection.mutable._` first, then `import shop._`.

    The library wildcard names no project class, so the lookup moves on to
    the next wildcard and `new Cart()` still lands on shop's Cart.
    """
    _index(
        project,
        mock_ingestor,
        {
            "src/main/scala/shop/Cart.scala": CART_SCALA,
            "src/main/scala/app/App.scala": """package app
import scala.collection.mutable._
import shop._
object App { def run(): Cart = new Cart() }
""",
        },
    )

    instantiates = _targets_from(
        mock_ingestor, "INSTANTIATES", f"{SRC}.app.App.App.run"
    )

    assert instantiates == {("Class", f"{SRC}.shop.Cart.Cart")}, instantiates


def test_a_type_lookup_without_a_position_or_a_scan_uses_what_is_known(
    project: Path, mock_ingestor: MagicMock
) -> None:
    """No position means no package block: the file's imports answer.

    A module never scanned has no packages and no imports, so nothing does.
    """
    _write(
        project,
        {
            "src/main/scala/shop/Cart.scala": CART_SCALA,
            "src/main/scala/app/App.scala": """package app
import shop.Cart
object App { def run(): Cart = new Cart() }
""",
        },
    )
    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing=SKIP)
    processor = updater.factory.import_processor

    assert processor.scala_type_qn(f"{SRC}.app.App", "Cart") == (
        f"{SRC}.shop.Cart.Cart"
    )
    assert processor.scala_type_qn(f"{SRC}.app.App", "Missing") is None
    assert processor.scala_type_qn(f"{SRC}.nowhere.Gone", "Cart", 0) is None


def test_a_resolved_scala_edge_to_a_module_no_longer_known_is_dropped(
    tmp_path: Path, mock_ingestor: MagicMock
) -> None:
    """An edge resolved onto the project's module never goes external.

    When that module is not among the known modules at flush time there is
    no Module to land on, and no ExternalModule stands in for it.
    """
    importer = "proj.app.App"
    gone = "proj.shop.Cart"
    processor = ImportProcessor(
        repo_path=tmp_path, project_name="proj", ingestor=mock_ingestor
    )
    processor.defer_import_edge(importer, gone, cs.SupportedLanguage.SCALA)
    processor._scala_resolved_edges.add((importer, gone))

    emitted = processor.flush_deferred_import_edges({importer: ""})

    assert emitted == 0
    assert get_relationships(mock_ingestor, "IMPORTS") == []
    assert get_nodes(mock_ingestor, cs.NodeLabel.EXTERNAL_MODULE) == []
