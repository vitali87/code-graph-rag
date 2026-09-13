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
from codebase_rag.parsers.definition_docstring import extract_definition_docstring
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
        """`//!` documents the enclosing module whether or not it is adjacent."""
        for source in ("//! FILEDOC\nfn f() {}\n", "//! FILEDOC\n\nfn f() {}\n"):
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

    def test_a_java_annotation_does_not_detach_the_doc(self, parsers: dict) -> None:
        node = _declaration(
            parsers,
            Lang.JAVA,
            "class C {\n  /** DOC */\n  @Override\n  void g() {}\n}\n",
            "method_declaration",
        )
        assert extract_definition_docstring(node, Lang.JAVA) == "DOC"

    def test_a_rust_attribute_does_not_detach_the_doc(self, parsers: dict) -> None:
        node = _declaration(
            parsers,
            Lang.RUST,
            "/// DOC\n#[derive(Debug)]\nstruct S;\n",
            "struct_item",
        )
        assert extract_definition_docstring(node, Lang.RUST) == "DOC"

    def test_a_csharp_attribute_does_not_detach_the_doc(self, parsers: dict) -> None:
        node = _declaration(
            parsers,
            Lang.CSHARP,
            "class C {\n  /// DOC\n  [Obsolete]\n  void G() {}\n}\n",
            "method_declaration",
        )
        assert extract_definition_docstring(node, Lang.CSHARP) == "DOC"


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
        assert extract_definition_docstring(root, Lang.SQL) is None
