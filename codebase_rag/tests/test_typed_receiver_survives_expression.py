"""A receiver assigned from an expression keeps its type.

Issue #1868. `_collect_local_aliases` recorded an alias only when the
assignment's right-hand side was a bare name or attribute. Every other
expression left the variable UNTYPED, and a later `var.method()` then fell
back to matching the bare method name against every class in the project that
defines it -- emitting a CALLS edge to each.

The shapes below are the common ones for a typed receiver: `a or default`,
`a if cond else b`, and `base / "sub"` (the pathlib idiom). All three appear
in this repo's own `codebase_rag/trace`, where they produced false CALLS
edges from `cli.pull_cmd` into five unrelated `*FrameResolver.resolve`
methods.

Every test here pairs the collision fixture with a CONTROL in which the
decoy class defines `compute()` instead of `resolve()`. Without the control
a green result is unreadable: it would look identical whether the edge was
correctly suppressed or the harness simply produced no edges at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

# The decoy defines a method whose NAME collides with pathlib.Path.resolve.
# Nothing in the fixture ever calls it.
_DECOY_COLLIDES = "class Engine:\n    def resolve(self) -> int:\n        return 1\n"
# Same class, method renamed: the bare-name fallback has nothing to match, so
# a correctly-behaving indexer and a broken one both emit no edge. This is the
# instrument check, not a behaviour assertion.
_DECOY_CONTROL = "class Engine:\n    def compute(self) -> int:\n        return 1\n"

_HEAD = "from pathlib import Path\n\ndef run(target: Path | None, base: Path) -> str:\n"

_BODIES = {
    "or_expression": (
        "    resolved = target or Path('x')\n    return resolved.resolve().as_posix()\n"
    ),
    "ternary": (
        "    resolved = target if target else Path('x')\n"
        "    return resolved.resolve().as_posix()\n"
    ),
    "path_division": (
        "    resolved = base / 'sub'\n    return resolved.resolve().as_posix()\n"
    ),
}


def _decoy_resolve_edges(repo: Path) -> set[tuple[str, str]]:
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(ingestor=store, repo_path=repo, parsers=parsers, queries=queries).run(
        force=True
    )
    return {
        (str(src), str(tgt))
        for _sl, src, rel, _tl, tgt in store.edges
        if rel == cs.RelationshipType.CALLS.value and "Engine.resolve" in str(tgt)
    }


def _build(tmp_path: Path, body: str, decoy: str) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "__init__.py").touch()
    (repo / "engine.py").write_text(decoy)
    (repo / "app.py").write_text(_HEAD + body)
    return repo


@pytest.mark.parametrize("shape", sorted(_BODIES))
def test_an_expression_receiver_does_not_call_every_same_named_method(
    tmp_path: Path, shape: str
) -> None:
    """The defect: the receiver's type is lost and the call matches by name."""
    repo = _build(tmp_path, _BODIES[shape], _DECOY_COLLIDES)

    assert not _decoy_resolve_edges(repo), (
        f"a {shape} receiver is a pathlib.Path, but .resolve() was linked to "
        f"Engine.resolve, which nothing calls"
    )


@pytest.mark.parametrize("shape", sorted(_BODIES))
def test_the_control_cannot_produce_the_edge_it_is_checking_for(
    tmp_path: Path, shape: str
) -> None:
    """Validates the instrument rather than the code under test.

    With the decoy's method renamed there is no name to collide with, so no
    edge can be emitted for any reason. If this ever fails, the fixture is
    matching something other than the collision and the test above proves
    nothing.
    """
    repo = _build(tmp_path, _BODIES[shape], _DECOY_CONTROL)

    assert not _decoy_resolve_edges(repo)


def test_a_long_operator_chain_does_not_void_the_whole_function(
    tmp_path: Path,
) -> None:
    """A deep chain must not take the enclosing function's type map with it.

    `_alias_referent` recurses once per operand, so a long `or` chain can
    exhaust the stack. The RecursionError never reaches a user: the caller
    sits inside a broad `except Exception` in `type_inference.py` that logs at
    debug and returns, dropping EVERY alias for the function. The receiver two
    lines away then reverts to bare-name matching -- issue #1868 reintroduced
    by an unrelated statement.

    So the chain and the receiver share a function deliberately: that is the
    blast radius being asserted. Found by the local review of the #1868 fix,
    which measured 495 RecursionErrors raised and swallowed on this shape.
    """
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "__init__.py").touch()
    (repo / "engine.py").write_text(_DECOY_COLLIDES)
    chain = " or ".join(f"a{i}" for i in range(1200))
    (repo / "app.py").write_text(
        "from pathlib import Path\n"
        "\n"
        "def run(target: Path | None) -> str:\n"
        f"    junk = {chain}\n"
        "    resolved = target or Path('x')\n"
        "    return resolved.resolve().as_posix() + str(junk)\n"
    )

    assert not _decoy_resolve_edges(repo), (
        "a deep operator chain elsewhere in the function voided its type map, "
        "so the receiver fell back to matching Engine.resolve by name"
    )


