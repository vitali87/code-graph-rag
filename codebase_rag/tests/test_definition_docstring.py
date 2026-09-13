"""Doc comments reach the definition they document (issue #1809).

Seven labels declare `docstring: string?` and only Python ever populated it:
`_get_docstring` reads a Python string literal from the body, so for every
other language a `/** */` or `///` above a declaration was dropped.

The tests are organised around ONE decision, because that is what the code
is: a doc comment either describes the file or the declaration beneath it,
and `module_docstring` already decides it from the other side. So each
language is checked on all three rows of that decision --

    attached to the declaration   -> the DECLARATION's
    detached by a blank line      -> the FILE's, where the marker allows
    absent                        -> nobody's

-- rather than on a representative case. The per-language table is enumerated
for the same reason the module tests are: a language whose spec is wrong fails
its own row instead of hiding behind a sibling's pass.
"""

from __future__ import annotations

import pytest

from codebase_rag.constants import SupportedLanguage as Lang
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.definition_docstring import (
    extract_definition_docstring,
    libclang_docstring,
)
from codebase_rag.parsers.module_docstring import extract_module_docstring
from codebase_rag.types_defs import ASTNode

# Every language whose documentation is a COMMENT, with a declaration the
# grammar names and a marker that language actually uses. Python is absent
# deliberately: its docstring is a string literal inside the body, not a
# comment, and `_get_docstring` owns that path. SQL is absent because it has
# no documentation convention.
#
# Written out rather than derived from the spec table on purpose. A test that
# iterates the same mapping it is checking cannot see a language being
# dropped from it -- the case disappears along with the entry and the suite
# stays green (the defect shape measured on PR #1829).
CASES: tuple[tuple[Lang, str, str, str], ...] = (
    (Lang.RUST, "fn f() {}", "function_item", "/// DOC"),
    (Lang.GO, "func F() {}", "function_declaration", "// DOC"),
    (Lang.JAVA, "class C {}", "class_declaration", "/** DOC */"),
    (Lang.SCALA, "class C", "class_definition", "/** DOC */"),
    (Lang.C, "struct S { int a; };", "struct_specifier", "/** DOC */"),
    (Lang.CPP, "class C { };", "class_specifier", "/** DOC */"),
    (Lang.CSHARP, "class C {}", "class_declaration", "/// DOC"),
    (Lang.LUA, "local function f() end", "function_declaration", "--- DOC"),
    (Lang.DART, "class C {}", "class_definition", "/// DOC"),
    (Lang.JS, "class C {}", "class_declaration", "/** DOC */"),
    (Lang.TS, "interface I {}", "interface_declaration", "/** DOC */"),
    (Lang.TSX, "class C {}", "class_declaration", "/** DOC */"),
)

# PHP needs its opening tag, so its source is assembled differently and it
# gets its own cases rather than a branch inside the shared builder.
PHP_DECL = "class C {}"
PHP_TYPE = "class_declaration"

LANGUAGE_IDS = tuple(case[0].value for case in CASES)


@pytest.fixture(scope="module")
def parsers() -> dict:
    loaded, _ = load_parsers()
    return loaded


def _find(node: ASTNode, node_type: str) -> ASTNode | None:
    if node.type == node_type:
        return node
    for child in node.children:
        found = _find(child, node_type)
        if found is not None:
            return found
    return None


def _parse(parsers: dict, language: Lang, source: str) -> ASTNode:
    parser = parsers.get(language.value)
    if parser is None:
        pytest.skip(f"{language.value} parser not available")
    return parser.parse(source.encode()).root_node


def _declaration(parsers: dict, language: Lang, source: str, node_type: str) -> ASTNode:
    root = _parse(parsers, language, source)
    node = _find(root, node_type)
    # A fixture guard, not a behaviour assertion. A grammar that renames a node
    # type leaves `node is None`, and without this the test reports "no
    # docstring extracted" -- a production-looking failure caused by the
    # fixture never landing (the shape that cost a peer a Windows-only
    # debugging session on PR #1835).
    assert node is not None, (
        f"fixture: {language.value} grammar has no {node_type!r} in this source"
    )
    return node


