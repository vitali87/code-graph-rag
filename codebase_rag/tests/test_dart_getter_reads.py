# A Dart getter access is an attribute read, not an invocation: no CALLS
# edge can ever land on one, so dead-code flagged every read-only getter
# (roughly 15 of the wonderous app's ~20 residual candidates: _enableVideo,
# _artifactRoute, startYr/endYr and kin). Mirror the C# property-read
# design: mark getter_signature methods is_property and emit REFERENCES
# edges for bare and receiver-position reads (issue #869).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

REFERENCES = cs.RelationshipType.REFERENCES.value


def _run(tmp_path: Path, files: dict[str, str]) -> MagicMock:
    parsers, queries = load_parsers()
    if "dart" not in parsers:
        pytest.skip("dart parser not available")
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock, repo_path=tmp_path, parsers=parsers, queries=queries
    ).run()
    return mock


def _rels(mock: MagicMock) -> set[tuple[str, str, str]]:
    return {
        (c.args[0][2], str(c.args[1]), c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
    }


def _has(
    rels: set[tuple[str, str, str]], caller_suffix: str, rel: str, callee_suffix: str
) -> bool:
    return any(
        a.endswith(caller_suffix) and r == rel and b.endswith(callee_suffix)
        for a, r, b in rels
    )


def test_getter_is_marked_is_property(tmp_path: Path) -> None:
    mock = _run(
        tmp_path,
        {"m.dart": "class Money {\n  bool get enabled => true;\n}\n"},
    )
    props: dict[str, dict] = {}
    for c in mock.ensure_node_batch.call_args_list:
        if c.args[0] == cs.NodeLabel.METHOD:
            props.setdefault(c.args[1][cs.KEY_QUALIFIED_NAME], {}).update(c.args[1])
    enabled = next(v for k, v in props.items() if k.endswith("Money.enabled"))
    assert enabled.get(cs.KEY_IS_PROPERTY) is True, enabled


def test_bare_getter_read_is_referenced(tmp_path: Path) -> None:
    files = {
        "app.dart": (
            "class Player {\n"
            "  bool get enableVideo => true;\n"
            "  bool get unusedFlag => false;\n"
            "  void start() {\n"
            "    if (enableVideo) {\n"
            "      run();\n"
            "    }\n"
            "  }\n"
            "  void run() {}\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert _has(rels, ".Player.start", REFERENCES, ".Player.enableVideo"), rels
    assert not _has(rels, ".Player.start", REFERENCES, ".Player.unusedFlag")


def test_receiver_getter_read_is_referenced(tmp_path: Path) -> None:
    files = {
        "app.dart": (
            "class Marker {\n"
            "  int get startYr => 1900;\n"
            "}\n"
            "\n"
            "class Timeline {\n"
            "  void draw(Marker marker) {\n"
            "    print(marker.startYr);\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert _has(rels, ".Timeline.draw", REFERENCES, ".Marker.startYr"), rels


def test_this_qualified_getter_read_is_referenced(tmp_path: Path) -> None:
    files = {
        "app.dart": (
            "class Gauge {\n"
            "  int get level => 1;\n"
            "  void show() {\n"
            "    print(this.level);\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert _has(rels, ".Gauge.show", REFERENCES, ".Gauge.level"), rels


def test_local_shadow_suppresses_bare_read(tmp_path: Path) -> None:
    # A local (or parameter) named like the getter hides it for bare reads;
    # emitting an edge here would fabricate liveness for a dead getter.
    files = {
        "app.dart": (
            "class Cart {\n"
            "  int get total => 3;\n"
            "  void tally(int total) {\n"
            "    print(total);\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert not _has(rels, ".Cart.tally", REFERENCES, ".Cart.total"), rels


def test_cascade_getter_read_is_referenced(tmp_path: Path) -> None:
    # `marker..startYr` reads through a cascade_section, not the ordinary
    # selector chain; a cascade holding an argument_part is an invocation
    # the call pass owns, and a cascade WRITE (`..endYr = 5`) targets the
    # setter, so neither may fabricate a getter read.
    files = {
        "app.dart": (
            "class Marker {\n"
            "  int get startYr => 1900;\n"
            "  int get endYr => 2000;\n"
            "  void refresh() {}\n"
            "}\n"
            "\n"
            "class Board {\n"
            "  void ping(Marker marker) {\n"
            "    marker..startYr;\n"
            "    marker..refresh();\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert _has(rels, ".Board.ping", REFERENCES, ".Marker.startYr"), rels
    assert not _has(rels, ".Board.ping", REFERENCES, ".Marker.refresh"), rels


def test_closure_shadow_does_not_suppress_outer_read(tmp_path: Path) -> None:
    # A closure's parameter named like the getter shadows it only INSIDE the
    # closure: the enclosing method's own bare read still resolves to the
    # getter and must be referenced, while the closure-internal read of the
    # parameter must not be.
    files = {
        "app.dart": (
            "class Cart {\n"
            "  int get total => 3;\n"
            "  int get untouched => 4;\n"
            "  void tally() {\n"
            "    run((int total) {\n"
            "      print(total);\n"
            "      print(untouched);\n"
            "    });\n"
            "    print(total);\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert _has(rels, ".Cart.tally", REFERENCES, ".Cart.total"), rels
    assert _has(rels, ".Cart.tally", REFERENCES, ".Cart.untouched"), rels


def test_loop_catch_and_pattern_binders_shadow_bare_reads(tmp_path: Path) -> None:
    # A loop variable, catch parameter, or pattern binding reusing a getter
    # name declares a local like any other: reads of it must not fabricate
    # liveness for the (otherwise unused) getter.
    files = {
        "app.dart": (
            "class Bag {\n"
            "  int get total => 1;\n"
            "  int get errorCode => 2;\n"
            "  int get alpha => 3;\n"
            "  void churn(List<int> xs) {\n"
            "    for (final total in xs) {\n"
            "      print(total);\n"
            "    }\n"
            "    try {\n"
            "      print(xs);\n"
            "    } catch (errorCode) {\n"
            "      print(errorCode);\n"
            "    }\n"
            "    var (alpha, beta) = (1, 2);\n"
            "    print(alpha);\n"
            "    print(beta);\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert not _has(rels, ".Bag.churn", REFERENCES, ".Bag.total"), rels
    assert not _has(rels, ".Bag.churn", REFERENCES, ".Bag.errorCode"), rels
    assert not _has(rels, ".Bag.churn", REFERENCES, ".Bag.alpha"), rels


def test_reads_before_the_binder_still_reference_the_getter(tmp_path: Path) -> None:
    # A binder is live only AFTER its declaration: the for-in ITERABLE and
    # the try BODY precede theirs, so a getter read there is genuine and
    # must not be suppressed by the statement-wide shadow.
    files = {
        "app.dart": (
            "class Scanner {\n"
            "  int get total => 1;\n"
            "  int get errorCode => 2;\n"
            "  void scan() {\n"
            "    for (final total in [total]) {\n"
            "      print(total);\n"
            "    }\n"
            "    try {\n"
            "      print(errorCode);\n"
            "    } catch (errorCode) {\n"
            "      print(errorCode);\n"
            "    }\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert _has(rels, ".Scanner.scan", REFERENCES, ".Scanner.total"), rels
    assert _has(rels, ".Scanner.scan", REFERENCES, ".Scanner.errorCode"), rels


def test_call_result_cascade_read_is_referenced(tmp_path: Path) -> None:
    # `getMarker()..startYr` reads the getter through a call-result cascade:
    # the receiver chain carries a call hop the resolver types from the
    # callee's declared return type.
    files = {
        "app.dart": (
            "class Marker {\n"
            "  int get startYr => 1900;\n"
            "}\n"
            "\n"
            "class Board {\n"
            "  Marker getMarker() {\n"
            "    return Marker();\n"
            "  }\n"
            "  void ping() {\n"
            "    getMarker()..startYr;\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert _has(rels, ".Board.ping", REFERENCES, ".Marker.startYr"), rels


def test_field_initializer_getter_read_is_referenced(tmp_path: Path) -> None:
    # A class FIELD INITIALIZER reads getters outside any method body
    # (wonderous: `late final TextStyle body = _createFont(contentFont, ...)`),
    # so neither a method caller's walk nor the module pass (which skipped
    # class subtrees) saw it, and the getter reported dead.
    files = {
        "app.dart": (
            "int wrap(int v) {\n"
            "  return v;\n"
            "}\n"
            "\n"
            "class Palette {\n"
            "  int get base => 3;\n"
            "  int get factor => 2;\n"
            "  int get unusedTone => 9;\n"
            "  late final int wrapped = wrap(base);\n"
            "  late final int scaled = factor * 2;\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert any(r == REFERENCES and b.endswith(".Palette.base") for _a, r, b in rels), (
        rels
    )
    assert any(
        r == REFERENCES and b.endswith(".Palette.factor") for _a, r, b in rels
    ), rels
    assert not any(
        r == REFERENCES and b.endswith(".Palette.unusedTone") for _a, r, b in rels
    ), rels


def test_field_initializers_resolve_against_the_owning_class(tmp_path: Path) -> None:
    # Two classes can define the same getter name: each field initializer
    # must resolve against ITS OWN class, and one class's read must not
    # dedup away the other's.
    files = {
        "app.dart": (
            "int wrap(int v) {\n"
            "  return v;\n"
            "}\n"
            "\n"
            "class Alpha {\n"
            "  int get tone => 1;\n"
            "  late final int a = wrap(tone);\n"
            "}\n"
            "\n"
            "class Beta {\n"
            "  int get tone => 2;\n"
            "  late final int b = wrap(tone);\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert any(r == REFERENCES and b.endswith("Alpha.tone") for _a, r, b in rels), rels
    assert any(r == REFERENCES and b.endswith("Beta.tone") for _a, r, b in rels), rels


def test_initializer_closure_read_is_referenced(tmp_path: Path) -> None:
    # A closure INSIDE a field initializer belongs to no method pass: its
    # body must still be walked or the getter it reads reports dead.
    files = {
        "app.dart": (
            "class Deck {\n"
            "  int get tone => 1;\n"
            "  late final int Function() pick = () {\n"
            "    return tone;\n"
            "  };\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert any(r == REFERENCES and b.endswith("Deck.tone") for _a, r, b in rels), rels


def test_getter_call_chain_is_not_double_counted(tmp_path: Path) -> None:
    # `other.total()` is an invocation the call pass already resolves; the
    # read pass must not add a REFERENCES edge for the same chain, or every
    # method call would double as a phantom property read.
    files = {
        "app.dart": (
            "class Engine {\n"
            "  void ignite() {}\n"
            "  void fire(Engine other) {\n"
            "    other.ignite();\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert not _has(rels, ".Engine.fire", REFERENCES, ".Engine.ignite"), rels


def test_method_parameter_does_not_shadow_initializer_read(tmp_path: Path) -> None:
    # A method parameter named like the getter scopes to ITS method body,
    # which the initializer walk never enters: it must not suppress a field
    # initializer's read of the getter.
    files = {
        "app.dart": (
            "class Widgeta {\n"
            "  int get tone => 1;\n"
            "  late final int value = tone;\n"
            "  void resize(int tone) {\n"
            "    print(tone);\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert any(r == REFERENCES and b.endswith("Widgeta.tone") for _a, r, b in rels), (
        rels
    )


def test_receiver_position_getter_read_is_referenced(tmp_path: Path) -> None:
    # `_wonders.length` reads the `_wonders` getter even though the FINAL
    # member (`length`) is external: the chain head in receiver position is
    # a read (issue #873, wonderous `_HomeScreenState._wonders`).
    files = {
        "app.dart": (
            "class Screen {\n"
            "  List<int> get _wonders => [1, 2];\n"
            "  int get _numWonders => _wonders.length;\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert _has(rels, ".Screen._numWonders", REFERENCES, ".Screen._wonders"), rels


def test_receiver_position_read_shadowed_by_local(tmp_path: Path) -> None:
    # A local named like the getter owns the receiver position inside its
    # scope: no read of the getter may be fabricated.
    files = {
        "app.dart": (
            "class Screen {\n"
            "  List<int> get _wonders => [1, 2];\n"
            "  int count() {\n"
            "    final _wonders = <int>[3];\n"
            "    return _wonders.length;\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert not _has(rels, ".Screen.count", REFERENCES, ".Screen._wonders"), rels


def test_cascade_receiver_getter_read_is_referenced(tmp_path: Path) -> None:
    # `_wonders..add(3)` reads the `_wonders` getter to obtain the cascade
    # receiver; the cascaded call itself belongs to the call pass.
    files = {
        "app.dart": (
            "class Screen {\n"
            "  List<int> get _wonders => [1, 2];\n"
            "  void fill() {\n"
            "    _wonders..add(3);\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert _has(rels, ".Screen.fill", REFERENCES, ".Screen._wonders"), rels


def test_invoked_chain_head_is_not_a_property_read(tmp_path: Path) -> None:
    # `_wonders(3)` on a getter returning a callable is an invocation the
    # call pass owns (it emits the CALLS edge that keeps the getter alive):
    # a head followed directly by an argument_part must stay out of the
    # read pass or every such site would double as a phantom read.
    files = {
        "app.dart": (
            "class Screen {\n"
            "  int Function(int) get _wonders => (n) => n;\n"
            "  void fill() {\n"
            "    _wonders(3);\n"
            "  }\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert not _has(rels, ".Screen.fill", REFERENCES, ".Screen._wonders"), rels
    assert _has(rels, ".Screen.fill", "CALLS", ".Screen._wonders"), rels


def test_construction_receiver_getter_read_is_referenced(tmp_path: Path) -> None:
    # A member read on a construction receiver names the member on the
    # constructed class, for the `new` and type-argument forms as well as
    # the bare one (issue #2015). The call pass already types the same
    # receiver, which is why `Box<int>(1).describe()` binds; the read pass
    # lost the `new` form to an unhandled new_expression base and the
    # generic form to a `<`/`>` relational mis-parse.
    files = {
        "app.dart": (
            "class Box<T> {\n"
            "  Box(T v);\n"
            "  int get height => 2;\n"
            "  String describe() => 'b';\n"
            "}\n"
            "int bareGet() { return Box(1).height; }\n"
            "int newGet() { return new Box(1).height; }\n"
            "int genGet() { return Box<int>(1).height; }\n"
            "void chained() { Box<int>(1).describe(); }\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    # Control: the bare form and the generic CALL already bind on main.
    assert _has(rels, ".app.bareGet", REFERENCES, ".Box.height"), rels
    assert _has(rels, ".app.chained", "CALLS", ".Box.describe"), rels
    assert _has(rels, ".app.newGet", REFERENCES, ".Box.height"), rels
    assert _has(rels, ".app.genGet", REFERENCES, ".Box.height"), rels


def test_real_comparison_is_not_read_as_a_construction(tmp_path: Path) -> None:
    # `X<int>(1).m` and the chained comparison `a < b > (1).m` parse
    # identically, so the construction reading is a guess (issue #2015).
    # Both operator orders are covered, and `flipped`/`same` deliberately
    # use a LOCAL NAMED AFTER THE CLASS: that is the only shape in which a
    # fabricated receiver resolves, so a weaker fixture would pass either
    # way. A local or parameter of that name means the operand is that
    # variable, not a type, so no member may bind.
    files = {
        "app.dart": (
            "class Box {\n"
            "  int get height => 2;\n"
            "  int get width => 3;\n"
            "}\n"
            "dynamic same(dynamic Box, dynamic b) {\n"
            "  return Box < b > (1).height;\n"
            "}\n"
            "dynamic flipped(dynamic Box, dynamic b) {\n"
            "  return Box > b < (1).height;\n"
            "}\n"
            "bool nested(int a, int b, Box lo) {\n"
            "  return (a < b).hashCode > lo.width;\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    # The genuine receiver read still binds...
    assert _has(rels, ".app.nested", REFERENCES, ".Box.width"), rels
    # ...and neither comparison invents a construction receiver.
    assert not _has(rels, ".app.same", REFERENCES, ".Box.height"), rels
    assert not _has(rels, ".app.flipped", REFERENCES, ".Box.height"), rels


def test_const_construction_receiver_read_is_referenced(tmp_path: Path) -> None:
    # `const X(1).m` is a const_object_expression, a DIFFERENT node type
    # from `new X(1).m`'s new_expression (issue #2015); both carry the class
    # as their first type_identifier.
    files = {
        "app.dart": (
            "class Box {\n"
            "  const Box(int v);\n"
            "  int get height => 2;\n"
            "}\n"
            "int constGet() { return const Box(1).height; }\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert _has(rels, ".app.constGet", REFERENCES, ".Box.height"), rels


def test_import_prefixed_construction_receiver_keeps_the_class_hop(
    tmp_path: Path,
) -> None:
    # An import-prefixed construction (`p.Box<int>(1).height`,
    # `new p.Box(1).height`) must name the member on the CLASS, not on the
    # prefix (issue #2033). Both shapes put the prefix where the unprefixed
    # ones put the class: `new p.Box(1)` is type_identifier(p) +
    # identifier(Box), and the mis-parsed generic's left spine leads with
    # the prefix identifier and carries `.Box` as a selector.
    files = {
        "lib.dart": ("class Box<T> {\n  Box(T v);\n  int get height => 2;\n}\n"),
        "app.dart": (
            "import 'lib.dart' as p;\n"
            "int genGet() { return p.Box<int>(1).height; }\n"
            "int newGet() { return new p.Box(1).height; }\n"
            "int bareGet() { return p.Box(1).height; }\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    # Control: the bare prefixed form already keeps its class hop on main.
    assert _has(rels, ".app.bareGet", REFERENCES, ".Box.height"), rels
    assert _has(rels, ".app.genGet", REFERENCES, ".Box.height"), rels
    assert _has(rels, ".app.newGet", REFERENCES, ".Box.height"), rels


def test_an_import_alias_does_not_displace_another_import(tmp_path: Path) -> None:
    # Registering the `as` prefix (issue #2033) must not overwrite a key an
    # earlier import already owns. `import 'helper.dart';` keys on the file
    # name, and `import 'other.dart' as helper;` claims the same string, so
    # writing the alias unconditionally dropped the FIRST import entirely:
    # both IMPORTS edges pointed at other.dart and helper.dart was lost.
    files = {
        "helper.dart": "class H {\n  int get v => 1;\n}\n",
        "other.dart": "class O {\n  int get v => 2;\n}\n",
        "app.dart": (
            "import 'helper.dart';\n"
            "import 'other.dart' as helper;\n"
            "int f() { return H().v; }\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    imports = {b for a, r, b in rels if r == "IMPORTS" and a.endswith(".app")}
    assert any(q.endswith(".helper") for q in imports), imports
    assert any(q.endswith(".other") for q in imports), imports


def test_a_shadowed_prefix_is_not_read_as_a_construction(tmp_path: Path) -> None:
    # The shadow check that stops a comparison being read as a construction
    # (issue #2015) keys on the BARE binder, but an import-prefixed base is
    # dotted (`p.Box`, issue #2033). A parameter named `p` must still make
    # `p.Box < b > (1).height` a comparison; looking the dotted name up in
    # the shadow spans would never match and the wrong edge would return.
    files = {
        "lib.dart": "class Box {\n  int get height => 2;\n}\n",
        "app.dart": (
            "import 'lib.dart' as p;\n"
            "dynamic cmp(dynamic p, dynamic b) {\n"
            "  return p.Box < b > (1).height;\n"
            "}\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert not _has(rels, ".app.cmp", REFERENCES, ".Box.height"), rels


def test_a_prefixed_factory_function_types_the_chain_by_its_return(
    tmp_path: Path,
) -> None:
    # A hop after an import prefix need not be a constructor: `p.make()` is a
    # FUNCTION, so the fold types the chain from its recorded return type
    # instead of treating `make` as a class (issue #2033). This is the
    # positive case for that branch -- the factory guard test asserts only an
    # absence, which a fold-nothing implementation would also satisfy.
    files = {
        "lib.dart": (
            "class Thing {\n  int get v => 1;\n}\n\nThing make() { return Thing(); }\n"
        ),
        "app.dart": ("import 'lib.dart' as p;\nint f() { return p.make().v; }\n"),
    }
    rels = _rels(_run(tmp_path, files))
    assert _has(rels, ".app.f", "CALLS", ".lib.make"), rels
    assert _has(rels, ".app.f", REFERENCES, ".Thing.v"), rels


def test_a_named_constructor_is_not_part_of_the_receiver_type(
    tmp_path: Path,
) -> None:
    # `new Box.named(1)` constructs a Box, so the read binds Box.height. The
    # grammar separates the shapes by node type -- an import prefix is a
    # type_identifier, a named constructor an identifier -- and joining both
    # made the receiver type `Box.named`, which resolves to nothing
    # (issue #2033). The prefixed form keeps its prefix.
    files = {
        "lib.dart": (
            "class Box {\n"
            "  Box(int v);\n"
            "  Box.named(int v);\n"
            "  int get height => 2;\n"
            "}\n"
        ),
        "app.dart": (
            "import 'lib.dart';\n"
            "import 'lib.dart' as p;\n"
            "int named() { return new Box.named(1).height; }\n"
            "int constNamed() { return const Box.named(1).height; }\n"
            "int prefixedNamed() { return new p.Box.named(1).height; }\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert _has(rels, ".app.named", REFERENCES, ".Box.height"), rels
    assert _has(rels, ".app.constNamed", REFERENCES, ".Box.height"), rels
    assert _has(rels, ".app.prefixedNamed", REFERENCES, ".Box.height"), rels


def test_a_local_shadowing_an_import_prefix_is_not_folded(tmp_path: Path) -> None:
    # A parameter named after the import prefix SHADOWS it, so
    # `p.Box(1).height` reads a member of that parameter, not of the imported
    # class, and must emit no edge (issue #2033). Folding unconditionally
    # resolved it through `import ... as p` and invented an edge where the
    # resolver had emitted none.
    #
    # Known gap, not asserted here: an untyped Dart local (`var p = 1`)
    # never reaches local_var_types, so this guard does not fire for it and
    # the fold still resolves through the import. The name IS available --
    # _dart_declared_names binds it syntactically -- so closing this means
    # threading the call processor's shadow spans into resolution, not new
    # analysis.
    #
    # Separately, the bare `p.Box.named(1).height` spelling (no `new`) has
    # never resolved on any branch; filed as #2084.
    files = {
        "lib.dart": "class Box {\n  Box(int v);\n  int get height => 2;\n}\n",
        "app.dart": (
            "import 'lib.dart' as p;\n"
            "int shadowed(dynamic p) { return p.Box(1).height; }\n"
            "int unshadowed() { return p.Box(1).height; }\n"
        ),
    }
    rels = _rels(_run(tmp_path, files))
    assert not _has(rels, ".app.shadowed", REFERENCES, ".Box.height"), rels
    # Control: the unshadowed prefix still resolves, so the guard is not
    # simply switching the fold off.
    assert _has(rels, ".app.unshadowed", REFERENCES, ".Box.height"), rels
