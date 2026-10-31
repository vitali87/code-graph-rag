"""TypeScript, Dart and Python enums get `EnumVariant` nodes and
`HAS_VARIANT` edges like Rust, Java, C/C++, C# and PHP already did
(issue #2583). Same node properties, same `<enum qn>.<name>` qualified name,
same `{index}` on the edge, same `enum_variants` capture gate.

Python is the one owner that is not an `Enum` node: an `enum.Enum` subclass
stays a `Class` (it inherits, is called and carries methods like any class,
and the Python resolvers read it as one), so its variants hang off that
`Class`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import RELATIONSHIP_SCHEMAS
from evals.cgr_graph import _StatefulIngestor

SOURCES: dict[str, str] = {
    "colors.ts": (
        "export enum Color {\n"
        "  Red,\n"
        "  /** The green one. */\n"
        "  Green = 5,\n"
        '  "Blue-ish" = "b",\n'
        "}\n"
        "const enum Flags { A = 1 << 0, B }\n"
        "declare enum Ambient { X }\n"
        "export const Palette = { Red: 1, Green: 2 } as const;\n"
    ),
    "size.tsx": 'export enum Size { Small, Large = "L" }\n',
    "status.dart": (
        "enum Status {\n"
        "  /// Active one.\n"
        "  active,\n"
        "  inactive,\n"
        "}\n"
        "\n"
        "enum Planet implements Comparable<Planet> {\n"
        "  earth(1.0),\n"
        "  mars(0.5),\n"
        "  venus.named(2);\n"
        "\n"
        "  const Planet(this.g);\n"
        "  const Planet.named(this.g);\n"
        "  final double g;\n"
        "  static const Planet home = earth;\n"
        "  int compareTo(Planet other) => 0;\n"
        "}\n"
    ),
    "mood.py": (
        "import enum\n"
        "from enum import Enum, auto, member, nonmember\n"
        "from enum import IntFlag as IF\n"
        "\n"
        "\n"
        "class Mood(Enum):\n"
        '    """Moods."""\n'
        "\n"
        '    _ignore_ = ["tmp", "scratch"]\n'
        "    HAPPY = 1\n"
        "    SAD: int = 2\n"
        "    LATER = auto()\n"
        "    A, B = 3, 4\n"
        "    C = D = 5\n"
        "    tmp = 9\n"
        "    scratch = 10\n"
        "    __dunder__ = 11\n"
        "    _sunder_ = 12\n"
        "    __private = 13\n"
        "    ANNOTATED_ONLY: int\n"
        "    lam = lambda self: 1\n"
        "    prop = property(lambda self: 1)\n"
        "    stat = staticmethod(len)\n"
        "    klass = classmethod(len)\n"
        "    kept_out = nonmember(14)\n"
        "    kept_out_too = enum.nonmember(15)\n"
        "    EXPLICIT = member(len)\n"
        "\n"
        "    def method(self):\n"
        "        return 1\n"
        "\n"
        "    @property\n"
        "    def described(self):\n"
        "        return 2\n"
        "\n"
        "    class Inner:\n"
        "        NESTED = 1\n"
        "\n"
        "\n"
        "class Period(enum.IntEnum):\n"
        '    _ignore_ = "day, week"\n'
        "    day = 1\n"
        "    week = 7\n"
        "    MONDAY = 1\n"
        "\n"
        "\n"
        "class Perm(IF):\n"
        "    R = 4\n"
        "    W = 2\n"
        "\n"
        "\n"
        "class Shade(str, Enum):\n"
        '    DARK = "dark"\n'
    ),
    "flags.py": (
        "import enum as en\n"
        "from enum import StrEnum\n"
        "\n"
        "\n"
        "class Bits(en.Flag, boundary=en.STRICT):\n"
        "    ONE = 1\n"
        "\n"
        "\n"
        "@en.unique\n"
        "class Word(StrEnum):\n"
        '    HI = "hi"\n'
    ),
    "plain.py": ("class Plain:\n    RED = 1\n    GREEN = 2\n"),
    # Quoted member names spelled with escapes: the member is the string the
    # runtime reads (`Esc["ab"]`), whether it has an initializer or not.
    "escaped.ts": (
        "export enum Esc {\n"
        '  "a\\u0062" = 1,\n'
        '  "c\\x64",\n'
        '  "e\\u{66}" = 4,\n'
        '  "line\\\ncont" = 5,\n'
        '  "\\uD83D\\uDE00" = 7,\n'
        '  "q\\"uote" = 8,\n'
        "  'sq' = 9,\n"
        "}\n"
    ),
    # Membership follows what a call refers to, not how it is spelled: an
    # aliased descriptor is still a descriptor, and a first-party function
    # that happens to be named `nonmember` is not `enum.nonmember`.
    "descriptors.py": (
        "import enum\n"
        "import functools\n"
        "from functools import cached_property as cp\n"
        "\n"
        "\n"
        "def nonmember(value):\n"
        "    return value\n"
        "\n"
        "\n"
        "class Mode(enum.Enum):\n"
        "    REAL = nonmember(1)\n"
        "    ALSO = 2\n"
        "    p = cp(lambda self: 3)\n"
        "    q = functools.cached_property(lambda self: 4)\n"
        "    r = enum.nonmember(5)\n"
        "    s = enum.property(lambda self: 6)\n"
    ),
    # A first-party class that merely shares the stdlib name, in a module
    # that never imports `enum`: its subclass is not an enum.
    "fake.py": ("class Enum:\n    pass\n\n\nclass Fake(Enum):\n    NOT_A_MEMBER = 1\n"),
}

LABEL = cs.NodeLabel.ENUM_VARIANT.value
REL = cs.RelationshipType.HAS_VARIANT.value

_LANGUAGES = (
    cs.SupportedLanguage.TS,
    cs.SupportedLanguage.TSX,
    cs.SupportedLanguage.DART,
    cs.SupportedLanguage.PYTHON,
)


def _index(tmp_path: Path, tokens: list[str]) -> _StatefulIngestor:
    repo = tmp_path / "proj"
    repo.mkdir(parents=True)
    for name, src in SOURCES.items():
        (repo / name).write_text(src, encoding="utf-8")
    parsers, queries = load_parsers()
    missing = [lang for lang in _LANGUAGES if lang not in parsers]
    if missing:
        pytest.skip(f"{missing[0]} parser not available")
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture(tokens),
    ).run(force=True)
    return store


def _variants(store: _StatefulIngestor, owner_qn: str) -> dict[str, dict]:
    """The owner's variants, in the order the index assigned them."""
    found = {
        str(props[cs.KEY_NAME]): props
        for (label, _uid), props in store.nodes.items()
        if label == LABEL
        and str(props[cs.KEY_QUALIFIED_NAME]).startswith(f"{owner_qn}.")
        and str(props[cs.KEY_QUALIFIED_NAME])[len(owner_qn) + 1 :]
        == str(props[cs.KEY_NAME])
    }
    return dict(sorted(found.items(), key=lambda kv: kv[1][cs.KEY_INDEX]))


