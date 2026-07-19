# (H) Dart receiver typing (follow-up to PR #804): a member call on a typed
# (H) receiver (`g.greet()` where g is a declared local, a construction-bound
# (H) local, a parameter, or a class field) resolves through the generic
# (H) local-type machinery once Dart supplies a variable-type map, exactly
# (H) like Java/C#/C++/Go. Construction typing keys off the UpperCamelCase
# (H) base identifier of `Base(...)` / `Base.named(...)` initializers.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import run_updater

SKIP = "dart"

APP_DART = """
class Greeter {
  String name;
  Greeter(this.name);
  Greeter.named(String n) : name = n;

  String greet() {
    return name;
  }

  String hail() {
    return name;
  }

  static Greeter create() {
    return Greeter('s');
  }
}

class Shouter {
  Shouter();

  String greet() {
    return 'LOUD';
  }

  String hail() {
    return 'LOUD';
  }
}

class Animal {
  String speak() {
    return 'a';
  }
}

class Dog extends Animal {
  Dog();

  String bark() {
    return 'b';
  }
}

class OtherSpeaker {
  String speak() {
    return 'o';
  }
}

class Registry {
  Registry();

  static String describe() {
    return 'r';
  }

  String evict() {
    return 'e';
  }
}

class Widgetry {
  Widgetry();

  static void configure() {
    return;
  }

  String render() {
    return 'w';
  }
}

class OtherRenderer {
  String render() {
    return 'o';
  }

  String evict() {
    return 'o';
  }
}

class Holder {
  Greeter buddy;
  Holder(this.buddy);

  String viaField() {
    return buddy.greet();
  }

  String viaThisField() {
    return this.buddy.hail();
  }
}

Greeter makeIt() {
  return Greeter('m');
}

String useParam(Greeter g) {
  return g.greet();
}

String useDeclared() {
  Greeter t = makeIt();
  return t.greet();
}

String useConstructed() {
  var b = Greeter('x');
  return b.greet();
}

String useNamedCtor() {
  final n = Greeter.named('y');
  return n.hail();
}

String useUntypedHelper() {
  var h = lowercaseFactory();
  return h.greet();
}

String useStaticFactory() {
  var s = Greeter.create();
  return s.greet();
}

String useInherited(Dog d) {
  return d.speak();
}

String misuseFactory() {
  var r = Registry.describe();
  return r.evict();
}

String useDeclaredWins() {
  Widgetry w = Widgetry.configure();
  return w.render();
}

String outerEnrich() {
  var r = Registry();
  void innerBind() {
    var r = Registry.describe();
    r.evict();
  }
  innerBind();
  return r.evict();
}

String outerScoped() {
  var s = Greeter('o');
  void inner() {
    var s = Shouter();
    return s.greet();
  }
  inner();
  return s.greet();
}

String useMultiDeclaration() {
  var first = Greeter('a'), second = Shouter();
  Greeter third = makeIt(), fourth = makeIt();
  return second.greet() + fourth.hail();
}

Greeter lowercaseFactory() {
  return Greeter('z');
}

String useConstructionChain() {
  return Greeter('x').greet();
}

String useFactoryChain() {
  return lowercaseFactory().hail();
}

String useInheritedConstructionChain() {
  return Dog().speak();
}
"""


@pytest.fixture
def dart_typed_project(temp_repo: Path) -> Path:
    root = temp_repo / "dtyped"
    root.mkdir()
    (root / "app.dart").write_text(APP_DART, encoding="utf-8")
    return root


def _calls(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == cs.RelationshipType.CALLS.value
    }


def _has(edges: set[tuple[str, str]], src: str, dst: str) -> bool:
    return any(s.endswith(src) and d.endswith(dst) for s, d in edges)


def test_param_typed_receiver(dart_typed_project: Path, mock_ingestor: MagicMock):
    # (H) Shouter.greet is a same-named decoy in every test here: bare
    # (H) suffix-trie binding cannot disambiguate, only receiver typing can.
    run_updater(dart_typed_project, mock_ingestor, skip_if_missing=SKIP)
    calls = _calls(mock_ingestor)
    assert _has(calls, ".app.useParam", ".Greeter.greet"), sorted(calls)
    assert not _has(calls, ".app.useParam", ".Shouter.greet"), sorted(calls)


