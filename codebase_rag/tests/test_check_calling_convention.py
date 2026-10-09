"""The structural delta reports a change to how a definition is called.

Adding `@property` to a method, dropping `@staticmethod`, a TypeScript
`get` or `static`, a Java `static` or a narrower visibility breaks every
caller written the old way, while the body and the parameters stay as they
were. The delta read neither decorators nor modifiers, so `cgr check
--fail-on-found` reported `changed: []` and exit 0 (issue #3259).
"""

from __future__ import annotations

import copy
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_check import run_check
from codebase_rag.structural_delta import (
    CallSite,
    ConventionChange,
    Definition,
    Snapshot,
    StructuralDelta,
    _changed,
    _convention,
    _convention_verdict,
    _site_form,
    _SiteForm,
    _SourceTrees,
    has_findings,
    observe,
    snapshot,
)
from evals.cgr_graph import _StatefulIngestor

PROJECT = "cc"

_TSC = shutil.which("tsc")
_JAVAC = shutil.which("javac")
needs_tsc = pytest.mark.skipif(_TSC is None, reason="tsc is not installed")
needs_javac = pytest.mark.skipif(_JAVAC is None, reason="javac is not installed")

CART_PY = (
    "class Cart:\n"
    "    def __init__(self):\n        self.items = [1, 2]\n\n"
    "    def total(self):\n        return sum(self.items)\n\n"
    "    @staticmethod\n    def tax(x):\n        return x * 2\n\n"
    "    @classmethod\n    def make(cls):\n        return 'made'\n"
)
SHOP_PY = (
    "from cart import Cart\n\n\n"
    "def subtotal():\n    return Cart().total()\n\n\n"
    "def vat():\n    return Cart().tax(3)\n\n\n"
    "def vat_through_class():\n    return Cart.tax(4)\n\n\n"
    "def fresh():\n    return Cart.make()\n\n\n"
    "def fresh_from_instance():\n    return Cart().make()\n"
)


def _index(root: Path, files: dict[str, str]) -> tuple[_StatefulIngestor, GraphUpdater]:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)
    return store, updater


def _edit(root: Path, files: dict[str, str], rel: str, edited: str) -> StructuralDelta:
    store, updater = _index(root, files)
    (root / rel).write_text(edited, encoding="utf-8")
    return observe(
        store.fetch_all,
        PROJECT,
        [rel],
        lambda: updater.reingest([rel]),
        repo_root=root,
    )


def _verdicts(delta: StructuralDelta, name: str) -> dict[str, str]:
    change = _change(delta, name)
    return {
        site["caller"].rsplit(".", 1)[-1]: site["verdict"] for site in change["sites"]
    }


def _change(delta: StructuralDelta, name: str) -> ConventionChange:
    (change,) = [
        c
        for c in delta["convention_changes"]
        if c["qualified_name"].partition("(")[0].endswith(f".{name}")
    ]
    return change


