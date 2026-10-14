"""A C++ template specialization gets a stable, single-line, separator-safe
name (issue #2434).

tree-sitter names a specialization with a `template_type` node, and its raw
text became the class's `name` and the last segment of its qualified name:
argument lists spread over several lines kept their newlines and indentation,
a whitespace-only reformat renamed the node, and a `.` inside the arguments
(`decltype(std::declval<T&>().data())`, a pack expansion `Ts...`) read as a
qualified-name separator to everything that splits qns on `.`.

The bracketed spelling stays (`Box<int>` is how the graph already names a
specialization, see test_cpp_template_specialization_qn), rendered from the
tokens: whitespace and comments dropped, one space after each comma, between
two words, and between two tokens that written together would lex as others
(`a + ++b`, not `a+++b`); a `.` in the arguments is written as
CPP_TEMPLATE_ARG_DOT.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import (
    _MockIngestor,
    create_and_run_updater,
    get_nodes,
    get_relationships,
    run_updater,
)

_DOT = cs.CPP_TEMPLATE_ARG_DOT

_FMT_SHAPE = (
    "template <typename T, typename Enable = void>\n"
    "struct is_contiguous : std::false_type {};\n"
    "template <typename T>\n"
    "struct is_contiguous<T,\n"
    "                     void_t<decltype(std::declval<T&>().data()),\n"
    "                            decltype(std::declval<T&>().size())>>\n"
    "    : std::true_type {\n"
    "  int probe() { return helper(); }\n"
    "};\n"
)
_FMT_SPEC_NAME = (
    f"is_contiguous<T, void_t<decltype(std::declval<T&>(){_DOT}data()), "
    f"decltype(std::declval<T&>(){_DOT}size())>>"
)


def _index(repo: Path, source: str, mock_ingestor: MagicMock) -> str:
    (repo / "spec.cpp").write_text(source, encoding="utf-8")
    run_updater(repo, mock_ingestor, skip_if_missing="cpp")
    return f"{repo.name}.spec"


def _class_names(mock_ingestor: MagicMock) -> dict[str, str]:
    return {
        c.args[1][cs.KEY_QUALIFIED_NAME]: c.args[1][cs.KEY_NAME]
        for c in get_nodes(mock_ingestor, cs.NodeLabel.CLASS)
    }


def _qns(mock_ingestor: MagicMock, label: cs.NodeLabel) -> list[str]:
    return [c.args[1][cs.KEY_QUALIFIED_NAME] for c in get_nodes(mock_ingestor, label)]


def _edges(mock_ingestor: MagicMock, rel: cs.RelationshipType) -> set[tuple[str, str]]:
    return {(c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, rel)}


_HELPER = "int helper() { return 1; }\n"


# --- red: the reported shapes ---------------------------------------------


def test_a_multi_line_partial_specialization_gets_a_single_line_name(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    module = _index(temp_repo, _HELPER + _FMT_SHAPE, mock_ingestor)

    classes = _class_names(mock_ingestor)

    spec_qn = f"{module}.{_FMT_SPEC_NAME}"
    assert classes.get(spec_qn) == _FMT_SPEC_NAME, classes
    assert not any("\n" in qn or "\n" in name for qn, name in classes.items())


def test_a_dot_inside_template_arguments_is_not_a_qn_separator(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    source = (
        _HELPER
        + _FMT_SHAPE
        + "template <class... Ts> struct is_contiguous<std::tuple<Ts...>> {\n"
        "  int probe() { return helper(); }\n"
        "};\n"
    )
    module = _index(temp_repo, source, mock_ingestor)
    depth = module.count(cs.SEPARATOR_DOT) + 1
    pack_name = f"is_contiguous<std::tuple<Ts{_DOT * 3}>>"

    classes = _class_names(mock_ingestor)
    methods = _qns(mock_ingestor, cs.NodeLabel.METHOD)

    assert f"{module}.{pack_name}" in classes, classes
    # Splitting on the separator gives the module, then exactly one class
    # segment (and one method segment): the parent of every member is its
    # class, which is what DEFINES_METHOD says too.
    for qn, name in classes.items():
        assert qn.split(cs.SEPARATOR_DOT)[depth:] == [name], qn
    assert len(methods) == 2, methods
    defines = _edges(mock_ingestor, cs.RelationshipType.DEFINES_METHOD)
    for method_qn in methods:
        owner = method_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
        assert owner in classes, method_qn
        assert (owner, method_qn) in defines, method_qn


@pytest.mark.parametrize(
    "spelling",
    [
        "Box<T*, int>",
        "Box<T *,int>",
        "Box< T*,\n           int >",
        "Box<T* /* pointer */, int>",
        "Box<T*, // pointer\n    int>",
    ],
)
def test_a_whitespace_only_reformat_keeps_the_name(
    temp_repo: Path, mock_ingestor: MagicMock, spelling: str
) -> None:
    source = (
        "template <typename T, typename U> struct Box {};\n"
        f"template <typename T> struct {spelling} {{ void put(T* v) {{}} }};\n"
    )
    module = _index(temp_repo, source, mock_ingestor)

    assert f"{module}.Box<T*, int>" in _class_names(mock_ingestor)
    assert f"{module}.Box<T*, int>.put" in _qns(mock_ingestor, cs.NodeLabel.METHOD)


def test_the_fmt_specialization_is_named_alike_however_it_is_wrapped(
    tmp_path: Path,
) -> None:
    one_line = " ".join(line.strip() for line in _FMT_SHAPE.splitlines())
    names: list[set[str]] = []
    for label, source in (("wrapped", _FMT_SHAPE), ("flat", one_line)):
        repo = tmp_path / label
        repo.mkdir()
        ingestor = _MockIngestor()
        (repo / "spec.cpp").write_text(source, encoding="utf-8")
        create_and_run_updater(repo, ingestor, skip_if_missing="cpp")
        names.append(set(_class_names(ingestor).values()))

    assert names[0] == names[1] == {"is_contiguous", _FMT_SPEC_NAME}


def test_partial_and_full_specializations_are_distinct_canonical_classes(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    source = (
        "namespace ns {\n"
        "template <typename T, typename U = void> struct Box { void put() {} };\n"
        "template <typename T> struct Box<T*> { void put() {} };\n"
        "template <typename T> struct Box<T,int> { void put() {} };\n"
        "template <typename T>\n"
        "struct Box<typename T::value_type,\n"
        "           std :: pair < T , unsigned   long > > { void put() {} };\n"
        "template <> struct Box<char> { void put() {} };\n"
        "template <> class Box< unsigned   int > { void put() {} };\n"
        "}\n"
    )
    module = _index(temp_repo, source, mock_ingestor)

    classes = _qns(mock_ingestor, cs.NodeLabel.CLASS)
    methods = _qns(mock_ingestor, cs.NodeLabel.METHOD)

    expected = {
        "Box",
        "Box<T*>",
        "Box<T, int>",
        "Box<typename T::value_type, std::pair<T, unsigned long>>",
        "Box<char>",
        "Box<unsigned int>",
    }
    assert sorted(classes) == sorted(f"{module}.ns.{name}" for name in expected)
    assert sorted(methods) == sorted(f"{module}.ns.{name}.put" for name in expected)
    # Six definitions, six names: none collided into a `@line` variant.
    assert not any(cs.DUP_QN_MARKER in qn for qn in classes + methods)


def test_members_of_a_specialization_hang_off_its_canonical_name(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    source = (
        _HELPER + "template <typename T, typename U = void> struct Box {};\n"
        "template <typename T>\n"
        "struct Box<T*,\n"
        "           decltype(T().size())> {\n"
        "  struct Inner { int get() { return helper(); } };\n"
        "  int put() { return helper(); }\n"
        "};\n"
    )
    module = _index(temp_repo, source, mock_ingestor)
    spec_qn = f"{module}.Box<T*, decltype(T(){_DOT}size())>"

    assert {f"{spec_qn}.Inner", spec_qn} <= set(_class_names(mock_ingestor))
    assert {
        (spec_qn, f"{spec_qn}.put"),
        (f"{spec_qn}.Inner", f"{spec_qn}.Inner.get"),
    } <= _edges(mock_ingestor, cs.RelationshipType.DEFINES_METHOD)
    # The call pass names the caller the same way, or its CALLS edges would
    # hang off a method node that does not exist.
    assert {
        (f"{spec_qn}.put", f"{module}.helper"),
        (f"{spec_qn}.Inner.get", f"{module}.helper"),
    } <= _edges(mock_ingestor, cs.RelationshipType.CALLS)


def test_the_call_pass_spells_an_unrecorded_caller_s_class_alike(
    temp_repo: Path,
) -> None:
    # Recovery reads the `FMT_CATCH(...) {...}` macro block as a nested
    # definition the definition pass never records, so the call pass names
    # that caller from its own walk to the class. Run without the
    # conftest graph audit: that caller has no node for ANY class (fmt's
    # `FMT_TRY`/`FMT_CATCH`, a separate defect); what is checked here is
    # only that the call pass spells the specialization as the
    # definition pass does.
    (temp_repo / "spec.cpp").write_text(
        _HELPER + "template <typename T, typename U = void> struct Box {};\n"
        "template <typename T>\n"
        "struct Box<T,\n"
        "           decltype(T().size())> {\n"
        "  int put() {\n"
        "    FMT_TRY { helper(); }\n"
        "    FMT_CATCH(...) { helper(); }\n"
        "    return 0;\n"
        "  }\n"
        "};\n",
        encoding="utf-8",
    )
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.CPP not in parsers:
        pytest.skip("cpp parser not available")
    ingestor = _MockIngestor()
    GraphUpdater(
        ingestor=ingestor, repo_path=temp_repo, parsers=parsers, queries=queries
    ).run()
    spec_qn = f"{temp_repo.name}.spec.Box<T, decltype(T(){_DOT}size())>"

    callers = {caller for caller, _ in _edges(ingestor, cs.RelationshipType.CALLS)}

    assert f"{spec_qn}.put" in callers, callers
    assert all(caller.startswith(f"{spec_qn}.") for caller in callers), callers


# --- negative: what must not change ------------------------------------------


def test_the_primary_template_and_ordinary_classes_keep_their_names(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    source = (
        _FMT_SHAPE + "namespace ns { struct Plain { void m() {} }; }\n"
        "struct Outer { class Inner; };\n"
        "class Outer::Inner { void k() {} };\n"
        "template <typename T> class Holder { T value; };\n"
    )
    module = _index(temp_repo, source, mock_ingestor)

    classes = _class_names(mock_ingestor)

    for qn, name in (
        (f"{module}.is_contiguous", "is_contiguous"),
        (f"{module}.ns.Plain", "Plain"),
        (f"{module}.Outer", "Outer"),
        (f"{module}.Outer::Inner", "Outer::Inner"),
        (f"{module}.Holder", "Holder"),
    ):
        assert classes.get(qn) == name, classes


@pytest.mark.parametrize(
    "spelling",
    [
        "Box<char>",
        "Box<T*, int>",
        "Box<std::vector<int>>",
        "Box<std::map<std::string, int>, const T&>",
        "std::hash<ns::Key>",
    ],
)
def test_a_conventionally_written_specialization_keeps_its_existing_name(
    temp_repo: Path, mock_ingestor: MagicMock, spelling: str
) -> None:
    # Single-line, one space after each comma, no dot: what the graph
    # already held for most code stays byte-for-byte the same.
    source = (
        "namespace ns { struct Key {}; }\n"
        "template <typename T, typename U = void> struct Box {};\n"
        f"template <typename T> struct {spelling} {{ void put() {{}} }};\n"
    )
    module = _index(temp_repo, source, mock_ingestor)

    assert _class_names(mock_ingestor).get(f"{module}.{spelling}") == spelling


# --- string literal arguments keep their own spelling (review of PR 3356) --


def test_whitespace_inside_a_string_literal_argument_keeps_specializations_apart(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A C++20 fixed-string argument's spaces are part of its value: `"a b"`
    # and `"a  b"` are two specializations, and must not share one name.
    module = _index(
        temp_repo,
        "template <auto S> struct Box {};\n"
        'template <> struct Box<"a b"> {};\n'
        'template <> struct Box<"a  b"> {};\n'
        "template <> struct Box<' '> {};\n",
        mock_ingestor,
    )

    names = set(_class_names(mock_ingestor).values())

    assert {'Box<"a b">', 'Box<"a  b">', "Box<' '>"} <= names
    assert f'{module}.Box<"a  b">' in _qns(mock_ingestor, cs.NodeLabel.CLASS)


def test_a_raw_string_argument_stays_on_one_line(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A raw literal may hold a line break; it is written as an escape so the
    # name stays one line without merging it with any other spelling.
    _index(
        temp_repo,
        'template <auto S> struct Box {};\ntemplate <> struct Box<R"(x\ny)"> {};\n',
        mock_ingestor,
    )

    names = set(_class_names(mock_ingestor).values())

    assert 'Box<R"(x\\ny)">' in names
    assert not any("\n" in name for name in names)


def test_spacing_around_a_string_literal_argument_is_still_normalised(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: only the literal's own text is kept; the layout around it
    # still renders the conventional way.
    _index(
        temp_repo,
        'template <auto S> struct Box {};\ntemplate <> struct Box<\n  "a b"\n> {};\n',
        mock_ingestor,
    )

    assert 'Box<"a b">' in set(_class_names(mock_ingestor).values())


def test_a_raw_line_break_and_a_raw_backslash_n_stay_two_names(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The escape is lossless: a raw literal spelling backslash-n is a
    # different argument from one holding a real line break.
    _index(
        temp_repo,
        "template <auto S> struct Box {};\n"
        'template <> struct Box<R"(a\\nb)"> {};\n'
        'template <> struct Box<R"(a\nb)"> {};\n',
        mock_ingestor,
    )

    names = set(_class_names(mock_ingestor).values())

    assert {'Box<R"(a\\\\nb)">', 'Box<R"(a\\nb)">'} <= names


def test_a_crlf_checkout_names_a_raw_line_break_as_one_newline(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Translation phase 1 reads a CRLF line ending as one new-line, so a raw
    # literal spanning one holds the same value as in an LF checkout (a
    # Windows clone with autocrlf writes every line ending as CRLF).
    (temp_repo / "spec.cpp").write_bytes(
        b"template <auto S> struct Box {};\r\n"
        b'template <> struct Box<R"(x\r\ny)"> {};\r\n'
    )
    run_updater(temp_repo, mock_ingestor, skip_if_missing="cpp")

    names = set(_class_names(mock_ingestor).values())

    assert 'Box<R"(x\\ny)">' in names
    assert not any("\\r" in name for name in names)


def test_a_crlf_checkout_keeps_a_raw_backslash_n_apart_from_a_line_break(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "spec.cpp").write_bytes(
        b"template <auto S> struct Box {};\r\n"
        b'template <> struct Box<R"(a\\nb)"> {};\r\n'
        b'template <> struct Box<R"(a\r\nb)"> {};\r\n'
    )
    run_updater(temp_repo, mock_ingestor, skip_if_missing="cpp")

    names = set(_class_names(mock_ingestor).values())

    assert {'Box<R"(a\\\\nb)">', 'Box<R"(a\\nb)">'} <= names


def test_a_lone_carriage_return_in_a_raw_literal_keeps_its_escape(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Only a CRLF pair is a line ending to fold; a carriage return on its own
    # inside the literal stays the character it is.
    (temp_repo / "spec.cpp").write_bytes(
        b'template <auto S> struct Box {};\ntemplate <> struct Box<R"(x\ry)"> {};\n'
    )
    run_updater(temp_repo, mock_ingestor, skip_if_missing="cpp")

    names = set(_class_names(mock_ingestor).values())

    assert 'Box<R"(x\\ry)">' in names


def test_a_literal_dot_leader_is_not_mistaken_for_an_argument_dot(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The `.` stand-in must stay unambiguous: a string argument that already
    # holds U+2024 is a different specialization from one holding a `.`.
    _index(
        temp_repo,
        "template <auto S> struct Box {};\n"
        'template <> struct Box<"a.b"> {};\n'
        f'template <> struct Box<"a{_DOT}b"> {{}};\n',
        mock_ingestor,
    )

    names = set(_class_names(mock_ingestor).values())

    assert len({name for name in names if name.startswith("Box<")}) == 2
    assert f'Box<"a{_DOT}b">' in names


# --- operator tokens stay apart (review of PR 3356) --------------------------


def _name_tokens_of(spelling: str) -> list[str]:
    """The tokens tree-sitter lexes a class head named `spelling` into.

    Its lexer munches maximally and closes nested template lists the way C++
    does, so a rendered name that lexes back to its source's tokens kept
    every operator apart. tree-sitter lexes a comment only once it is
    closed, so the trailing `/**/` lets a `/*` the name opens swallow the
    rest of the head.
    """
    parsers, _ = load_parsers()
    if cs.SupportedLanguage.CPP not in parsers:
        pytest.skip("cpp parser not available")
    source = f"template <> struct {spelling.replace(_DOT, '.')} {{}}; /**/\n"
    tokens: list[str] = []
    stack = [parsers[cs.SupportedLanguage.CPP].parse(source.encode()).root_node]
    while stack:
        node = stack.pop()
        if node.type == cs.TS_COMMENT:
            continue
        if node.child_count:
            stack.extend(reversed(node.children))
        elif node.text:
            tokens.append(node.text.decode())
    return tokens


def test_specializations_that_split_operators_differently_get_two_names(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `a + ++b` and `a++ + b` are different expressions, and an overloaded
    # operator can make them pick different specializations; joined without
    # a space both read `a+++b`, so the later one became an `@line` variant.
    module = _index(
        temp_repo,
        _HELPER + "template <typename T, typename U> struct Box {};\n"
        "template <typename T> struct Box<T, decltype(T::a + ++T::b)> {\n"
        "  int put() { return helper(); }\n"
        "};\n"
        "template <typename T> struct Box<T, decltype(T::a++ + T::b)> {\n"
        "  int put() { return helper(); }\n"
        "};\n",
        mock_ingestor,
    )
    first = "Box<T, decltype(T::a+ ++T::b)>"
    second = "Box<T, decltype(T::a++ +T::b)>"

    classes = _qns(mock_ingestor, cs.NodeLabel.CLASS)
    methods = _qns(mock_ingestor, cs.NodeLabel.METHOD)

    assert {f"{module}.{first}", f"{module}.{second}"} <= set(classes), classes
    assert {f"{module}.{first}.put", f"{module}.{second}.put"} <= set(methods)
    assert not any(cs.DUP_QN_MARKER in qn for qn in classes + methods)


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        pytest.param(
            "Box<X, decltype(X::a - --X::b)>",
            "Box<X, decltype(X::a-- - X::b)>",
            ("Box<X, decltype(X::a- --X::b)>", "Box<X, decltype(X::a-- -X::b)>"),
            id="minus-then-decrement",
        ),
        pytest.param(
            # `a - -b` must not read `a-- b`.
            "Box<X, decltype(X::a - -X::b)>",
            "Box<X, decltype(X::a-- - X::b)>",
            ("Box<X, decltype(X::a- -X::b)>", "Box<X, decltype(X::a-- -X::b)>"),
            id="minus-then-negate",
        ),
        pytest.param(
            "Box<X, decltype(X::a & &X::b)>",
            "Box<X, decltype(X::a && X::b)>",
            ("Box<X, decltype(X::a& &X::b)>", "Box<X, decltype(X::a&&X::b)>"),
            id="ampersand-pair",
        ),
        pytest.param(
            # `operator<` given template arguments: `< <`, not `<<`.
            "Box<X, decltype(&X::operator< <int>)>",
            "Box<X, decltype(&X::operator<< <int>)>",
            (
                "Box<X, decltype(&X::operator< <int>)>",
                "Box<X, decltype(&X::operator<< <int>)>",
            ),
            id="less-then-template-list",
        ),
        pytest.param(
            # `operator-` as the last argument: `- >` must not read `->`.
            "Box<X, &X::operator- >",
            "Box<X, &X::operator-> >",
            ("Box<X, &X::operator- >", "Box<X, &X::operator-> >"),
            id="minus-then-closer",
        ),
        pytest.param(
            # Two characters of the left token and one of the right make
            # `<=>`, which no single pair of characters shows.
            "Box<X, &X::operator<= >",
            "Box<X, &X::operator<=> >",
            ("Box<X, &X::operator<= >", "Box<X, &X::operator<=> >"),
            id="less-equal-then-closer",
        ),
        pytest.param(
            # Not valid C++, but tree-sitter reads it as `.` then `*`, and a
            # name must still not merge it with `.*`.
            "Box<X, decltype(X::a . *X::b)>",
            "Box<X, decltype(X::a .* X::b)>",
            (
                f"Box<X, decltype(X::a{_DOT} *X::b)>",
                f"Box<X, decltype(X::a{_DOT}*X::b)>",
            ),
            id="dot-then-star",
        ),
        pytest.param(
            # `/` then `*` written together would open a comment.
            "Box<X, decltype(X::a / *X::b)>",
            "Box<X, decltype(X::a /**/ *X::b)>",
            ("Box<X, decltype(X::a/ *X::b)>", "Box<X, decltype(X::a*X::b)>"),
            id="slash-then-star",
        ),
        pytest.param(
            # A list's closing `>` then a greater-than or a comparison: only
            # two closers are written together.
            "Box<X, decltype(X::template v<int> > 3)>",
            "Box<X, decltype(X::template v<int> >= 3)>",
            (
                "Box<X, decltype(X::template v<int> >3)>",
                "Box<X, decltype(X::template v<int> >=3)>",
            ),
            id="closer-then-comparison",
        ),
        pytest.param(
            # `0x1E+1` is one preprocessing number, not a sum.
            "Box<X, 0x1E + 1>",
            "Box<X, 0x1E - 1>",
            ("Box<X, 0x1E +1>", "Box<X, 0x1E -1>"),
            id="hex-exponent-then-sign",
        ),
        pytest.param(
            # `<:` is a digraph for `[`, so a less-than keeps `::` apart;
            # `<=` before `::` spells none and stays joined.
            "Box<X, decltype(X::a < ::b)>",
            "Box<X, decltype(X::a <= ::b)>",
            ("Box<X, decltype(X::a< ::b)>", "Box<X, decltype(X::a<=::b)>"),
            id="less-then-scope",
        ),
    ],
)
def test_adjacent_operator_tokens_keep_a_separator(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    first: str,
    second: str,
    expected: tuple[str, str],
) -> None:
    module = _index(
        temp_repo,
        f"template <> struct {first} {{ void put() {{}} }};\n"
        f"template <> struct {second} {{ void put() {{}} }};\n",
        mock_ingestor,
    )

    classes = _class_names(mock_ingestor)
    names = sorted(name for name in classes.values() if name.startswith("Box<"))

    assert names == sorted(expected), classes
    # Each name lexes back to its own source's tokens, so no two token
    # sequences can share one.
    assert sorted(_name_tokens_of(name) for name in names) == sorted(
        _name_tokens_of(spelling) for spelling in (first, second)
    )
    assert {f"{module}.{name}" for name in expected} <= set(classes)
    assert not any(cs.DUP_QN_MARKER in qn for qn in classes)


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("Box<A<B<C>>>", "Box<A<B<C>>>"),
        ("Box<A<B<C> > >", "Box<A<B<C>>>"),
        ("Box<A<B<C> >>", "Box<A<B<C>>>"),
        ("Box<::ns::Key>", "Box<::ns::Key>"),
        ("Box<X, decltype(X::a)>", "Box<X, decltype(X::a)>"),
        (
            "Box<typename A<X>::type, unsigned  long>",
            "Box<typename A<X>::type, unsigned long>",
        ),
        ("Box<X, decltype(X::a << X::b)>", "Box<X, decltype(X::a<<X::b)>"),
        ("Box<X, decltype(X::a->b + 1)>", "Box<X, decltype(X::a->b+1)>"),
        ("Box<X, decltype(X::a++ - 1)>", "Box<X, decltype(X::a++-1)>"),
        ("Box<1 + 1>", "Box<1+1>"),
        ("Box<const X&, X&&>", "Box<const X&, X&&>"),
    ],
)
def test_tokens_that_lex_apart_anyway_stay_unspaced(
    temp_repo: Path, mock_ingestor: MagicMock, spelling: str, expected: str
) -> None:
    # Negative: a separator goes only where joining would re-lex. Closing
    # template lists still read `>>` however the source spaced them, a
    # list's `<` still meets a leading `::`, and `::`, words and ordinary
    # operators keep the spelling they had.
    module = _index(
        temp_repo,
        f"template <> struct {spelling} {{ void put() {{}} }};\n",
        mock_ingestor,
    )

    assert _class_names(mock_ingestor).get(f"{module}.{expected}") == expected