class TestAttachedDocBelongsToTheDeclaration:
    """Row 1: a doc comment touching a declaration is that declaration's."""

    @pytest.mark.parametrize(
        ("language", "decl", "node_type", "marker"), CASES, ids=LANGUAGE_IDS
    )
    def test_every_language_attaches_its_doc(
        self, parsers: dict, language: Lang, decl: str, node_type: str, marker: str
    ) -> None:
        prologue = "package m\n\n" if language is Lang.GO else ""
        node = _declaration(
            parsers, language, f"{prologue}{marker}\n{decl}\n", node_type
        )
        assert extract_definition_docstring(node, language) == "DOC"

    def test_php_attaches_its_doc(self, parsers: dict) -> None:
        node = _declaration(
            parsers, Lang.PHP, f"<?php\n/** DOC */\n{PHP_DECL}\n", PHP_TYPE
        )
        assert extract_definition_docstring(node, Lang.PHP) == "DOC"


class TestAbsentDocIsNobodys:
    """Row 3: no doc comment means None, for every language."""

    @pytest.mark.parametrize(
        ("language", "decl", "node_type", "marker"), CASES, ids=LANGUAGE_IDS
    )
    def test_a_declaration_with_no_comment_has_no_docstring(
        self, parsers: dict, language: Lang, decl: str, node_type: str, marker: str
    ) -> None:
        prologue = "package m\n\n" if language is Lang.GO else ""
        node = _declaration(parsers, language, f"{prologue}{decl}\n", node_type)
        assert extract_definition_docstring(node, language) is None

    # Go is excluded, and the exclusion is the finding rather than a
    # convenience. Go has NO doc marker: `//` immediately above a declaration
    # IS its doc comment, which is what gofmt and pkgsite render, and the
    # module table already says so ("Go has no marker at all"). So there is no
    # "ordinary comment" in that position to decline -- this row asked Go for
    # behaviour the language does not have, and the first run failed on the
    # test rather than the code. Go's declining case is adjacency, covered by
    # `test_a_detached_go_comment_is_not_the_declarations` below.
    @pytest.mark.parametrize(
        ("language", "decl", "node_type", "marker"),
        [case for case in CASES if case[0] is not Lang.GO],
        ids=[name for name in LANGUAGE_IDS if name != Lang.GO.value],
    )
    def test_an_ordinary_comment_is_not_documentation(
        self, parsers: dict, language: Lang, decl: str, node_type: str, marker: str
    ) -> None:
        """The known-negative the whole table needs.

        Without this the suite proves only that the extractor can return a
        string, never that it can decline -- and an extractor that captures
        every comment passes every row above.
        """
        plain = "-- ordinary" if language is Lang.LUA else "// ordinary"
        node = _declaration(parsers, language, f"{plain}\n{decl}\n", node_type)
        assert extract_definition_docstring(node, language) is None

    def test_a_detached_go_comment_is_not_the_declarations(self, parsers: dict) -> None:
        """Go's declining case is the blank line, since it has no marker.

        A licence header or a stray note separated from the declaration must
        not become its documentation -- adjacency is the whole distinction
        the language offers.
        """
        node = _declaration(
            parsers,
            Lang.GO,
            "package m\n\n// detached note\n\nfunc F() {}\n",
            "function_declaration",
        )
        assert extract_definition_docstring(node, Lang.GO) is None


