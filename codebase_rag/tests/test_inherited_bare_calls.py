"""A method's call to a method its class inherits binds the base's method.

In C++, Dart and PHP, `label()` / `this->label()` / `$this->label()` inside
`Circle` reach the resolver as the bare name `label`. Only `Circle` itself
was searched, so the name-only fallback bound a same-named method of an
unrelated class (`Archive.label`, heuristic) instead of `Base.label` (issue
#3169; aria2: 713 of 1,693 such calls, every bloc `Cubit.emit`). Scala is
left as on main for now (see the last test).
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
    ],
    ids=[
        "cpp-implicit",
        "cpp-this",
        "dart-implicit",
        "dart-this",
        "dart-mixin",
        "php-this",
        "php-trait",
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


# A trait or mixin overrides the superclass: Dart looks the last `with` type
# up first, and PHP takes a used trait's method over the parent's.
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
    # `insteadof` picks the trait whose method runs, whatever the order
    # (Greptile on PR #3240).
    "picks.php": """\
<?php
trait First { public function label(): string { return "first"; } }
trait Second { public function label(): string { return "second"; } }
class Picked {
    use First, Second { First::label insteadof Second; }
    public function thisCall(): string { return $this->label(); }
}
class PickedLater {
    use Second, First { Second::LABEL insteadof First; }
    public function thisCall(): string { return $this->label(); }
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
            ("php", _PHP_OVERRIDE),
        )
    }


@pytest.mark.parametrize(
    ("grammar", "caller", "callee"),
    [
        ("dart", "lib.signs.Sign.implicitCall", "lib.signs.Loud.label"),
        ("dart", "lib.signs.Banner.implicitCall", "lib.signs.Quiet.label"),
        ("php", "signs.Sign.thisCall", "signs.Loud.label"),
        ("php", "picks.Picked.thisCall", "picks.First.label"),
        ("php", "picks.PickedLater.thisCall", "picks.Second.label"),
    ],
    ids=[
        "dart-mixin",
        "dart-last-mixin",
        "php-trait",
        "php-insteadof",
        "php-insteadof-folded",
    ],
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


# A `package:` URI is kept as written, so the guard cannot read what such an
# import brings in. Where the project declares a top-level function of the
# name, that import may be the one bringing it in (here it is the package's
# own lib/helpers.dart), so the inherited member is no certainty. Where the
# project declares none, nothing first-party can answer, and an import the
# guard reads in full rules the name out either way.
_DART_PACKAGE = {
    "pubspec.yaml": 'name: shapes\nenvironment:\n  sdk: ">=3.0.0 <4.0.0"\n',
    "lib/helpers.dart": 'String shout() => "top";\n',
    "lib/base.dart": 'class Base { String shout() => "b"; String emit() => "b"; }\n',
    "lib/signs.dart": """\
import 'package:shapes/helpers.dart';
import 'package:bloc/bloc.dart';
import 'base.dart';
class Sign extends Base {
  String packageCall() => shout();
  String unreadCall() => emit();
}
""",
    "lib/plain.dart": """\
import 'base.dart';
class Plain extends Base {
  String resolvedCall() => shout();
}
""",
}


@pytest.fixture(scope="module")
def dart_package(tmp_path_factory: pytest.TempPathFactory) -> _Calls:
    return _calls(tmp_path_factory.mktemp("dart3169p") / "pkg", _DART_PACKAGE, "dart")


def test_a_dart_package_import_may_bring_the_top_level_function_in(
    dart_package: _Calls,
) -> None:
    # `import 'package:shapes/helpers.dart'` brings in lib/helpers.dart's
    # `shout`, so the call keeps the name search's answer, not Base.shout.
    callees = _callees(dart_package, "lib.signs.Sign.packageCall")
    assert "lib.helpers.shout" in callees, dart_package
    assert not any(".Base." in to for to in callees), dart_package


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("lib.signs.Sign.unreadCall", "lib.base.Base.emit"),
        ("lib.plain.Plain.resolvedCall", "lib.base.Base.shout"),
    ],
    ids=["no-top-level-of-the-name", "only-readable-imports"],
)
def test_a_dart_inherited_call_stays_exact_where_no_import_can_hide_it(
    dart_package: _Calls, caller: str, callee: str
) -> None:
    # Controls: no first-party top-level `emit` exists for the unread
    # `package:bloc` import to bring in, and plain.dart imports only
    # base.dart, which the guard reads and which declares no `shout`.
    assert _callees(dart_package, caller) == {callee: "exact"}, dart_package