def _owner_edges(
    store: _StatefulIngestor, owner_qn: str
) -> list[tuple[str, str, str, str, str]]:
    return [edge for edge in store.edges if edge[2] == REL and edge[1] == owner_qn]


def _label_of(store: _StatefulIngestor, qn: str) -> set[str]:
    return {
        label
        for (label, _uid), props in store.nodes.items()
        if props.get(cs.KEY_QUALIFIED_NAME) == qn
    }


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory) -> _StatefulIngestor:
    return _index(tmp_path_factory.mktemp("enums2583"), ["+enum_variants", "+fields"])


# --- Red: the three languages the issue names -------------------------------


@pytest.mark.parametrize(
    ("owner_qn", "owner_label", "names", "values"),
    [
        (
            "proj.colors.Color",
            cs.NodeLabel.ENUM,
            ["Red", "Green", "Blue-ish"],
            {"Green": "5", "Blue-ish": '"b"'},
        ),
        ("proj.colors.Flags", cs.NodeLabel.ENUM, ["A", "B"], {"A": "1 << 0"}),
        ("proj.colors.Ambient", cs.NodeLabel.ENUM, ["X"], {}),
        ("proj.size.Size", cs.NodeLabel.ENUM, ["Small", "Large"], {"Large": '"L"'}),
        ("proj.status.Status", cs.NodeLabel.ENUM, ["active", "inactive"], {}),
        # Enhanced enum: constructor arguments are a constructor call, not a
        # discriminant, so no `value` -- Java's constants record none either.
        ("proj.status.Planet", cs.NodeLabel.ENUM, ["earth", "mars", "venus"], {}),
        (
            "proj.mood.Mood",
            cs.NodeLabel.CLASS,
            ["HAPPY", "SAD", "LATER", "A", "B", "C", "D", "EXPLICIT"],
            {
                "HAPPY": "1",
                "SAD": "2",
                "LATER": "auto()",
                "A": "3",
                "B": "4",
                "C": "5",
                "D": "5",
                "EXPLICIT": "member(len)",
            },
        ),
        ("proj.mood.Period", cs.NodeLabel.CLASS, ["MONDAY"], {"MONDAY": "1"}),
        ("proj.mood.Perm", cs.NodeLabel.CLASS, ["R", "W"], {"R": "4", "W": "2"}),
        ("proj.mood.Shade", cs.NodeLabel.CLASS, ["DARK"], {"DARK": '"dark"'}),
        ("proj.flags.Bits", cs.NodeLabel.CLASS, ["ONE"], {"ONE": "1"}),
        ("proj.flags.Word", cs.NodeLabel.CLASS, ["HI"], {"HI": '"hi"'}),
    ],
    ids=[
        "ts-enum",
        "ts-const-enum",
        "ts-declare-enum",
        "tsx-enum",
        "dart-enum",
        "dart-enhanced-enum",
        "py-enum",
        "py-intenum-ignore-string",
        "py-intflag-alias",
        "py-str-mixin",
        "py-module-alias-flag",
        "py-strenum",
    ],
)
def test_every_member_becomes_a_variant_in_declaration_order(
    indexed: _StatefulIngestor,
    owner_qn: str,
    owner_label: cs.NodeLabel,
    names: list[str],
    values: dict[str, str],
) -> None:
    variants = _variants(indexed, owner_qn)
    assert list(variants) == names, list(variants)
    for index, name in enumerate(names):
        props = variants[name]
        assert props[cs.KEY_QUALIFIED_NAME] == f"{owner_qn}.{name}"
        assert props[cs.KEY_INDEX] == index
        assert props[cs.KEY_START_LINE] >= 1
        assert props[cs.KEY_PATH]
        assert Path(props[cs.KEY_ABSOLUTE_PATH]).is_absolute()
        if name in values:
            assert props[cs.KEY_VALUE] == values[name], props
        else:
            assert cs.KEY_VALUE not in props, props
    edges = _owner_edges(indexed, owner_qn)
    assert sorted(e[4] for e in edges) == sorted(f"{owner_qn}.{n}" for n in names)
    for edge in edges:
        assert edge[0] == owner_label.value, edge
        assert edge[3] == LABEL, edge
        name = edge[4][len(owner_qn) + 1 :]
        assert indexed.props_for(edge)[cs.KEY_INDEX] == names.index(name), edge