def test_declared_local_receiver(dart_typed_project: Path, mock_ingestor: MagicMock):
    run_updater(dart_typed_project, mock_ingestor, skip_if_missing=SKIP)
    calls = _calls(mock_ingestor)
    assert _has(calls, ".app.useDeclared", ".Greeter.greet"), sorted(calls)
    assert not _has(calls, ".app.useDeclared", ".Shouter.greet"), sorted(calls)


def test_construction_bound_receiver(
    dart_typed_project: Path, mock_ingestor: MagicMock
):
    run_updater(dart_typed_project, mock_ingestor, skip_if_missing=SKIP)
    calls = _calls(mock_ingestor)
    assert _has(calls, ".app.useConstructed", ".Greeter.greet"), sorted(calls)
    assert not _has(calls, ".app.useConstructed", ".Shouter.greet"), sorted(calls)
    assert _has(calls, ".app.useNamedCtor", ".Greeter.hail"), sorted(calls)
    assert not _has(calls, ".app.useNamedCtor", ".Shouter.hail"), sorted(calls)


def test_field_typed_receiver(dart_typed_project: Path, mock_ingestor: MagicMock):
    run_updater(dart_typed_project, mock_ingestor, skip_if_missing=SKIP)
    calls = _calls(mock_ingestor)
    # (H) bare field receiver and explicit this.field receiver both hop
    # (H) through the field's declared type
    assert _has(calls, ".Holder.viaField", ".Greeter.greet"), sorted(calls)
    assert not _has(calls, ".Holder.viaField", ".Shouter.greet"), sorted(calls)
    assert _has(calls, ".Holder.viaThisField", ".Greeter.hail"), sorted(calls)


def test_nested_local_function_does_not_poison_outer_scope(
    dart_typed_project: Path, mock_ingestor: MagicMock
):
    run_updater(dart_typed_project, mock_ingestor, skip_if_missing=SKIP)
    calls = _calls(mock_ingestor)
    # (H) inner()'s same-named `s` (a Shouter) must not conflict-drop the
    # (H) outer `s` (a Greeter) from the outer caller's type map
    # (H) (PR #806 review round 3)
    assert _has(calls, ".app.outerScoped", ".Greeter.greet"), sorted(calls)
    # (H) a local function registers flat (app.inner) per the Dart FQN spec
    # (H) and its own map types ITS s as Shouter
    assert _has(calls, ".app.inner", ".Shouter.greet"), sorted(calls)


def test_multi_variable_declarations_type_every_binding(
    dart_typed_project: Path, mock_ingestor: MagicMock
):
    run_updater(dart_typed_project, mock_ingestor, skip_if_missing=SKIP)
    calls = _calls(mock_ingestor)
    # (H) additional variables of a multi-declaration nest as
    # (H) initialized_identifier children; `second` takes its OWN
    # (H) construction's type, `fourth` the shared declared type
    # (H) (PR #806 review).
    assert _has(calls, ".app.useMultiDeclaration", ".Shouter.greet"), sorted(calls)
    assert not _has(calls, ".app.useMultiDeclaration", ".Greeter.greet"), sorted(calls)
    assert _has(calls, ".app.useMultiDeclaration", ".Greeter.hail"), sorted(calls)


def test_static_factory_return_types_the_local(
    dart_typed_project: Path, mock_ingestor: MagicMock
):
    run_updater(dart_typed_project, mock_ingestor, skip_if_missing=SKIP)
    calls = _calls(mock_ingestor)
    # (H) `var s = Greeter.create()` types s from create's recorded return
    # (H) type; the Shouter.greet decoy defeats both the suffix trie and the
    # (H) unique-member gate, so only return typing can bind this
    assert _has(calls, ".app.useStaticFactory", ".Greeter.greet"), sorted(calls)
    assert not _has(calls, ".app.useStaticFactory", ".Shouter.greet"), sorted(calls)


def test_non_class_static_return_does_not_type_the_local(
    dart_typed_project: Path, mock_ingestor: MagicMock
):
    run_updater(dart_typed_project, mock_ingestor, skip_if_missing=SKIP)
    calls = _calls(mock_ingestor)
    # (H) Registry.describe() returns a String, not a Registry: the recorded
    # (H) return type must override the construction heuristic, so `r` is a
    # (H) String and `r.evict()` must NOT bind Registry.evict
    assert not _has(calls, ".app.misuseFactory", ".Registry.evict"), sorted(calls)
    # (H) the static call itself still resolves
    assert _has(calls, ".app.misuseFactory", ".Registry.describe"), sorted(calls)