class TestTheTwoLevelsAreACoherentPair:
    """Row 2, and the reason the pair is one decision rather than two.

    `module_docstring` REJECTS a comment that documents the declaration below
    it; this module ACCEPTS that same comment. Exactly one of them must claim
    any given comment -- a row both claim attributes a class's own
    description to its file as well, and a row neither claims drops the
    documentation silently.
    """

    def test_attached_goes_to_the_declaration_and_not_the_file(
        self, parsers: dict
    ) -> None:
        source = "/** DOC */\nclass C {}\n"
        root = _parse(parsers, Lang.JAVA, source)
        node = _declaration(parsers, Lang.JAVA, source, "class_declaration")
        assert extract_definition_docstring(node, Lang.JAVA) == "DOC"
        assert extract_module_docstring(root, Lang.JAVA) is None

    def test_detached_goes_to_the_file_and_not_the_declaration(
        self, parsers: dict
    ) -> None:
        """Java, deliberately: `/** */` marks BOTH levels there.

        Rust cannot express this row -- it marks a file `//!` and an item
        `///`, so a detached `///` is claimed by neither and that is correct
        rather than a gap. Testing this row in Rust reports a hole that
        cannot exist in any language where the markers coincide.
        """
        source = "/** DOC */\n\nclass C {}\n"
        root = _parse(parsers, Lang.JAVA, source)
        node = _declaration(parsers, Lang.JAVA, source, "class_declaration")
        assert extract_definition_docstring(node, Lang.JAVA) is None
        assert extract_module_docstring(root, Lang.JAVA) == "DOC"

    def test_rust_inner_doc_is_the_files_at_any_distance(self, parsers: dict) -> None:
        """`//!` and `/*! */` document the enclosing module, adjacent or not.

        The block form is the one the first version got wrong: `/*!` was in the
        definition marker set too, so an adjacent `/*! FILEDOC */` was stored on
        the module and on `f` (both bots on PR #1888).
        """
        for source in (
            "//! FILEDOC\nfn f() {}\n",
            "//! FILEDOC\n\nfn f() {}\n",
            "/*! FILEDOC */\nfn f() {}\n",
            "/*! FILEDOC */\n\nfn f() {}\n",
        ):
            root = _parse(parsers, Lang.RUST, source)
            node = _declaration(parsers, Lang.RUST, source, "function_item")
            assert extract_definition_docstring(node, Lang.RUST) is None
            assert extract_module_docstring(root, Lang.RUST) == "FILEDOC"

    def test_a_detached_rust_outer_doc_is_nobodys(self, parsers: dict) -> None:
        """Neither half claims it, and rustc warns on it too."""
        source = "/// ORPHAN\n\nfn f() {}\n"
        root = _parse(parsers, Lang.RUST, source)
        node = _declaration(parsers, Lang.RUST, source, "function_item")
        assert extract_definition_docstring(node, Lang.RUST) is None
        assert extract_module_docstring(root, Lang.RUST) is None


class TestTheMarkersDifferByLevel:
    """Rust is why this module has its own spec table at all."""

    def test_rust_definitions_use_the_outer_marker(self, parsers: dict) -> None:
        node = _declaration(parsers, Lang.RUST, "/// DOC\nfn f() {}\n", "function_item")
        assert extract_definition_docstring(node, Lang.RUST) == "DOC"

    def test_a_rust_definition_is_not_documented_by_the_inner_marker(
        self, parsers: dict
    ) -> None:
        """The mutation that a shared table would not survive.

        Reusing `MODULE_DOC_SPECS[RUST]` here looks for `//!` above an item
        and finds nothing, silently -- every Rust definition reads as
        undocumented. This asserts the markers are not interchangeable.
        """
        node = _declaration(
            parsers, Lang.RUST, "//! FILEDOC\nfn f() {}\n", "function_item"
        )
        assert extract_definition_docstring(node, Lang.RUST) is None


class TestInterleavedNodes:
    """Attributes, annotations and metadata sit between a doc and its subject."""

    def test_a_rust_attribute_does_not_detach_the_doc(self, parsers: dict) -> None:
        node = _declaration(
            parsers,
            Lang.RUST,
            "/// DOC\n#[derive(Debug)]\nstruct S;\n",
            "struct_item",
        )
        assert extract_definition_docstring(node, Lang.RUST) == "DOC"

    def test_an_annotation_absorbed_by_the_declaration_still_keeps_the_doc(
        self, parsers: dict
    ) -> None:
        """Java and C# keep the doc, but NOT via the interleaved walk.

        Named for what it actually exercises. Both grammars make the
        annotation a CHILD of the declaration, so the comment is already the
        preceding sibling and the skip walk never runs -- which mutation
        proved: disabling that walk leaves these green and reddens only Rust.
        Still worth asserting, because the doc must survive the annotation;
        just not evidence about the walk.
        """
        java = _declaration(
            parsers,
            Lang.JAVA,
            "class C {\n  /** DOC */\n  @Override\n  void g() {}\n}\n",
            "method_declaration",
        )
        assert extract_definition_docstring(java, Lang.JAVA) == "DOC"
        csharp = _declaration(
            parsers,
            Lang.CSHARP,
            "class C {\n  /// DOC\n  [Obsolete]\n  void G() {}\n}\n",
            "method_declaration",
        )
        assert extract_definition_docstring(csharp, Lang.CSHARP) == "DOC"