def _python(root: Path, *calls: str) -> list[str]:
    errors = []
    for call in calls:
        result = subprocess.run(
            [sys.executable, "-B", "-c", f"import shop; shop.{call}()"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding=cs.ENCODING_UTF8,
            check=False,
        )
        errors.append(result.stderr.strip().splitlines()[-1] if result.stderr else "")
    return errors


# --- Python ------------------------------------------------------------------


def test_the_issue_repro_is_changed_and_breaks_its_callers(temp_repo: Path) -> None:
    edited = CART_PY.replace(
        "    def total(self):", "    @property\n    def total(self):"
    ).replace("    @staticmethod\n", "")

    delta = _edit(
        temp_repo, {"cart.py": CART_PY, "shop.py": SHOP_PY}, "cart.py", edited
    )

    assert delta["symbols"]["changed"] == [
        f"{PROJECT}.cart.Cart.tax",
        f"{PROJECT}.cart.Cart.total",
    ]
    total = _change(delta, "total")
    assert total["changes"] == [cs.DELTA_CONVENTION_BECAME_ACCESSOR]
    assert total["before"] == [] and total["after"] == ["@property"]
    # One site per call, though the graph drew it before and after the edit.
    assert len(total["sites"]) == 1
    assert _verdicts(delta, "total") == {"subtotal": cs.DELTA_CONVENTION_CALLS_ACCESSOR}
    assert _change(delta, "tax")["changes"] == [cs.DELTA_CONVENTION_NO_LONGER_STATIC]
    # `Cart().tax(3)` now binds the instance to `x`; `Cart.tax(4)` still
    # reaches the plain function and passes it 4.
    assert _verdicts(delta, "tax") == {
        "vat": cs.DELTA_CONVENTION_REBOUND,
        "vat_through_class": cs.DELTA_ARITY_OK,
    }
    assert has_findings(delta)
    # What Python itself says about the edited tree.
    subtotal, vat, through_class = _python(
        temp_repo, "subtotal", "vat", "vat_through_class"
    )
    assert "'int' object is not callable" in subtotal
    assert "takes 1 positional argument but 2 were given" in vat
    assert through_class == ""


def test_a_classmethod_made_an_instance_method_breaks_its_class_calls(
    temp_repo: Path,
) -> None:
    edited = CART_PY.replace("    @classmethod\n", "")

    delta = _edit(
        temp_repo, {"cart.py": CART_PY, "shop.py": SHOP_PY}, "cart.py", edited
    )

    assert _change(delta, "make")["changes"] == [
        cs.DELTA_CONVENTION_NO_LONGER_CLASSMETHOD
    ]
    # Negative: through an instance the receiver is still bound.
    assert _verdicts(delta, "make") == {
        "fresh": cs.DELTA_CONVENTION_REBOUND,
        "fresh_from_instance": cs.DELTA_ARITY_OK,
    }
    assert has_findings(delta)
    fresh, from_instance = _python(temp_repo, "fresh", "fresh_from_instance")
    assert "missing 1 required positional argument" in fresh
    assert from_instance == ""


def test_a_property_made_a_method_breaks_its_reads(temp_repo: Path) -> None:
    cart = CART_PY.replace(
        "    def total(self):", "    @property\n    def total(self):"
    )
    shop = SHOP_PY.replace("Cart().total()", "Cart().total + 1")

    delta = _edit(temp_repo, {"cart.py": cart, "shop.py": shop}, "cart.py", CART_PY)

    assert _change(delta, "total")["changes"] == [
        cs.DELTA_CONVENTION_NO_LONGER_ACCESSOR
    ]
    assert _verdicts(delta, "total") == {"subtotal": cs.DELTA_CONVENTION_READS_METHOD}
    assert has_findings(delta)


def test_a_static_method_that_drops_its_self_keeps_its_callers(
    temp_repo: Path,
) -> None:
    # Negative: `def tax(self, x)` becoming `@staticmethod def tax(x)` is
    # the refactor that keeps `Cart().tax(3)` passing 3 as `x`.
    cart = CART_PY.replace(
        "    @staticmethod\n    def tax(x):", "    def tax(self, x):"
    )

    delta = _edit(temp_repo, {"cart.py": cart, "shop.py": SHOP_PY}, "cart.py", CART_PY)

    assert f"{PROJECT}.cart.Cart.tax" in delta["symbols"]["changed"]
    assert _verdicts(delta, "tax")["vat"] == cs.DELTA_ARITY_OK
    assert not has_findings(delta)


@pytest.mark.parametrize(
    "decorate",
    [
        pytest.param(
            lambda text: text.replace(
                "    def total(self):",
                "    @functools.lru_cache(maxsize=None)\n    def total(self):",
            ).replace("class Cart:", "import functools\n\n\nclass Cart:"),
            id="cache-decorator",
        ),
        pytest.param(
            lambda text: text.replace(
                "        return x * 2", "        return x * 2  # vat"
            ),
            id="comment-only",
        ),
    ],
)
def test_a_change_that_keeps_the_convention_is_no_finding(
    temp_repo: Path, decorate: Callable[[str], str]
) -> None:
    # Negative: a decorator that leaves the call form alone, or a comment.
    delta = _edit(
        temp_repo,
        {"cart.py": CART_PY, "shop.py": SHOP_PY},
        "cart.py",
        decorate(CART_PY),
    )

    assert delta["convention_changes"] == []
    assert not has_findings(delta)


READER_PY = """class Cart:
    def tax(x):
        return x * 2

    def own(self):
        return self.tax(3)

    @classmethod
    def through_cls(cls):
        return cls.tax(4)


def factory():
    return Cart


def through_factory():
    return factory().tax(5)


def chained():
    return Cart().tax(6).tax(7)


def read():
    return Cart().tax + 1
"""


@pytest.mark.parametrize(
    ("line", "col", "form"),
    [
        pytest.param(
            6, 15, _SiteForm(True, cs.DELTA_RECEIVER_INSTANCE), id="self-instance"
        ),
        pytest.param(10, 15, _SiteForm(True, cs.DELTA_RECEIVER_CLASS), id="cls-class"),
        # Negative: what `factory()` returns is not read, and two calls of
        # `tax` start where the chain does, so neither is the site's.
        pytest.param(18, 11, _SiteForm(True, None), id="factory-unread"),
        pytest.param(22, 11, None, id="chain-ambiguous"),
        pytest.param(
            26, 11, _SiteForm(False, cs.DELTA_RECEIVER_INSTANCE), id="read-not-call"
        ),
    ],
)
def test_the_site_form_is_read_from_the_source(
    temp_repo: Path, line: int, col: int, form: _SiteForm | None
) -> None:
    # `self.tax(3)` and `cls.tax(4)` are bound by name today, so the delta
    # judges neither through the graph; the reading itself is pinned here.
    (temp_repo / "cart.py").write_text(READER_PY, encoding="utf-8")
    tax = _method("tax", "cart.py", ("x",))
    empty = Snapshot(frozenset(), {}, {}, (), {}, {})

    read = _site_form(
        _SourceTrees(temp_repo), _call("cart.py", line, col, tax), tax, empty
    )

    assert read == form


def test_self_rebinds_where_cls_does_not() -> None:
    # Through `self` the receiver is now bound as `x`; through `cls` the
    # plain function still takes 4 as `x`.
    plain = _method("tax", "cart.py", ("x",))
    static = plain._replace(decorators=("@staticmethod",))
    old, new = _convention(static), _convention(plain)
    assert old is not None and new is not None
    site = _call("cart.py", 1, 0, plain)

    verdicts = [
        _convention_verdict(site, static, plain, old, new, _SiteForm(True, receiver))
        for receiver in (cs.DELTA_RECEIVER_INSTANCE, cs.DELTA_RECEIVER_CLASS)
    ]

    assert verdicts == [cs.DELTA_CONVENTION_REBOUND, cs.DELTA_ARITY_OK]


def test_a_definition_that_recorded_no_decorators_has_no_convention() -> None:
    assert (
        _convention(_method("tax", "cart.py", ("x",))._replace(decorators=None)) is None
    )


def test_a_method_with_no_parameter_gaining_a_receiver_is_rebound(
    temp_repo: Path,
) -> None:
    # `def ping()` had only `@staticmethod` to keep a receiver out; without
    # it `Cart().ping()` passes one to a method that takes none.
    cart = "class Cart:\n    @staticmethod\n    def ping():\n        return 1\n"
    shop = "from cart import Cart\n\n\ndef call():\n    return Cart().ping()\n"

    delta = _edit(
        temp_repo,
        {"cart.py": cart, "shop.py": shop},
        "cart.py",
        cart.replace("    @staticmethod\n", ""),
    )

    assert _verdicts(delta, "ping") == {"call": cs.DELTA_CONVENTION_REBOUND}
    assert (
        "takes 0 positional arguments but 1 was given" in _python(temp_repo, "call")[0]
    )


def test_a_method_read_without_a_call_is_not_judged(temp_repo: Path) -> None:
    # Negative: `fn = Cart().total` hands the method on; what becomes of
    # `fn` is not this site's to say, nor is `fn()`'s receiver readable.
    shop = "from cart import Cart\n\n\ndef later():\n    fn = Cart().total\n    return fn()\n"

    delta = _edit(
        temp_repo,
        {"cart.py": CART_PY, "shop.py": shop},
        "cart.py",
        CART_PY.replace("    def total(self):", "    @property\n    def total(self):"),
    )

    sites = _change(delta, "total")["sites"]
    assert [(s["line"], s["verdict"]) for s in sites] == [
        (5, cs.DELTA_ARITY_UNKNOWN),
        (6, cs.DELTA_ARITY_UNKNOWN),
    ]
    assert not has_findings(delta)


def test_a_called_property_made_a_method_is_not_judged(temp_repo: Path) -> None:
    # Negative: `Cart().handler()` called what the property returned; it
    # now calls the method, which may return anything.
    cart = (
        "class Cart:\n    @property\n    def handler(self):\n        return lambda: 1\n"
    )
    shop = "from cart import Cart\n\n\ndef call():\n    return Cart().handler()\n"

    delta = _edit(
        temp_repo,
        {"cart.py": cart, "shop.py": shop},
        "cart.py",
        cart.replace("    @property\n", ""),
    )

    assert _verdicts(delta, "handler") == {"call": cs.DELTA_ARITY_UNKNOWN}
    assert not has_findings(delta)


def test_a_binding_change_that_moves_the_parameters_too_is_left_to_the_count(
    temp_repo: Path,
) -> None:
    # Negative: `tax(self, x)` becoming `@staticmethod tax(x, rate)` changes
    # what `Cart().tax(3)` fills, but whether `rate` has a default is the
    # signature change's question, where it stays a hint.
    cart = CART_PY.replace(
        "    @staticmethod\n    def tax(x):", "    def tax(self, x):"
    )

    delta = _edit(
        temp_repo,
        {"cart.py": cart, "shop.py": SHOP_PY},
        "cart.py",
        CART_PY.replace("    def tax(x):", "    def tax(x, rate):"),
    )

    assert _verdicts(delta, "tax")["vat"] == cs.DELTA_ARITY_UNKNOWN
    assert [c["qualified_name"] for c in delta["signature_changes"]] == [
        f"{PROJECT}.cart.Cart.tax"
    ]
    assert not has_findings(delta)


def _method(name: str, path: str, params: tuple[str, ...]) -> Definition:
    return Definition(
        label=cs.NodeLabel.METHOD.value,
        qualified_name=f"{PROJECT}.cart.Cart.{name}",
        name=name,
        path=path,
        start_line=1,
        end_line=2,
        positional_params=params,
        fingerprint="",
        fingerprint_nodes=0,
        branches=frozenset(),
        decorators=(),
        modifiers=(),
    )


def _call(path: str, line: int, col: int, callee: Definition) -> CallSite:
    return CallSite(
        caller=f"{PROJECT}.shop.caller",
        caller_path=path,
        rel=cs.RelationshipType.CALLS.value,
        resolution=cs.EdgeResolution.EXACT.value,
        spread_args=False,
        call_qualifier=None,
        callee=callee.qualified_name,
        callee_path=callee.path,
        line=line,
        col=col,
        arg_count=0,
        kwarg_names=(),
        star_args=False,
    )


def test_a_site_bound_by_name_alone_is_no_finding() -> None:
    # Negative: a heuristic edge may lead to a same-named method the call
    # never runs, however plainly the call is written.
    method = _method("total", "cart.py", ("self",))
    accessor = method._replace(decorators=("@property",))
    site = _call("shop.py", 5, 11, method)
    old, new = _convention(method), _convention(accessor)
    assert old is not None and new is not None
    called = _SiteForm(called=True, receiver=cs.DELTA_RECEIVER_INSTANCE)

    exact = _convention_verdict(site, method, accessor, old, new, called)
    heuristic = _convention_verdict(
        site._replace(resolution=cs.EdgeResolution.HEURISTIC.value),
        method,
        accessor,
        old,
        new,
        called,
    )

    assert (exact, heuristic) == (
        cs.DELTA_CONVENTION_CALLS_ACCESSOR,
        cs.DELTA_ARITY_UNKNOWN,
    )


def test_a_receiver_the_call_does_not_show_is_no_finding(temp_repo: Path) -> None:
    # Negative: `cart` may hold an instance or the class itself; the site is
    # listed for a reviewer but trips nothing. `Cart().tax(3)` beside it is
    # read and still found.
    shop = SHOP_PY + "\n\ndef held(cart):\n    return cart.tax(5)\n"
    shop = shop.replace(
        "from cart import Cart\n", "from cart import Cart\n\n\nHELD = held(Cart())\n"
    )
    edited = CART_PY.replace("    @staticmethod\n", "")

    delta = _edit(temp_repo, {"cart.py": CART_PY, "shop.py": shop}, "cart.py", edited)

    verdicts = _verdicts(delta, "tax")
    assert verdicts["held"] == cs.DELTA_ARITY_UNKNOWN, verdicts
    assert verdicts["vat"] == cs.DELTA_CONVENTION_REBOUND
    held_only = copy.deepcopy(delta)
    for change in held_only["convention_changes"]:
        change["sites"] = [s for s in change["sites"] if s["caller"].endswith(".held")]
    assert not has_findings(held_only)


def test_a_graph_that_recorded_no_decorators_is_not_compared(
    temp_repo: Path,
) -> None:
    # Negative: a node written before decorators and modifiers were read
    # back carries none; a list on one side only is no change.
    store, _updater = _index(temp_repo, {"cart.py": CART_PY, "shop.py": SHOP_PY})
    after = snapshot(store.fetch_all, PROJECT, ["cart.py"])
    legacy = {
        qn: definition._replace(decorators=None, modifiers=None)
        for qn, definition in after.definitions.items()
    }

    assert _changed(after._replace(definitions=legacy), after) == []
    assert isinstance(after.definitions[f"{PROJECT}.cart.Cart.tax"], Definition)
    assert after.definitions[f"{PROJECT}.cart.Cart.tax"].decorators == (
        "@staticmethod",
    )


def test_a_route_decorator_change_is_a_changed_symbol(temp_repo: Path) -> None:
    app = (
        "from flask import Flask\n\napp = Flask(__name__)\n\n\n"
        "@app.route('/x', methods=['DELETE'])\ndef drop():\n    return 'ok'\n"
    )

    delta = _edit(
        temp_repo, {"app.py": app}, "app.py", app.replace("'DELETE'", "'GET'")
    )

    assert delta["symbols"]["changed"] == [f"{PROJECT}.app.drop"]
    assert delta["convention_changes"] == []


def test_a_decorator_on_an_existing_duplicate_is_no_new_duplicate(
    temp_repo: Path,
) -> None:
    # Negative: the body is what a duplicate is made of, and it did not move.
    body = "    total = 0\n    for item in items:\n        if item > 0:\n            total += item * 2\n        else:\n            total -= item\n    return total\n"
    source = f"def first(items):\n{body}\n\ndef second(items):\n{body}"

    delta = _edit(
        temp_repo,
        {"m.py": source},
        "m.py",
        source.replace("def second", "@staticmethod\ndef second"),
    )

    assert delta["symbols"]["changed"] == [f"{PROJECT}.m.second"]
    assert delta["new_duplicates"] == []


def test_cgr_check_fails_on_the_issue_repro(temp_repo: Path) -> None:
    root = temp_repo
    store, _updater = _index(root, {"cart.py": CART_PY, "shop.py": SHOP_PY})
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.email=x@x", "-c", "user.name=x", "commit", "-qm", "init"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (root / "cart.py").write_text(
        CART_PY.replace("    def total(self):", "    @property\n    def total(self):"),
        encoding="utf-8",
    )
    parsers, queries = load_parsers()

    delta = run_check(root, "HEAD", PROJECT, store, parsers, queries)

    assert delta["symbols"]["changed"] == [f"{PROJECT}.cart.Cart.total"]
    assert _verdicts(delta, "total") == {"subtotal": cs.DELTA_CONVENTION_CALLS_ACCESSOR}
    assert has_findings(delta)