def test_a_variant_is_positioned_at_its_name(indexed: _StatefulIngestor) -> None:
    # `  Green = 5,` is line 4 of colors.ts; `    SAD: int = 2` line 11 of
    # mood.py; the Dart `  mars(0.5),` line 9.
    green = _variants(indexed, "proj.colors.Color")["Green"]
    assert (green[cs.KEY_START_LINE], green[cs.KEY_START_COL]) == (4, 2)
    sad = _variants(indexed, "proj.mood.Mood")["SAD"]
    assert (sad[cs.KEY_START_LINE], sad[cs.KEY_START_COL]) == (11, 4)
    b = _variants(indexed, "proj.mood.Mood")["B"]
    assert (b[cs.KEY_START_LINE], b[cs.KEY_START_COL]) == (13, 7)
    mars = _variants(indexed, "proj.status.Planet")["mars"]
    assert (mars[cs.KEY_START_LINE], mars[cs.KEY_START_COL]) == (9, 2)


def test_a_variant_carries_its_doc_comment(indexed: _StatefulIngestor) -> None:
    assert (
        _variants(indexed, "proj.colors.Color")["Green"][cs.KEY_DOCSTRING]
        == "The green one."
    )
    assert (
        _variants(indexed, "proj.status.Status")["active"][cs.KEY_DOCSTRING]
        == "Active one."
    )
    assert cs.KEY_DOCSTRING not in _variants(indexed, "proj.colors.Color")["Red"]