class TestMultiLineAndNoise:
    def test_consecutive_line_comments_form_one_docstring(self, parsers: dict) -> None:
        node = _declaration(
            parsers, Lang.RUST, "/// One.\n/// Two.\nfn f() {}\n", "function_item"
        )
        assert extract_definition_docstring(node, Lang.RUST) == "One.\nTwo."

    def test_a_separator_rule_does_not_truncate_the_doc(self, parsers: dict) -> None:
        node = _declaration(
            parsers,
            Lang.RUST,
            "////////\n/// Real.\nfn f() {}\n",
            "function_item",
        )
        assert extract_definition_docstring(node, Lang.RUST) == "Real."

    def test_a_go_directive_is_not_documentation(self, parsers: dict) -> None:
        node = _declaration(
            parsers,
            Lang.GO,
            "package m\n\n//go:generate stringer\nfunc F() {}\n",
            "function_declaration",
        )
        assert extract_definition_docstring(node, Lang.GO) is None

    def test_line_endings_do_not_change_the_result(self, parsers: dict) -> None:
        """Adjacency is row arithmetic, so CRLF is the platform axis here.

        This Mac cannot run the Windows job, so the endings case is checked
        directly rather than left to CI.
        """
        node = _declaration(
            parsers, Lang.RUST, "/// One.\r\n/// Two.\r\nfn f() {}\r\n", "function_item"
        )
        assert extract_definition_docstring(node, Lang.RUST) == "One.\nTwo."


class TestUnsupportedLanguages:
    def test_python_is_not_handled_here(self, parsers: dict) -> None:
        """Its docstring is a string literal in the body, not a comment."""
        node = _declaration(
            parsers,
            Lang.PYTHON,
            '# not a doc\ndef f():\n    """D."""\n',
            "function_definition",
        )
        assert extract_definition_docstring(node, Lang.PYTHON) is None

    def test_sql_has_no_convention(self, parsers: dict) -> None:
        root = _parse(parsers, Lang.SQL, "-- note\nSELECT 1;\n")
        # A CHILD, not the root: the root has no parent, so the extractor
        # returns None before it ever consults the spec table, and a SQL entry
        # added to that table would leave this test green (greptile-local).
        statement = [c for c in root.children if c.type != "comment"][0]
        assert extract_definition_docstring(statement, Lang.SQL) is None


class TestWrappedDeclarations:
    """The ingester hands over the INNER node; the doc sits beside the wrapper.

    Every case here came back None end-to-end before the wrapper climb was
    added (greptile-local, first review round).
    """

    def test_ts_export_class(self, parsers: dict) -> None:
        node = _declaration(
            parsers, Lang.TS, "/** DOC */\nexport class C {}\n", "class_declaration"
        )
        assert extract_definition_docstring(node, Lang.TS) == "DOC"

    def test_ts_export_default_function(self, parsers: dict) -> None:
        node = _declaration(
            parsers,
            Lang.TS,
            "/** DOC */\nexport default function f() {}\n",
            "function_declaration",
        )
        assert extract_definition_docstring(node, Lang.TS) == "DOC"

    def test_js_const_arrow_function(self, parsers: dict) -> None:
        node = _declaration(
            parsers, Lang.JS, "/** DOC */\nconst f = () => {};\n", "arrow_function"
        )
        assert extract_definition_docstring(node, Lang.JS) == "DOC"

    def test_js_commonjs_assigned_function(self, parsers: dict) -> None:
        node = _declaration(
            parsers,
            Lang.JS,
            "/** DOC */\nmodule.exports.f = function () {};\n",
            "function_expression",
        )
        assert extract_definition_docstring(node, Lang.JS) == "DOC"

    def test_go_type_spec(self, parsers: dict) -> None:
        node = _declaration(
            parsers, Lang.GO, "package m\n\n// DOC\ntype S struct{}\n", "type_spec"
        )
        assert extract_definition_docstring(node, Lang.GO) == "DOC"

    def test_dart_method_signature(self, parsers: dict) -> None:
        node = _declaration(
            parsers,
            Lang.DART,
            "class C {\n  /// DOC\n  void m() {}\n}\n",
            "function_signature",
        )
        assert extract_definition_docstring(node, Lang.DART) == "DOC"

    def test_a_grouped_go_type_is_not_climbed(self, parsers: dict) -> None:
        """The group's comment describes the group; a member's doc is its sibling.

        Climbing a two-member group would hand the group comment to BOTH.
        """
        src = "package m\n\n// GROUP\ntype (\n\t// DOC\n\tA struct{}\n\tB struct{}\n)\n"
        root = _parse(parsers, Lang.GO, src)
        specs = [n for n in _all(root, "type_spec")]
        assert len(specs) == 2, "fixture: expected two type_spec nodes"
        assert extract_definition_docstring(specs[0], Lang.GO) == "DOC"
        assert extract_definition_docstring(specs[1], Lang.GO) is None