def _calls_from(repo: Path, caller: str) -> set[str]:
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(ingestor=store, repo_path=repo, parsers=parsers, queries=queries).run(
        force=True
    )
    return {
        str(tgt)
        for _sl, src, rel, _tl, tgt in store.edges
        if rel == cs.RelationshipType.CALLS.value and str(src).endswith(caller)
    }


def test_and_takes_the_guarded_value_not_the_guard(tmp_path: Path) -> None:
    """`flag and Engine()` evaluates to the Engine whenever it is called on.

    Left-first aliasing typed the receiver as the bool guard and DROPPED the
    call (Greptile P2 and CodeRabbit Major on PR #1870, both executed). The
    decoy `Other.start` proves the edge comes from the receiver's type, not
    from bare-name matching, which would emit both.
    """
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "__init__.py").touch()
    (repo / "engine.py").write_text(
        "class Engine:\n    def start(self) -> int:\n        return 1\n\n"
        "class Other:\n    def start(self) -> int:\n        return 2\n"
    )
    (repo / "app.py").write_text(
        "from engine import Engine\n"
        "\n"
        "def run(flag: bool) -> int:\n"
        "    resolved = flag and Engine()\n"
        "    return resolved.start()\n"
    )

    calls = _calls_from(repo, "app.run")
    assert any(t.endswith("engine.Engine.start") for t in calls), calls
    assert not any(t.endswith("engine.Other.start") for t in calls), calls


_TWO_CLASSES = (
    "class Widget:\n    def run(self) -> int:\n        return 1\n\n"
    "class Engine:\n    def run(self) -> int:\n        return 2\n"
)


def _pick_calls(tmp_path: Path, name: str, signature: str, body: str) -> set[str]:
    # A RELATIVE import: the fixture is a package, so `from engine import X`
    # names a top-level module that does not exist and the class never
    # resolves to its registry qn -- every receiver would then go through
    # the bare-name fallback and the test could not see typed resolution.
    repo = tmp_path / name
    repo.mkdir()
    (repo / "__init__.py").touch()
    (repo / "engine.py").write_text(_TWO_CLASSES)
    (repo / "app.py").write_text(
        f"from .engine import Widget, Engine\n\ndef pick({signature}) -> int:\n{body}"
    )
    return _calls_from(repo, "app.pick")


def test_branches_of_different_types_behave_like_a_union_annotation(
    tmp_path: Path,
) -> None:
    """`widget if flag else engine` IS `Widget | Engine`, and is treated as one.

    Typing the receiver as only the first branch keeps `Widget.run` and drops
    `Engine.run`; leaving it untyped hands it to the bare-name fallback, which
    picks ONE of the two arbitrarily (measured on this fixture: `Engine.run`
    alone). Neither is right. The receiver is typed as the union an annotation
    would spell, so whatever the resolver does for `chosen: Widget | Engine`
    -- today it emits no edge, and per-member emission would be the
    improvement for both -- it does identically here. The plain assignment is
    the known-positive that proves the harness observes edges at all.
    """
    both = "flag: bool, widget: Widget, engine: Engine"
    direct = _pick_calls(
        tmp_path, "direct", both, "    chosen = widget\n    return chosen.run()\n"
    )
    annotated = _pick_calls(
        tmp_path, "annotated", "chosen: Widget | Engine", "    return chosen.run()\n"
    )
    ternary = _pick_calls(
        tmp_path,
        "ternary",
        both,
        "    chosen = widget if flag else engine\n    return chosen.run()\n",
    )

    assert any(t.endswith("engine.Widget.run") for t in direct), direct
    assert ternary == annotated, (ternary, annotated)
    assert not (
        len(ternary) == 1 and next(iter(ternary)).endswith("engine.Engine.run")
    ), "the bare-name fallback picked one branch arbitrarily"