def test_every_new_variant_edge_is_on_the_documented_schema(
    indexed: _StatefulIngestor,
) -> None:
    # graph_audit flags an edge whose (source, type, target) triple the
    # schema does not list; the Python owner is a Class, so the schema has
    # to name it.
    allowed = {
        (source.value, schema.rel_type.value, target.value)
        for schema in RELATIONSHIP_SCHEMAS
        for source in schema.sources
        for target in schema.targets
    }
    edges = [e for e in indexed.edges if e[2] == REL]
    assert edges
    off_schema = [e for e in edges if (e[0], e[2], e[3]) not in allowed]
    assert off_schema == []


# --- Negative: what must stay out, or stay as it was ------------------------


def test_the_default_index_emits_no_variant_for_these_languages(
    tmp_path: Path,
) -> None:
    store = _index(tmp_path, [])
    assert not [1 for (label, _uid) in store.nodes if label == LABEL]
    assert not [1 for e in store.edges if e[2] == REL]


def test_python_enum_non_members_are_not_variants(indexed: _StatefulIngestor) -> None:
    names = set(_variants(indexed, "proj.mood.Mood"))
    for excluded in (
        # the enum's own control attribute and the names it lists
        "_ignore_",
        "tmp",
        "scratch",
        # reserved and private names
        "__dunder__",
        "_sunder_",
        "__private",
        # a bare annotation declares no member
        "ANNOTATED_ONLY",
        # descriptors and explicit non-members
        "lam",
        "prop",
        "stat",
        "klass",
        "kept_out",
        "kept_out_too",
        # methods and nested classes
        "method",
        "described",
        "Inner",
        "NESTED",
    ):
        assert excluded not in names, excluded
    assert set(_variants(indexed, "proj.mood.Period")) == {"MONDAY"}
    # A nested class inside an enum is not an enum of its own.
    assert _variants(indexed, "proj.mood.Mood.Inner") == {}


def test_a_ts_member_name_spelled_with_escapes_is_the_runtime_string(
    indexed: _StatefulIngestor,
) -> None:
    variants = _variants(indexed, "proj.escaped.Esc")
    assert list(variants) == [
        "ab",
        "cd",
        "ef",
        "linecont",
        "\U0001f600",
        'q"uote',
        "sq",
    ]
    assert variants["ab"][cs.KEY_QUALIFIED_NAME] == "proj.escaped.Esc.ab"
    assert variants["ab"][cs.KEY_VALUE] == "1"
    # A bare quoted member has no initializer, so no value.
    assert cs.KEY_VALUE not in variants["cd"]


def test_python_enum_membership_follows_what_the_call_refers_to(
    indexed: _StatefulIngestor,
) -> None:
    # REAL calls the module's own `nonmember`, so it is a member; `cp` is
    # functools.cached_property, a descriptor, as are the qualified spellings.
    assert list(_variants(indexed, "proj.descriptors.Mode")) == ["REAL", "ALSO"]