class TestTrailingComments:
    """A comment on the previous line's row is that line's, never the next's.

    Doxygen `///<`, Go and Java end-of-line remarks were being written to the
    following declaration as its docstring (greptile-local, first round).
    """

    def test_cpp_doxygen_member_comment(self, parsers: dict) -> None:
        node = _declaration(
            parsers,
            Lang.CPP,
            "class K {\n  int a; ///< The a field\n  void m() {}\n};\n",
            "function_definition",
        )
        assert extract_definition_docstring(node, Lang.CPP) is None

    def test_go_end_of_line_comment(self, parsers: dict) -> None:
        node = _declaration(
            parsers,
            Lang.GO,
            "package m\n\nvar x = 1 // trailing note\nfunc Trail() {}\n",
            "function_declaration",
        )
        assert extract_definition_docstring(node, Lang.GO) is None

    def test_java_end_of_line_block_comment(self, parsers: dict) -> None:
        node = _declaration(
            parsers,
            Lang.JAVA,
            "class C {\n  int a; /** trailing */\n  void g() {}\n}\n",
            "method_declaration",
        )
        assert extract_definition_docstring(node, Lang.JAVA) is None

    def test_a_trailing_line_does_not_open_a_doc_block(self, parsers: dict) -> None:
        """The control for the walk-up: the real doc survives, the trailer does not."""
        node = _declaration(
            parsers,
            Lang.CPP,
            "class K {\n  int a; ///< trailer\n  /// Real.\n  void m() {}\n};\n",
            "function_definition",
        )
        assert extract_definition_docstring(node, Lang.CPP) == "Real."


def _all(node: ASTNode, node_type: str) -> list[ASTNode]:
    found = [node] if node.type == node_type else []
    for child in node.children:
        found.extend(_all(child, node_type))
    return found