def test_an_overloaded_operator_yields_its_return_type(tmp_path: Path) -> None:
    """`factory / config` is whatever `Factory.__truediv__` returns.

    Treating every operator as returning its left operand's type indexed
    `product.run()` as `Factory.run` and never as `Product.run` (Greptile P2 on
    PR #1870, executed). `Factory.run` exists precisely so the wrong answer is
    observable rather than merely absent.
    """
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "__init__.py").touch()
    (repo / "engine.py").write_text(
        "class Config:\n    pass\n\n"
        "class Product:\n    def run(self) -> int:\n        return 1\n\n"
        "class Factory:\n"
        "    def run(self) -> int:\n        return 0\n"
        "    def __truediv__(self, config: Config) -> Product:\n"
        "        return Product()\n"
    )
    (repo / "app.py").write_text(
        "from .engine import Factory, Config\n"
        "\n"
        "def exercise(factory: Factory, config: Config) -> int:\n"
        "    product = factory / config\n"
        "    return product.run()\n"
    )

    calls = _calls_from(repo, "app.exercise")
    assert any(t.endswith("engine.Product.run") for t in calls), calls
    assert not any(t.endswith("engine.Factory.run") for t in calls), calls


def _operator_repo(tmp_path: Path, engine_src: str, app_src: str) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "__init__.py").touch()
    (repo / "engine.py").write_text(engine_src)
    (repo / "app.py").write_text("from .engine import *  # noqa: F403\n\n" + app_src)
    return repo


_PRODUCT = "class Config:\n    pass\n\nclass Product:\n    def run(self) -> int:\n        return 1\n\n"


def test_an_inherited_operator_is_found_through_the_base_class(tmp_path: Path) -> None:
    """`Sub / cfg` where only `Base` defines `__truediv__ -> Product`.

    Looking for the dunder on `Sub` alone found nothing and kept `Sub` as the
    result type, indexing `product.run()` as `Sub.run` (Greptile, executed;
    CodeRabbit Major). The method is found up the bases, as the interpreter
    finds it. `Base.run` and `Sub.run` exist so the wrong answer is visible.
    """
    repo = _operator_repo(
        tmp_path,
        _PRODUCT + "class Base:\n    def run(self) -> int:\n        return 0\n"
        "    def __truediv__(self, config: Config) -> Product:\n        return Product()\n\n"
        "class Sub(Base):\n    def run(self) -> int:\n        return 2\n",
        "def exercise(sub: Sub, config: Config) -> int:\n"
        "    product = sub / config\n    return product.run()\n",
    )
    calls = _calls_from(repo, "app.exercise")
    assert any(t.endswith("engine.Product.run") for t in calls), calls
    assert not any(
        t.endswith("engine.Sub.run") or t.endswith("engine.Base.run") for t in calls
    ), calls


def test_a_reflected_operator_on_the_right_operand_decides(tmp_path: Path) -> None:
    """`left / right` where `Left` has no `__truediv__` and `Right.__rtruediv__ -> Product`.

    Consulting only the left operand kept `Left` and indexed `product.run()`
    as `Left.run`. Python tries the right operand's reflected method when the
    left has no forward one; so does this.
    """
    repo = _operator_repo(
        tmp_path,
        _PRODUCT + "class Left:\n    def run(self) -> int:\n        return 0\n\n"
        "class Right:\n    def run(self) -> int:\n        return 3\n"
        "    def __rtruediv__(self, other: Left) -> Product:\n        return Product()\n",
        "def exercise(left: Left, right: Right) -> int:\n"
        "    product = left / right\n    return product.run()\n",
    )
    calls = _calls_from(repo, "app.exercise")
    assert any(t.endswith("engine.Product.run") for t in calls), calls
    assert not any(
        t.endswith("engine.Left.run") or t.endswith("engine.Right.run") for t in calls
    ), calls


def test_a_union_left_operand_resolves_each_member(tmp_path: Path) -> None:
    """`f = fa if flag else fb; product = f / cfg`, both factories returning Product.

    The union string `FactoryA | FactoryB` was handed to the class lookup as if
    it were one name, resolved to nothing, and the receiver kept the union.
    Each member's operator is resolved and the results merged.
    """
    repo = _operator_repo(
        tmp_path,
        _PRODUCT + "class FactoryA:\n    def run(self) -> int:\n        return 0\n"
        "    def __truediv__(self, config: Config) -> Product:\n        return Product()\n\n"
        "class FactoryB:\n    def run(self) -> int:\n        return 0\n"
        "    def __truediv__(self, config: Config) -> Product:\n        return Product()\n",
        "def exercise(flag: bool, fa: FactoryA, fb: FactoryB, config: Config) -> int:\n"
        "    factory = fa if flag else fb\n"
        "    product = factory / config\n    return product.run()\n",
    )
    calls = _calls_from(repo, "app.exercise")
    assert any(t.endswith("engine.Product.run") for t in calls), calls
    assert not any(
        t.endswith("engine.FactoryA.run") or t.endswith("engine.FactoryB.run")
        for t in calls
    ), calls