def test_declared_type_wins_over_initializer_return(
    dart_typed_project: Path, mock_ingestor: MagicMock
):
    run_updater(dart_typed_project, mock_ingestor, skip_if_missing=SKIP)
    calls = _calls(mock_ingestor)
    # (H) an EXPLICIT declared type statically fixes the variable's type; the
    # (H) initializer's recorded (void) return must not untype it
    # (H) (PR #807 review). OtherRenderer.render is the decoy.
    assert _has(calls, ".app.useDeclaredWins", ".Widgetry.render"), sorted(calls)
    assert not _has(calls, ".app.useDeclaredWins", ".OtherRenderer.render"), sorted(
        calls
    )


def test_nested_binding_does_not_retype_outer_local(
    dart_typed_project: Path, mock_ingestor: MagicMock
):
    run_updater(dart_typed_project, mock_ingestor, skip_if_missing=SKIP)
    calls = _calls(mock_ingestor)
    # (H) an inner function's same-named `var r = Registry.describe()` must
    # (H) not retype the OUTER r (a constructed Registry) to String
    # (H) (PR #807 review). OtherRenderer.evict is the decoy.
    assert _has(calls, ".app.outerEnrich", ".Registry.evict"), sorted(calls)


def test_inherited_method_on_typed_receiver(
    dart_typed_project: Path, mock_ingestor: MagicMock
):
    run_updater(dart_typed_project, mock_ingestor, skip_if_missing=SKIP)
    calls = _calls(mock_ingestor)
    # (H) speak() is defined on Animal, the receiver is typed Dog: lookup must
    # (H) walk the inheritance chain; OtherSpeaker.speak is the decoy
    assert _has(calls, ".app.useInherited", ".Animal.speak"), sorted(calls)
    assert not _has(calls, ".app.useInherited", ".OtherSpeaker.speak"), sorted(calls)


def test_construction_temporary_chain_resolves(
    dart_typed_project: Path, mock_ingestor: MagicMock
):
    run_updater(dart_typed_project, mock_ingestor, skip_if_missing=SKIP)
    calls = _calls(mock_ingestor)
    # (H) `Greeter('x').greet()` invokes a method on a construction temporary;
    # (H) the receiver is a call result, so only chained typing off the ctor
    # (H) can bind it. Shouter.greet is the decoy.
    assert _has(calls, ".app.useConstructionChain", ".Greeter.greet"), sorted(calls)
    assert not _has(calls, ".app.useConstructionChain", ".Shouter.greet"), sorted(
        calls
    )


def test_factory_return_chain_resolves(
    dart_typed_project: Path, mock_ingestor: MagicMock
):
    run_updater(dart_typed_project, mock_ingestor, skip_if_missing=SKIP)
    calls = _calls(mock_ingestor)
    # (H) `lowercaseFactory().hail()` types the receiver from the factory's
    # (H) recorded return type (Greeter), then binds hail on it
    assert _has(calls, ".app.useFactoryChain", ".Greeter.hail"), sorted(calls)
    assert not _has(calls, ".app.useFactoryChain", ".Shouter.hail"), sorted(calls)


def test_inherited_method_on_construction_chain(
    dart_typed_project: Path, mock_ingestor: MagicMock
):
    run_updater(dart_typed_project, mock_ingestor, skip_if_missing=SKIP)
    calls = _calls(mock_ingestor)
    # (H) `Dog().speak()` where speak is defined on Animal: chained typing must
    # (H) walk the inheritance chain. OtherSpeaker.speak is the decoy.
    assert _has(calls, ".app.useInheritedConstructionChain", ".Animal.speak"), sorted(
        calls
    )
    assert not _has(
        calls, ".app.useInheritedConstructionChain", ".OtherSpeaker.speak"
    ), sorted(calls)


def test_lowercase_initializer_does_not_type(
    dart_typed_project: Path, mock_ingestor: MagicMock
):
    run_updater(dart_typed_project, mock_ingestor, skip_if_missing=SKIP)
    calls = _calls(mock_ingestor)
    # (H) `var h = lowercaseFactory()` is a call, not a construction: the base
    # (H) identifier is not UpperCamelCase, so h stays untyped and the
    # (H) ambiguous `h.greet()` binds NEITHER candidate
    assert not _has(calls, ".app.useUntypedHelper", ".Greeter.greet"), sorted(calls)
    assert not _has(calls, ".app.useUntypedHelper", ".Shouter.greet"), sorted(calls)
    # (H) the construction inside lowercaseFactory itself still resolves
    assert _has(calls, ".app.lowercaseFactory", ".Greeter.Greeter"), sorted(calls)
