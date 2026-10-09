"""A method's call to a method its class inherits binds the base's method.

In C++, Dart, PHP and Scala, `label()` / `this->label()` / `$this->label()`
inside `Circle` reach the resolver as the bare name `label`. Only `Circle`
itself was searched, so the name-only fallback bound a same-named method of
an unrelated class (`Archive.label`, heuristic) instead of `Base.label`
(issue #3169; aria2: 713 of 1,693 such calls, every bloc `Cubit.emit`).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

_CPP = {
    "shapes.cpp": """\
int free_fn() { return 7; }
class Archive { public: int label() { return 0; } int own() { return 0; } };
class Base { public: int label() { return 1; } int own() { return 2; } };
class Circle : public Base {
 public:
  int own() { return 3; }
  int implicitCall() { return label(); }
  int thisCall() { return this->label(); }
  int ownCall() { return own(); }
  int freeCall() { return free_fn(); }
};
""",
}
_DART = {
    "pubspec.yaml": 'name: shapes\nenvironment:\n  sdk: ">=3.0.0 <4.0.0"\n',
    "lib/shapes.dart": """\
mixin Describes { String describe() => "mixin"; }
class Archive { String label() => "archive"; String describe() => "archive"; String shout() => "a"; }
class Base { String label() => "base"; String shout() => "b"; }
String shout() => "top";
class Circle extends Base with Describes {
  String implicitCall() => label();
  String thisCall() => this.label();
  String mixinCall() => describe();
  String topLevelCall() => shout();
}
""",
}
_PHP = {
    "shapes.php": """\
<?php
function helper(): string { return "fn"; }
trait Describes { public function describe(): string { return "trait"; } }
class Archive {
    public function label(): string { return "archive"; }
    public function describe(): string { return "archive"; }
    public function helper(): string { return "archive"; }
}
class Base {
    public function label(): string { return "base"; }
    public function helper(): string { return "base"; }
}
class Circle extends Base {
    use Describes;
    public function thisCall(): string { return $this->label(); }
    public function traitCall(): string { return $this->describe(); }
    public function functionCall(): string { return helper(); }
}
""",
}
_SCALA = {
    "Shapes.scala": """\
package s
class Archive { def label(): String = "archive" }
class Base { def label(): String = "base" }
class Circle extends Base {
  def implicitCall(): String = label()
  def thisCall(): String = this.label()
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
def cpp(tmp_path_factory: pytest.TempPathFactory) -> _Calls:
    return _calls(tmp_path_factory.mktemp("cpp3169") / "inh", _CPP, "cpp")


@pytest.fixture(scope="module")
def dart(tmp_path_factory: pytest.TempPathFactory) -> _Calls:
    return _calls(tmp_path_factory.mktemp("dart3169") / "inh", _DART, "dart")


@pytest.fixture(scope="module")
def php(tmp_path_factory: pytest.TempPathFactory) -> _Calls:
    return _calls(tmp_path_factory.mktemp("php3169") / "inh", _PHP, "php")


@pytest.fixture(scope="module")
def scala(tmp_path_factory: pytest.TempPathFactory) -> _Calls:
    return _calls(tmp_path_factory.mktemp("scala3169") / "inh", _SCALA, "scala")


def _callees(calls: _Calls, caller: str) -> dict[str, str]:
    return {to: res for (src, to), res in calls.items() if src == caller}


@pytest.mark.parametrize(
    ("calls", "caller", "callee"),
    [
        ("cpp", "shapes.Circle.implicitCall", "shapes.Base.label"),
        ("cpp", "shapes.Circle.thisCall", "shapes.Base.label"),
        ("dart", "lib.shapes.Circle.implicitCall", "lib.shapes.Base.label"),
        ("dart", "lib.shapes.Circle.thisCall", "lib.shapes.Base.label"),
        ("dart", "lib.shapes.Circle.mixinCall", "lib.shapes.Describes.describe"),
        ("php", "shapes.Circle.thisCall", "shapes.Base.label"),
        ("php", "shapes.Circle.traitCall", "shapes.Describes.describe"),
        ("scala", "Shapes.Circle.implicitCall", "Shapes.Base.label"),
        ("scala", "Shapes.Circle.thisCall", "Shapes.Base.label"),
    ],
    ids=[
        "cpp-implicit",
        "cpp-this",
        "dart-implicit",
        "dart-this",
        "dart-mixin",
        "php-this",
        "php-trait",
        "scala-implicit",
        "scala-this",
    ],
)
def test_an_inherited_call_binds_the_base_method(
    request: pytest.FixtureRequest, calls: str, caller: str, callee: str
) -> None:
    edges: _Calls = request.getfixturevalue(calls)
    assert _callees(edges, caller) == {callee: "exact"}, edges


@pytest.mark.parametrize(
    ("calls", "caller", "callee"),
    [
        ("cpp", "shapes.Circle.ownCall", "shapes.Circle.own"),
        ("cpp", "shapes.Circle.freeCall", "shapes.free_fn"),
        ("dart", "lib.shapes.Circle.topLevelCall", "lib.shapes.shout"),
        ("php", "shapes.Circle.functionCall", "shapes.helper"),
    ],
    ids=[
        "own-method-overrides-base",
        "cpp-free-function",
        "dart-top-level-function-first",
        "php-bare-call-is-a-function",
    ],
)
def test_names_the_class_does_not_inherit_resolve_as_before(
    request: pytest.FixtureRequest, calls: str, caller: str, callee: str
) -> None:
    # Negatives: the class's own override wins over its base's; a free C++
    # function no base defines is still the free function; a Dart top-level
    # function is found lexically before any inherited member; and a PHP
    # bare `helper()` calls the function, not `Base::helper`.
    edges: _Calls = request.getfixturevalue(calls)
    assert callee in _callees(edges, caller), edges