def test_a_subclass_on_the_right_gets_its_reflected_method_first(
    tmp_path: Path,
) -> None:
    """`base / derived` with `Derived(Base)`: Python calls `Derived.__rtruediv__`
    BEFORE `Base.__truediv__`, so the result is Second, not First (Greptile and
    CodeRabbit, round 3, both executed against CPython)."""
    repo = _operator_repo(
        tmp_path,
        "class First:\n    def run(self) -> int:\n        return 1\n\n"
        "class Second:\n    def run(self) -> int:\n        return 2\n\n"
        "class Base:\n"
        "    def __truediv__(self, other: object) -> First:\n        return First()\n\n"
        "class Derived(Base):\n"
        "    def __rtruediv__(self, other: Base) -> Second:\n        return Second()\n",
        "def exercise(base: Base, derived: Derived) -> int:\n"
        "    result = base / derived\n    return result.run()\n",
    )
    calls = _calls_from(repo, "app.exercise")
    assert any(t.endswith("engine.Second.run") for t in calls), calls
    assert not any(t.endswith("engine.First.run") for t in calls), calls


def test_operator_lookup_follows_c3_not_breadth_first(tmp_path: Path) -> None:
    """`Child(A, B)`, `A(X)`: the MRO is Child, A, X, B, so X's operator wins
    over B's. Breadth-first visited B before X and picked the wrong result
    (Greptile and CodeRabbit, round 3, executed against CPython)."""
    repo = _operator_repo(
        tmp_path,
        "class FromX:\n    def run(self) -> int:\n        return 1\n\n"
        "class FromB:\n    def run(self) -> int:\n        return 2\n\n"
        "class X:\n"
        "    def __truediv__(self, other: object) -> FromX:\n        return FromX()\n\n"
        "class A(X):\n    pass\n\n"
        "class B:\n"
        "    def __truediv__(self, other: object) -> FromB:\n        return FromB()\n\n"
        "class Child(A, B):\n    pass\n",
        "def exercise(child: Child) -> int:\n"
        "    result = child / 1\n    return result.run()\n",
    )
    calls = _calls_from(repo, "app.exercise")
    assert any(t.endswith("engine.FromX.run") for t in calls), calls
    assert not any(t.endswith("engine.FromB.run") for t in calls), calls


def test_an_inherited_reflected_method_gives_no_priority(tmp_path: Path) -> None:
    """`Base` defines both `__truediv__ -> First` and `__rtruediv__ -> Second`;
    `Derived(Base)` defines neither. For `base / derived` Python runs the
    FORWARD method: the subclass only inherits the reflected one, which is
    Base's own, so it earns no priority (Greptile, round 4, executed)."""
    repo = _operator_repo(
        tmp_path,
        "class First:\n    def run(self) -> int:\n        return 1\n\n"
        "class Second:\n    def run(self) -> int:\n        return 2\n\n"
        "class Base:\n"
        "    def __truediv__(self, other: object) -> First:\n        return First()\n"
        "    def __rtruediv__(self, other: object) -> Second:\n        return Second()\n\n"
        "class Derived(Base):\n    pass\n",
        "def exercise(base: Base, derived: Derived) -> int:\n"
        "    result = base / derived\n    return result.run()\n",
    )
    calls = _calls_from(repo, "app.exercise")
    assert any(t.endswith("engine.First.run") for t in calls), calls
    assert not any(t.endswith("engine.Second.run") for t in calls), calls


def test_the_fixture_can_go_red(tmp_path: Path) -> None:
    """A known-positive: the bare-name fallback DOES fire when the type is
    genuinely unknowable, so the assertions above are not vacuously green.

    `_make()` has no return annotation and returns a decoy instance, so there
    is no type to recover and matching by name is the only option left. An
    edge here is expected and correct; its presence proves the harness can
    observe an Engine.resolve edge when one is emitted.
    """
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "__init__.py").touch()
    (repo / "engine.py").write_text(_DECOY_COLLIDES)
    (repo / "app.py").write_text(
        "from engine import Engine\n"
        "\n"
        "def _make():\n"
        "    return Engine()\n"
        "\n"
        "def run():\n"
        "    thing = _make()\n"
        "    return thing.resolve()\n"
    )

    assert _decoy_resolve_edges(repo), (
        "the harness never observes an Engine.resolve edge, so the assertions "
        "that no such edge exists are not evidence of anything"
    )