# --- TypeScript ---------------------------------------------------------------

CART_TS = """export class Cart {
  items: number[] = [1, 2];

  total(): number {
    return this.items.length;
  }

  static tax(x: number): number {
    return x * 2;
  }

  static twice(x: number): number {
    return this.tax(x) * 2;
  }

  scale(x: number): number {
    return x * this.total();
  }

  peek(): number {
    return this.secret();
  }

  secret(): number {
    return 1;
  }

  shared(): number {
    return 2;
  }
}
"""
SHOP_TS = """import { Cart } from "./cart";

export function subtotal(): number {
  return new Cart().total();
}

export function vat(): number {
  return Cart.tax(3);
}

export function scaled(): number {
  return new Cart().scale(2);
}

export function reveal(): number {
  return new Cart().secret();
}

export class Sub extends Cart {
  viaSub(): number {
    return this.shared();
  }
}
"""


def _tsc(root: Path) -> str:
    assert _TSC is not None
    result = subprocess.run(
        [_TSC, "--noEmit", "--strict", "--target", "es2022", "cart.ts", "shop.ts"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
    )
    return result.stdout


@needs_tsc
def test_typescript_get_and_static_flips_break_their_callers(
    temp_repo: Path,
) -> None:
    edited = (
        CART_TS.replace("  total(): number {", "  get total(): number {")
        .replace("  static tax(", "  tax(")
        .replace("x * this.total()", "x * this.total")
    )
    files = {"cart.ts": CART_TS, "shop.ts": SHOP_TS}

    delta = _edit(temp_repo, files, "cart.ts", edited)

    assert _change(delta, "total")["changes"] == [cs.DELTA_CONVENTION_BECAME_ACCESSOR]
    assert _verdicts(delta, "total") == {"subtotal": cs.DELTA_CONVENTION_CALLS_ACCESSOR}
    assert _change(delta, "tax")["changes"] == [cs.DELTA_CONVENTION_NO_LONGER_STATIC]
    # `this` in a static method is the class.
    assert _verdicts(delta, "tax") == {
        "twice": cs.DELTA_CONVENTION_NEEDS_INSTANCE,
        "vat": cs.DELTA_CONVENTION_NEEDS_INSTANCE,
    }
    assert has_findings(delta)
    errors = _tsc(temp_repo)
    assert "TS6234" in errors and "TS2339" in errors, errors


@needs_tsc
def test_typescript_static_added_breaks_instance_calls(temp_repo: Path) -> None:
    edited = CART_TS.replace(
        "  scale(x: number): number {\n    return x * this.total();",
        "  static scale(x: number): number {\n    return x * 2;",
    )

    delta = _edit(
        temp_repo, {"cart.ts": CART_TS, "shop.ts": SHOP_TS}, "cart.ts", edited
    )

    assert _change(delta, "scale")["changes"] == [cs.DELTA_CONVENTION_BECAME_STATIC]
    assert _verdicts(delta, "scale") == {"scaled": cs.DELTA_CONVENTION_NEEDS_CLASS}
    assert has_findings(delta)
    assert "TS2576" in _tsc(temp_repo)


@needs_tsc
def test_typescript_private_breaks_only_callers_outside_the_class(
    temp_repo: Path,
) -> None:
    edited = CART_TS.replace("  secret(): number {", "  private secret(): number {")

    delta = _edit(
        temp_repo, {"cart.ts": CART_TS, "shop.ts": SHOP_TS}, "cart.ts", edited
    )

    assert _change(delta, "secret")["changes"] == [
        cs.DELTA_CONVENTION_VISIBILITY_NARROWED
    ]
    # Negative: `this.secret()` inside the class keeps its access.
    assert _verdicts(delta, "secret") == {
        "peek": cs.DELTA_ARITY_OK,
        "reveal": cs.DELTA_CONVENTION_INACCESSIBLE,
    }
    errors = _tsc(temp_repo)
    assert "TS2341" in errors and errors.count("error TS") == 1, errors


def test_a_class_call_the_change_makes_valid_is_no_finding(temp_repo: Path) -> None:
    # Negative: JavaScript's `Cart.scale(2)` found no `scale` on the class
    # until the method became static; `new Cart().scale(2)` loses it.
    cart = "export class Cart {\n  scale(x) {\n    return x * 2;\n  }\n}\n"
    shop = (
        'import { Cart } from "./cart.js";\n\n'
        "export function viaClass() {\n  return Cart.scale(2);\n}\n\n"
        "export function viaInstance() {\n  return new Cart().scale(2);\n}\n"
    )

    delta = _edit(
        temp_repo,
        {"package.json": '{"type": "module"}\n', "cart.js": cart, "shop.js": shop},
        "cart.js",
        cart.replace("  scale(x) {", "  static scale(x) {"),
    )

    assert _verdicts(delta, "scale") == {
        "viaClass": cs.DELTA_ARITY_OK,
        "viaInstance": cs.DELTA_CONVENTION_NEEDS_CLASS,
    }


def test_a_widened_visibility_is_no_convention_change(temp_repo: Path) -> None:
    # Negative: `private` to public lets more callers in, never fewer.
    cart = CART_TS.replace("  secret(): number {", "  private secret(): number {")
    shop = SHOP_TS.replace("new Cart().secret()", "0")

    delta = _edit(temp_repo, {"cart.ts": cart, "shop.ts": shop}, "cart.ts", CART_TS)

    assert f"{PROJECT}.cart.Cart.secret" in delta["symbols"]["changed"]
    assert delta["convention_changes"] == []


@needs_tsc
def test_a_protected_member_claims_nothing_of_a_caller_outside_the_class(
    temp_repo: Path,
) -> None:
    # Negative: `protected` still reaches a subclass, and the delta cannot
    # tell a subclass's call from any other outside the class.
    edited = CART_TS.replace("  shared(): number {", "  protected shared(): number {")

    delta = _edit(
        temp_repo, {"cart.ts": CART_TS, "shop.ts": SHOP_TS}, "cart.ts", edited
    )

    assert _verdicts(delta, "shared") == {"viaSub": cs.DELTA_ARITY_UNKNOWN}
    assert not has_findings(delta)
    assert _tsc(temp_repo) == ""


# --- Java ---------------------------------------------------------------------

CART_JAVA = """public class Cart {
  public static int tax(int x) { return x * 2; }
  public static int twice(int x) { return tax(x) * 2; }
  public int viaThis() { return this.tax(1); }
  public int total() { return 3; }
  public int scale(int x) { return x * total(); }
}
"""
SHOP_JAVA = """public class Shop {
  public int vat() { return Cart.tax(3); }
  public int sub() {
    Cart c = new Cart();
    return c.total();
  }
  public int scaled() {
    Cart c = new Cart();
    return c.scale(2);
  }
}
"""


def _javac(root: Path) -> str:
    assert _JAVAC is not None
    result = subprocess.run(
        [_JAVAC, "-d", str(root.parent / "classes"), "Cart.java", "Shop.java"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
        # A set JAVA_TOOL_OPTIONS makes the JVM announce itself on stderr.
        env={k: v for k, v in os.environ.items() if k != "JAVA_TOOL_OPTIONS"},
    )
    return result.stderr


@needs_javac
def test_java_static_removed_and_private_break_their_callers(
    temp_repo: Path,
) -> None:
    edited = CART_JAVA.replace("public static int tax", "public int tax").replace(
        "public int total()", "private int total()"
    )

    delta = _edit(
        temp_repo, {"Cart.java": CART_JAVA, "Shop.java": SHOP_JAVA}, "Cart.java", edited
    )

    # A bare `tax(x)` in a static method names the class; `this.tax(1)` an
    # instance, which still reaches it.
    assert _verdicts(delta, "tax") == {
        "twice(int)": cs.DELTA_CONVENTION_NEEDS_INSTANCE,
        "vat()": cs.DELTA_CONVENTION_NEEDS_INSTANCE,
        "viaThis()": cs.DELTA_ARITY_OK,
    }
    assert _change(delta, "total")["changes"] == [
        cs.DELTA_CONVENTION_VISIBILITY_NARROWED
    ]
    # The bare `total()` inside the class keeps its access.
    assert _verdicts(delta, "total") == {
        "scale(int)": cs.DELTA_ARITY_OK,
        "sub()": cs.DELTA_CONVENTION_INACCESSIBLE,
    }
    assert has_findings(delta)
    errors = _javac(temp_repo)
    assert "non-static method tax(int)" in errors and "has private access" in errors


@needs_javac
def test_java_static_added_keeps_instance_calls(temp_repo: Path) -> None:
    # Negative: Java lets an instance reach a static method.
    edited = CART_JAVA.replace(
        "public int scale(int x) { return x * total(); }",
        "public static int scale(int x) { return x * 2; }",
    )

    delta = _edit(
        temp_repo, {"Cart.java": CART_JAVA, "Shop.java": SHOP_JAVA}, "Cart.java", edited
    )

    assert _change(delta, "scale")["changes"] == [cs.DELTA_CONVENTION_BECAME_STATIC]
    assert _verdicts(delta, "scale") == {"scaled()": cs.DELTA_ARITY_OK}
    assert not has_findings(delta)
    assert _javac(temp_repo) == ""


@needs_javac
def test_a_nested_class_member_made_private_claims_nothing_in_its_file(
    temp_repo: Path,
) -> None:
    # Negative: Java's `private` reaches the whole top-level class, nested
    # classes included, so `Box.peek` keeps `Inner.f` though it sits
    # outside `Inner`.
    box = (
        "public class Box {\n  public int peek() {\n    Inner i = new Inner();\n"
        "    return i.f();\n  }\n\n  static class Inner {\n"
        "    public int f() { return 1; }\n  }\n}\n"
    )

    delta = _edit(
        temp_repo,
        {"Box.java": box},
        "Box.java",
        box.replace("public int f()", "private int f()"),
    )

    assert _verdicts(delta, "f") == {"peek()": cs.DELTA_ARITY_UNKNOWN}
    assert not has_findings(delta)


def test_reordered_modifiers_are_no_change(temp_repo: Path) -> None:
    # Negative: `static public` is `public static`.
    delta = _edit(
        temp_repo,
        {"Cart.java": CART_JAVA, "Shop.java": SHOP_JAVA},
        "Cart.java",
        CART_JAVA.replace("public static int tax", "static public int tax"),
    )

    assert delta["symbols"]["changed"] == []