# PHP, C++ and Scala name a call on any receiver by its bare method too:
# `$other->label()`, `a.b.label()` and `xs label y` reach the resolver as
# `label`, just as `$this->label()`, `label()` and `this.label(1)` do. Only a
# call on the caller's own object reaches what its class inherits; the others
# keep the name search's answer.
_PHP_RECEIVERS = {
    "receivers.php": """\
<?php
class Archive { public function label(): string { return "archive"; } }
class Base { public function label(): string { return "base"; } }
class Circle extends Base {
    private $repo;
    public function otherCall($other): string { return $other->label(); }
    public function chainCall(): string { return $this->repo->label(); }
    public function scopedCall(): string { return Archive::label(); }
    public function nullsafeCall(): string { return $this?->label(); }
}
""",
}
_CPP_RECEIVERS = {
    "receivers.cpp": """\
class Archive { public: int label() { return 0; } };
class Base {
 public:
  int label() { return 1; }
  Archive* getA() { return nullptr; }
};
struct Holder { Archive b; };
class Circle : public Base {
 public:
  Archive items[2];
  int factoryCall() { return getA()->label(); }
  int chainCall(Holder a) { return a.b.label(); }
  int indexCall() { return items[0].label(); }
  int derefCall(Archive* p) { return (*p).label(); }
  int derefThisCall() { return (*this).label(); }
};
""",
}
_SCALA_RECEIVERS = {
    "Receivers.scala": """\
package s
class Archive { def label(y: Int): String = "archive" }
class Base { def label(y: Int): String = "base" }
class Circle extends Base {
  def infixCall(xs: Archive, y: Int): String = xs label y
  def dotCall(xs: Archive): String = xs.label(1)
}
""",
}


@pytest.fixture(scope="module")
def receivers(tmp_path_factory: pytest.TempPathFactory) -> dict[str, _Calls]:
    return {
        grammar: _calls(
            tmp_path_factory.mktemp(f"{grammar}3169r") / "rcv", files, grammar
        )
        for grammar, files in (
            ("php", _PHP_RECEIVERS),
            ("cpp", _CPP_RECEIVERS),
            ("scala", _SCALA_RECEIVERS),
        )
    }


@pytest.mark.parametrize(
    ("grammar", "caller"),
    [
        ("php", "receivers.Circle.otherCall"),
        ("php", "receivers.Circle.chainCall"),
        ("php", "receivers.Circle.scopedCall"),
        ("cpp", "receivers.Circle.chainCall"),
        ("cpp", "receivers.Circle.indexCall"),
        ("cpp", "receivers.Circle.derefCall"),
        ("scala", "Receivers.Circle.infixCall"),
        ("scala", "Receivers.Circle.dotCall"),
    ],
    ids=[
        "php-other-variable",
        "php-property-chain",
        "php-other-class",
        "cpp-member-chain",
        "cpp-subscript",
        "cpp-dereferenced-pointer",
        "scala-infix",
        "scala-other-value",
    ],
)
def test_a_call_on_another_receiver_never_binds_an_inherited_method(
    receivers: dict[str, _Calls], grammar: str, caller: str
) -> None:
    edges = receivers[grammar]
    callees = _callees(edges, caller)
    assert not any(to.endswith(".Base.label") for to in callees), edges
    assert any(to.endswith(".Archive.label") for to in callees), edges


@pytest.mark.parametrize(
    ("grammar", "caller", "callee"),
    [
        ("php", "receivers.Circle.nullsafeCall", "receivers.Base.label"),
        ("cpp", "receivers.Circle.derefThisCall", "receivers.Base.label"),
    ],
    ids=["php-nullsafe-this", "cpp-dereferenced-this"],
)
def test_a_call_on_the_own_object_still_binds_the_base_method(
    receivers: dict[str, _Calls], grammar: str, caller: str, callee: str
) -> None:
    # Controls: `$this?->label()` and `(*this).label()` are calls on the
    # caller's own object, as `$this->label()` and `this->label()` are.
    assert _callees(receivers[grammar], caller) == {callee: "exact"}, receivers[grammar]


def test_an_inherited_factory_still_types_the_chain_it_starts(
    receivers: dict[str, _Calls],
) -> None:
    # Control: `getA()` in `getA()->label()` has no receiver of its own, so it
    # is Base.getA, whose return type then binds `label` on Archive.
    assert _callees(receivers["cpp"], "receivers.Circle.factoryCall") == {
        "receivers.Base.getA": "exact",
        "receivers.Archive.label": "exact",
    }, receivers["cpp"]


