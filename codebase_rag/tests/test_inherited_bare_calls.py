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


# A trait or mixin overrides the superclass: Dart and Scala look the last
# `with` type up first, and PHP takes a used trait's method over the parent's.
_DART_OVERRIDE = {
    "pubspec.yaml": 'name: shapes\nenvironment:\n  sdk: ">=3.0.0 <4.0.0"\n',
    "lib/signs.dart": """\
mixin Quiet { String label() => "quiet"; }
mixin Loud { String label() => "loud"; }
class Base { String label() => "base"; }
class Sign extends Base with Loud {
  String implicitCall() => label();
}
class Banner extends Base with Loud, Quiet {
  String implicitCall() => label();
}
""",
}
_SCALA_OVERRIDE = {
    "Signs.scala": """\
package s
class Base { def label(): String = "base" }
trait Loud extends Base { override def label(): String = "loud" }
class Sign extends Base with Loud {
  def implicitCall(): String = label()
}
""",
}
_PHP_OVERRIDE = {
    "lib/helper.php": '<?php\nnamespace Lib;\nfunction helper(): string { return "lib"; }\n',
    "helpers.php": '<?php\nfunction shout(): string { return "fn"; }\n',
    "signs.php": """\
<?php
use function Lib\\helper;
trait Loud { public function label(): string { return "loud"; } }
class Base {
    public function label(): string { return "base"; }
    public function shout(): string { return "base"; }
    public function HELPER(): string { return "base"; }
}
class Sign extends Base {
    use Loud;
    public function thisCall(): string { return $this->label(); }
    public function globalCall(): string { return shout(); }
    public function foldedCall(): string { return HELPER(); }
}
""",
}


@pytest.fixture(scope="module")
def overrides(tmp_path_factory: pytest.TempPathFactory) -> dict[str, _Calls]:
    return {
        grammar: _calls(
            tmp_path_factory.mktemp(f"{grammar}3169o") / "ovr", files, grammar
        )
        for grammar, files in (
            ("dart", _DART_OVERRIDE),
            ("scala", _SCALA_OVERRIDE),
            ("php", _PHP_OVERRIDE),
        )
    }


@pytest.mark.parametrize(
    ("grammar", "caller", "callee"),
    [
        ("dart", "lib.signs.Sign.implicitCall", "lib.signs.Loud.label"),
        ("dart", "lib.signs.Banner.implicitCall", "lib.signs.Quiet.label"),
        ("scala", "Signs.Sign.implicitCall", "Signs.Loud.label"),
        ("php", "signs.Sign.thisCall", "signs.Loud.label"),
    ],
    ids=["dart-mixin", "dart-last-mixin", "scala-trait", "php-trait"],
)
def test_a_trait_or_mixin_overrides_the_superclass(
    overrides: dict[str, _Calls], grammar: str, caller: str, callee: str
) -> None:
    edges = overrides[grammar]
    assert _callees(edges, caller) == {callee: "exact"}, edges


@pytest.mark.parametrize(
    ("caller", "method"),
    [
        ("signs.Sign.globalCall", "signs.Base.shout"),
        ("signs.Sign.foldedCall", "signs.Base.HELPER"),
    ],
    ids=["global-function-in-another-file", "case-folded-function-import"],
)
def test_a_php_bare_call_never_binds_an_inherited_method(
    overrides: dict[str, _Calls], caller: str, method: str
) -> None:
    # Negative: a bare PHP call is a function call whatever the class
    # inherits: `shout()` is the global function in helpers.php, and
    # `HELPER()` the `use function Lib\helper` import, case folded.
    edges = overrides["php"]
    assert method not in _callees(edges, caller), edges


# Dart looks a bare name up lexically first, and a library's scope holds the
# top-level names its unprefixed imports bring in, with whatever those hand
# on through `export` and `part`. A prefixed import's names are reachable
# only as `p.name`, so the inherited member still answers a bare call.
_DART_IMPORTS = {
    "pubspec.yaml": 'name: shapes\nenvironment:\n  sdk: ">=3.0.0 <4.0.0"\n',
    "lib/helpers.dart": "part 'pieces.dart';\nString shout() => \"top\";\n",
    "lib/pieces.dart": "part of 'helpers.dart';\nString hum() => \"part\";\n",
    "lib/more.dart": 'String whisper() => "more";\n',
    "lib/barrel.dart": "export 'more.dart';\n",
    "lib/other.dart": 'String yell() => "other";\n',
    "lib/signs.dart": """\
import 'helpers.dart';
import 'barrel.dart';
import 'other.dart' as p;
class Base {
  String shout() => "b"; String whisper() => "b";
  String hum() => "b"; String yell() => "b";
}
class Sign extends Base {
  String importedCall() => shout();
  String exportedCall() => whisper();
  String partCall() => hum();
  String prefixedCall() => yell();
}
""",
}


@pytest.fixture(scope="module")
def dart_imports(tmp_path_factory: pytest.TempPathFactory) -> _Calls:
    return _calls(tmp_path_factory.mktemp("dart3169i") / "imp", _DART_IMPORTS, "dart")


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("lib.signs.Sign.importedCall", "lib.helpers.shout"),
        ("lib.signs.Sign.exportedCall", "lib.more.whisper"),
        ("lib.signs.Sign.partCall", "lib.pieces.hum"),
    ],
    ids=["unprefixed-import", "through-export", "through-part"],
)
def test_a_dart_imported_top_level_function_wins_over_an_inherited_method(
    dart_imports: _Calls, caller: str, callee: str
) -> None:
    callees = _callees(dart_imports, caller)
    assert callee in callees, dart_imports
    assert not any(".Base." in to for to in callees), dart_imports


def test_a_dart_prefix_only_import_leaves_the_inherited_method(
    dart_imports: _Calls,
) -> None:
    # Negative: `import 'other.dart' as p;` binds `yell` as `p.yell` only.
    assert _callees(dart_imports, "lib.signs.Sign.prefixedCall") == {
        "lib.signs.Base.yell": "exact"
    }, dart_imports