class TestReachesTheGraph:
    """The extracted docstring lands on the definition node the ingestor writes.

    The unit tests above prove the extractor; these prove the WIRING, which is
    the half a unit test cannot see. `_get_docstring` dispatches on a
    `language` argument, and a call site that forgot to pass it would have
    fallen back to the Python path with nothing failing -- so the argument is
    required, and this drives a real `GraphUpdater` over real files to observe
    the property arriving rather than the function returning.
    """

    @pytest.fixture
    def updater(self, temp_repo, mock_ingestor):
        from codebase_rag.graph_updater import GraphUpdater

        loaded, queries = load_parsers()
        return GraphUpdater(
            ingestor=mock_ingestor,
            repo_path=temp_repo,
            parsers=loaded,
            queries=queries,
        )

    @staticmethod
    def _node_props(updater, label: str, name: str) -> dict:
        calls = updater.ingestor.ensure_node_batch.call_args_list
        found = [
            c[0][1] for c in calls if c[0][0] == label and c[0][1].get("name") == name
        ]
        # Fixture guard: a missing node is "the file was not processed", not
        # "the docstring is absent", and the two must not read the same.
        assert len(found) == 1, (
            f"expected one {label} node named {name!r}, got {len(found)}"
        )
        return found[0]

    def test_a_rust_function_node_carries_its_doc(self, temp_repo, updater) -> None:
        (temp_repo / "lib.rs").write_text("/// Adds two numbers.\nfn add() {}\n")
        updater.run()
        assert self._node_props(updater, "Function", "add")["docstring"] == (
            "Adds two numbers."
        )

    def test_a_java_class_node_carries_its_doc(self, temp_repo, updater) -> None:
        (temp_repo / "C.java").write_text("/** A class. */\nclass C {}\n")
        updater.run()
        assert self._node_props(updater, "Class", "C")["docstring"] == "A class."

    def test_the_python_path_still_works(self, temp_repo, updater) -> None:
        """The control: dispatching on language must not break the old path."""
        (temp_repo / "m.py").write_text('def f():\n    """Py doc."""\n    pass\n')
        updater.run()
        assert self._node_props(updater, "Function", "f")["docstring"] == "Py doc."

    def test_an_undocumented_definition_has_no_docstring(
        self, temp_repo, updater
    ) -> None:
        """None, not empty string -- the property is optional in the schema."""
        (temp_repo / "lib.rs").write_text("fn bare() {}\n")
        updater.run()
        assert self._node_props(updater, "Function", "bare").get("docstring") is None

    def test_wrapped_declarations_reach_their_nodes(self, temp_repo, updater) -> None:
        """The shapes the first review found undocumented end-to-end."""
        (temp_repo / "a.ts").write_text(
            "/** EC doc */\nexport class EC {}\n\n"
            "/** ef doc */\nexport function ef() {}\n\n"
            "/** EI doc */\nexport interface EI {}\n"
        )
        (temp_repo / "e.go").write_text("package m\n\n// S doc\ntype S struct{}\n")
        (temp_repo / "i.dart").write_text("class C {\n  /// m doc\n  void m() {}\n}\n")
        updater.run()
        assert self._node_props(updater, "Class", "EC")["docstring"] == "EC doc"
        assert self._node_props(updater, "Function", "ef")["docstring"] == "ef doc"
        assert self._node_props(updater, "Interface", "EI")["docstring"] == "EI doc"
        assert self._node_props(updater, "Class", "S")["docstring"] == "S doc"
        assert self._node_props(updater, "Method", "m")["docstring"] == "m doc"

    def test_a_trailing_comment_does_not_reach_the_next_node(
        self, temp_repo, updater
    ) -> None:
        (temp_repo / "e.go").write_text(
            "package m\n\nvar x = 1 // trailing note\nfunc Trail() {}\n"
        )
        updater.run()
        assert self._node_props(updater, "Function", "Trail").get("docstring") is None


class TestLibclangDocstring:
    """The pure-libclang C++ path: `Cursor.raw_comment` text -> docstring.

    libclang has already attached the comment to the right cursor, so this is
    cleaning only. Found by Greptile on PR #1888: `_node_props` hard-coded
    `docstring=None`, so with `CPP_FRONTEND=libclang` every documented C++
    definition lost its documentation.
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("/** Adds. */", "Adds."),
            ("/*! Adds. */", "Adds."),
            ("/**\n * Adds two.\n * Second line.\n */", "Adds two.\nSecond line."),
            ("/// Adds.\n/// Second.", "Adds.\nSecond."),
            ("//! Inner-style. ", "Inner-style."),
            ("////////\n/// Real.", "Real."),
        ],
        ids=[
            "block",
            "block-bang",
            "block-multiline",
            "lines",
            "line-bang",
            "separator-skipped",
        ],
    )
    def test_doxygen_forms_are_cleaned(self, raw: str, expected: str) -> None:
        assert libclang_docstring(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "   ", "/**/", "// ordinary comment"])
    def test_nothing_or_an_ordinary_comment_is_none(self, raw) -> None:
        assert libclang_docstring(raw) is None

    def test_a_test_double_attribute_is_none_not_its_repr(self) -> None:
        from unittest.mock import MagicMock

        assert libclang_docstring(MagicMock().raw_comment) is None