# Second review round (Greptile, CodeRabbit, PR #3240): a parameter or local
# hides an inherited method of its name, and PHP composes each class's traits
# on its own, names folded.
_ROUND2 = {
    "cpp": {
        "hide.cpp": """\
class Other { public: int label() { return 0; } };
class Base { public: int label() { return 1; } };
class Kid : public Base {
 public:
  int run(int (*label)()) { return label(); }
  int local() { auto label = [] { return 2; }; return label(); }
  int plain() { return label(); }
};
""",
    },
    "dart": {
        "pubspec.yaml": 'name: shapes\nenvironment:\n  sdk: ">=3.0.0 <4.0.0"\n',
        "lib/hide.dart": """\
class Other { String label() => "o"; }
class Base { String label() => "b"; }
class Kid extends Base {
  String run(String Function() label) => label();
  String explicit(String Function() label) => this.label();
  String plain() => label();
}
""",
    },
    "php": {
        "compose.php": """\
<?php
trait T { public function label(): string { return "t"; } }
class P { use T; public function label(): string { return "p"; } }
class C extends P {
    use T;
    public function call(): string { return $this->label(); }
}
class B2 { public function label(): string { return "b2"; } }
class C2 extends B2 {
    public function LABEL(): string { return "c2"; }
    public function call(): string { return $this->label(); }
}
trait First2 { public function label(): string { return "1"; } }
trait Second2 { public function label(): string { return "2"; } }
class PP { use First2, Second2 { First2::label insteadof Second2; } }
class CC extends PP {
    use Second2;
    public function call(): string { return $this->label(); }
}
function make() {
    return new class extends P {
        use T;
        public function call(): string { return $this->label(); }
    };
}
""",
    },
}


@pytest.fixture(scope="module")
def round2(tmp_path_factory: pytest.TempPathFactory) -> dict[str, _Calls]:
    return {
        grammar: _calls(
            tmp_path_factory.mktemp(f"{grammar}3240r") / "r2", files, grammar
        )
        for grammar, files in _ROUND2.items()
    }


@pytest.mark.parametrize(
    ("grammar", "caller", "method"),
    [
        ("cpp", "hide.Kid.run", "hide.Base.label"),
        ("cpp", "hide.Kid.local", "hide.Base.label"),
        ("dart", "lib.hide.Kid.run", "lib.hide.Base.label"),
    ],
    ids=["cpp-parameter", "cpp-local", "dart-parameter"],
)
def test_a_parameter_or_local_hides_an_inherited_method(
    round2: dict[str, _Calls], grammar: str, caller: str, method: str
) -> None:
    assert _callees(round2[grammar], caller).get(method) != "exact", round2[grammar]


@pytest.mark.parametrize(
    ("grammar", "caller", "callee"),
    [
        ("cpp", "hide.Kid.plain", "hide.Base.label"),
        ("dart", "lib.hide.Kid.plain", "lib.hide.Base.label"),
        ("php", "compose.C.call", "compose.T.label"),
        ("php", "compose.C2.call", "compose.C2.LABEL"),
        ("php", "compose.CC.call", "compose.Second2.label"),
        ("dart", "lib.hide.Kid.explicit", "lib.hide.Base.label"),
    ],
    ids=[
        "cpp-unhidden",
        "dart-unhidden",
        "php-reused-trait",
        "php-folded-override",
        "php-own-composition",
        "dart-explicit-this",
    ],
)
def test_php_composes_each_class_and_unhidden_calls_stay_exact(
    round2: dict[str, _Calls], grammar: str, caller: str, callee: str
) -> None:
    assert _callees(round2[grammar], caller).get(callee) == "exact", round2[grammar]


def test_an_anonymous_php_class_composes_its_traits(
    round2: dict[str, _Calls],
) -> None:
    # `new class extends P { use T; ... }`: the class's trait wins over the
    # parent's own `label` (Greptile on PR #3240).
    calls = round2["php"]
    anon = {
        to: res
        for (src, to), res in calls.items()
        if src.startswith("compose.make.") and src.endswith(".call")
    }
    assert anon == {"compose.T.label": "exact"}, calls