def test_a_python_class_that_is_not_an_enum_gets_no_variants(
    indexed: _StatefulIngestor,
) -> None:
    assert _variants(indexed, "proj.plain.Plain") == {}
    assert _owner_edges(indexed, "proj.plain.Plain") == []
    # `Enum` here is a first-party class of the same name, not `enum.Enum`.
    assert _variants(indexed, "proj.fake.Fake") == {}
    assert _owner_edges(indexed, "proj.fake.Fake") == []


def test_a_python_enum_keeps_its_class_label_bases_and_fields(
    indexed: _StatefulIngestor,
) -> None:
    # Only variants are added: the class is not relabelled, its INHERITS
    # edge to enum.Enum is still there, and the `fields` group still records
    # its class attributes as it did before.
    assert _label_of(indexed, "proj.mood.Mood") == {cs.NodeLabel.CLASS.value}
    inherits = {
        e[4]
        for e in indexed.edges
        if e[1] == "proj.mood.Mood" and e[2] == cs.RelationshipType.INHERITS.value
    }
    assert "enum.Enum" in inherits, inherits
    fields = {
        e[4]
        for e in indexed.edges
        if e[1] == "proj.mood.Mood" and e[2] == cs.RelationshipType.HAS_FIELD.value
    }
    assert "proj.mood.Mood.HAPPY" in fields, fields
    plain_fields = {
        e[4]
        for e in indexed.edges
        if e[1] == "proj.plain.Plain" and e[2] == cs.RelationshipType.HAS_FIELD.value
    }
    assert plain_fields == {"proj.plain.Plain.RED", "proj.plain.Plain.GREEN"}


def test_a_ts_const_object_literal_is_not_an_enum(indexed: _StatefulIngestor) -> None:
    assert cs.NodeLabel.ENUM.value not in _label_of(indexed, "proj.colors.Palette")
    assert _variants(indexed, "proj.colors.Palette") == {}


def test_dart_enum_body_members_are_not_variants(indexed: _StatefulIngestor) -> None:
    # Constructors, the field, the static const and the method are all in
    # the enum body; only the constants are variants.
    names = set(_variants(indexed, "proj.status.Planet"))
    assert names == {"earth", "mars", "venus"}
    for excluded in ("Planet", "named", "g", "home", "compareTo"):
        assert excluded not in names


def test_a_reparse_drops_variants_a_python_class_no_longer_declares(
    tmp_path: Path,
) -> None:
    # The Python owner is a Class, not an Enum: the module-delete walk has to
    # reach its variants through HAS_VARIANT all the same, both when a member
    # is removed and when the class stops being an enum at all.
    repo = tmp_path / "proj"
    repo.mkdir(parents=True)
    source = repo / "mood.py"
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.PYTHON not in parsers:
        pytest.skip("python parser not available")
    store = _StatefulIngestor()

    def _run(text: str) -> set[str]:
        source.write_text(text, encoding="utf-8")
        GraphUpdater(
            ingestor=store,
            repo_path=repo,
            parsers=parsers,
            queries=queries,
            capture=resolve_capture(["+enum_variants"]),
        ).run(force=True)
        names = set(_variants(store, "proj.mood.Mood"))
        owned = {
            e[4].rsplit(cs.SEPARATOR_DOT, 1)[-1]
            for e in _owner_edges(store, "proj.mood.Mood")
        }
        assert names == owned, names ^ owned
        return names

    header = "from enum import Enum\n\n\n"
    assert _run(header + "class Mood(Enum):\n    HAPPY = 1\n    GONE = 2\n") == {
        "HAPPY",
        "GONE",
    }
    assert _run(header + "class Mood(Enum):\n    HAPPY = 1\n") == {"HAPPY"}
    assert _run(header + "class Mood:\n    HAPPY = 1\n") == set()