# Third review round (Greptile, PR #3240). A PHP trait alias
# (`use T { T::original as label; }`) names the trait method `$this->label()`
# runs, ahead of a parent's own `label`. And a chain's first call
# (`make()->label()`) reaches an inherited method only as the call alone
# would: a parameter or local `make` is the callable the chain starts from.
_ROUND3 = {
    "php": {
        "alias.php": """\
<?php
trait T { public function original(): string { return "t"; } }
trait First { public function label(): string { return "1"; } }
trait Second { public function label(): string { return "2"; } }
class Base {
    public function label(): string { return "b"; }
    public function quiet(): string { return "b"; }
    public function picked(): string { return "b"; }
}
class Named extends Base {
    use T { T::original as label; }
    public function call(): string { return $this->label(); }
}
class Bare extends Base {
    use T { original as protected quiet; }
    public function call(): string { return $this->quiet(); }
}
class Excluded extends Base {
    use First, Second { First::label insteadof Second; Second::label as picked; }
    public function call(): string { return $this->picked(); }
}
class Inherits extends Named {
    public function call(): string { return $this->label(); }
}
class Visibility extends Base {
    use T { original as protected; }
    public function call(): string { return $this->label(); }
}
""",
    },
    "cpp": {
        "chain.cpp": """\
class A { public: int label() { return 1; } };
class Z { public: int label() { return 2; } };
class Base { public: A* make() { return nullptr; } };
class Kid : public Base {
 public:
  int run(Z* (*make)()) { return make()->label(); }
  int local() { auto make = []() -> Z* { return nullptr; }; return make()->label(); }
  int plain() { return make()->label(); }
};
""",
    },
    "dart": {
        "pubspec.yaml": 'name: shapes\nenvironment:\n  sdk: ">=3.0.0 <4.0.0"\n',
        "lib/chain.dart": """\
class A { String label() => "a"; }
class Z { String label() => "z"; }
class Base { A make() => A(); }
class Kid extends Base {
  String run(Z Function() make) => make().label();
  String explicit(Z Function() make) => this.make().label();
  String plain() => make().label();
}
""",
    },
}


@pytest.fixture(scope="module")
def round3(tmp_path_factory: pytest.TempPathFactory) -> dict[str, _Calls]:
    return {
        grammar: _calls(
            tmp_path_factory.mktemp(f"{grammar}3240r3") / "r3", files, grammar
        )
        for grammar, files in _ROUND3.items()
    }


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("alias.Named.call", "alias.T.original"),
        ("alias.Bare.call", "alias.T.original"),
        ("alias.Excluded.call", "alias.Second.label"),
        ("alias.Inherits.call", "alias.T.original"),
        ("alias.Visibility.call", "alias.Base.label"),
    ],
    ids=[
        "php-alias",
        "php-unqualified-alias",
        "php-alias-of-excluded",
        "php-parent-alias",
        "php-visibility-only",
    ],
)
def test_a_php_trait_alias_names_the_trait_method(
    round3: dict[str, _Calls], caller: str, callee: str
) -> None:
    # The alias, not the parent's same-named method, answers the call; an
    # `as` that only changes visibility renames nothing.
    assert _callees(round3["php"], caller) == {callee: "exact"}, round3["php"]


@pytest.mark.parametrize(
    ("grammar", "caller", "method"),
    [
        ("cpp", "chain.Kid.run", "chain.A.label"),
        ("cpp", "chain.Kid.local", "chain.A.label"),
        ("dart", "lib.chain.Kid.run", "lib.chain.A.label"),
    ],
    ids=["cpp-parameter-chain", "cpp-local-chain", "dart-parameter-chain"],
)
def test_a_parameter_or_local_hides_an_inherited_chain_root(
    round3: dict[str, _Calls], grammar: str, caller: str, method: str
) -> None:
    # `make` here is the parameter or local, which returns a `Z`: the
    # inherited `Base.make` returning an `A` cannot type the chain exactly.
    assert _callees(round3[grammar], caller).get(method) != "exact", round3[grammar]


@pytest.mark.parametrize(
    ("grammar", "caller", "callee"),
    [
        ("cpp", "chain.Kid.plain", "chain.A.label"),
        ("dart", "lib.chain.Kid.plain", "lib.chain.A.label"),
        ("dart", "lib.chain.Kid.explicit", "lib.chain.A.label"),
    ],
    ids=["cpp-unhidden-chain", "dart-unhidden-chain", "dart-this-chain"],
)
def test_an_unhidden_inherited_chain_root_still_types_the_chain(
    round3: dict[str, _Calls], grammar: str, caller: str, callee: str
) -> None:
    # Controls: with no parameter or local of the name, or through `this`,
    # the chain starts at the inherited `Base.make`.
    assert _callees(round3[grammar], caller).get(callee) == "exact", round3[grammar]


# Scala is left as on main for now: nothing yet reads which names a Scala
# parameter or local binds where a call sits, so a bare call there cannot be
# told from one a parameter hides (Greptile on PR #3240, #3663).
_SCALA_HIDDEN = {
    "Hide.scala": """\
package s
class A { def label(): String = "a" }
class Z { def label(): String = "z" }
class Base { def make(): A = new A() }
class Kid extends Base {
  def run(make: () => Z): String = make().label()
}
""",
}


def test_a_scala_parameter_call_never_binds_an_inherited_method_exactly(
    tmp_path: Path,
) -> None:
    calls = _calls(tmp_path / "hide", _SCALA_HIDDEN, "scala")
    assert _callees(calls, "Hide.Kid.run").get("Hide.Base.make") != "exact", calls
