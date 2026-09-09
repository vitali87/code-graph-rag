"""Module-level docstrings reach the `Module` node (issue #1789).

`_get_docstring` starts from the `body` field, which a tree-sitter `module`
node does not have -- its statements are direct children -- so every
file-level docstring was dropped while the functions inside the same file kept
theirs.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tree_sitter import Language, Parser

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

try:
    import tree_sitter_python as tspython

    PY_AVAILABLE = True
except ImportError:
    PY_AVAILABLE = False


@pytest.fixture
def py_parser() -> Parser | None:
    if not PY_AVAILABLE:
        return None
    language = Language(tspython.language())
    return Parser(language)


@pytest.fixture
def updater(temp_repo: Path, mock_ingestor: MagicMock) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=temp_repo,
        parsers=parsers,
        queries=queries,
    )


def _module_props(updater: GraphUpdater, name: str) -> dict:
    """The properties of the one `Module` node written for `name`."""
    calls = updater.ingestor.ensure_node_batch.call_args_list
    modules = [
        c[0][1] for c in calls if c[0][0] == "Module" and c[0][1].get("name") == name
    ]
    assert len(modules) == 1, f"expected one Module node for {name}, got {len(modules)}"
    return modules[0]


@pytest.mark.skipif(not PY_AVAILABLE, reason="tree-sitter-python not available")
class TestModuleDocstringExtraction:
    """The extractor reads a docstring from a `module` node."""

    def _extract(self, py_parser: Parser, code: bytes) -> str | None:
        from codebase_rag.parsers.definition_processor import DefinitionProcessor

        tree = py_parser.parse(code)
        processor = DefinitionProcessor.__new__(DefinitionProcessor)
        return processor._get_docstring(tree.root_node)

    def test_triple_quoted_module_docstring(self, py_parser: Parser) -> None:
        code = b'"""Module summary."""\n\nimport os\n'
        assert self._extract(py_parser, code) == "Module summary."

    def test_single_quoted_module_docstring(self, py_parser: Parser) -> None:
        code = b"'Single quoted module doc'\n\nimport os\n"
        assert self._extract(py_parser, code) == "Single quoted module doc"

    def test_docstring_before_imports_not_confused_with_function(
        self, py_parser: Parser
    ) -> None:
        """The module's own docstring, not the first function's."""
        code = b'"""Module doc."""\n\n\ndef f():\n    """Function doc."""\n'
        assert self._extract(py_parser, code) == "Module doc."

    def test_no_docstring(self, py_parser: Parser) -> None:
        code = b"import os\n\n\ndef f():\n    pass\n"
        assert self._extract(py_parser, code) is None

    def test_empty_file(self, py_parser: Parser) -> None:
        assert self._extract(py_parser, b"") is None

    def test_first_statement_is_not_a_string(self, py_parser: Parser) -> None:
        """A leading expression that is not a string is not a docstring."""
        code = b"x = 1\n"
        assert self._extract(py_parser, code) is None

    def test_leading_comment_is_not_a_docstring(self, py_parser: Parser) -> None:
        """A `#` comment is not a docstring, even as the first line."""
        code = b"# just a comment\nimport os\n"
        assert self._extract(py_parser, code) is None

    def test_function_docstring_still_extracted(self, py_parser: Parser) -> None:
        """The existing body-field path is unchanged."""
        from codebase_rag.parsers.definition_processor import DefinitionProcessor

        code = b'def f():\n    """Function doc."""\n    pass\n'
        tree = py_parser.parse(code)
        processor = DefinitionProcessor.__new__(DefinitionProcessor)
        assert processor._get_docstring(tree.root_node.children[0]) == "Function doc."


@pytest.mark.skipif(not PY_AVAILABLE, reason="tree-sitter-python not available")
class TestModuleDocstringOnNode:
    """The extracted docstring reaches the `Module` node the ingestor writes."""

    def test_module_node_carries_docstring(
        self, temp_repo: Path, updater: GraphUpdater
    ) -> None:
        (temp_repo / "documented.py").write_text(
            '"""What this module is for."""\n\nimport os\n', encoding="utf-8"
        )
        updater.run()

        assert (
            _module_props(updater, "documented.py")["docstring"]
            == "What this module is for."
        )

    def test_module_without_docstring_has_no_property(
        self, temp_repo: Path, updater: GraphUpdater
    ) -> None:
        """Absent, not empty string -- the property is optional in the schema."""
        (temp_repo / "bare.py").write_text("import os\n", encoding="utf-8")
        updater.run()

        assert _module_props(updater, "bare.py").get("docstring") is None

    def test_module_docstring_differs_from_its_function_docstring(
        self, temp_repo: Path, updater: GraphUpdater
    ) -> None:
        """Guards against the Module node picking up the first function's doc."""
        (temp_repo / "both.py").write_text(
            '"""Module level."""\n\n\ndef f():\n    """Function level."""\n    pass\n',
            encoding="utf-8",
        )
        updater.run()

        assert _module_props(updater, "both.py")["docstring"] == "Module level."


class TestModuleDocstringDeclared:
    """The schema declares the property the writer sets."""

    def test_module_schema_declares_docstring(self) -> None:
        from codebase_rag.schema_parse import parsed_node_schemas
        from codebase_rag.types_defs import NodeLabel

        specs = parsed_node_schemas()[NodeLabel.MODULE]
        docstring = [s for s in specs if s.name == "docstring"]

        assert docstring, "Module schema does not declare a docstring property"
        assert docstring[0].type_name == "string"
        assert docstring[0].optional


class TestModuleDocSpecCoverage:
    """Every supported language is a deliberate decision, not an omission."""

    def test_every_language_is_decided(self) -> None:
        """A new language must be added to the spec table or the skip list."""
        from codebase_rag.constants import SupportedLanguage
        from codebase_rag.parsers.module_docstring import MODULE_DOC_SPECS

        # Python's docstring is a string literal, not a comment, so it is
        # handled by `_get_docstring` rather than by a spec. SQL has no
        # module-documentation convention at all.
        handled_elsewhere = {SupportedLanguage.PYTHON, SupportedLanguage.SQL}
        undecided = set(SupportedLanguage) - set(MODULE_DOC_SPECS) - handled_elsewhere

        assert not undecided, f"languages with no module-doc decision: {undecided}"


@pytest.mark.skipif(not PY_AVAILABLE, reason="tree-sitter-python not available")
class TestModuleDocstringPerLanguage:
    """Each language's own module-doc convention, and what must NOT match."""

    def _extract(self, lang_name: str, source: bytes) -> str | None:
        from codebase_rag.constants import SupportedLanguage
        from codebase_rag.parser_loader import load_parsers
        from codebase_rag.parsers.module_docstring import extract_module_docstring

        parsers, _ = load_parsers()
        language = SupportedLanguage(lang_name)
        root = parsers[language].parse(source).root_node
        return extract_module_docstring(root, language)

    # --- positives: the documented convention of each language ---

    def test_rust_inner_line_doc(self) -> None:
        got = self._extract("rust", b"//! Crate docs.\n//! Second line.\n\nfn f() {}\n")
        assert got == "Crate docs.\nSecond line."

    def test_rust_inner_block_doc(self) -> None:
        assert (
            self._extract("rust", b"/*! Crate docs. */\nfn f() {}\n") == "Crate docs."
        )

    def test_go_package_comment(self) -> None:
        got = self._extract("go", b"// Package m does things.\n// More.\npackage m\n")
        assert got == "Package m does things.\nMore."

    def test_java_javadoc(self) -> None:
        source = b"/**\n * Package docs.\n * More.\n */\npackage com.x;\n"
        assert self._extract("java", source) == "Package docs.\nMore."

    def test_javascript_jsdoc(self) -> None:
        source = b'/**\n * Module docs.\n */\n\nimport os from "os";\n'
        assert self._extract("javascript", source) == "Module docs."

    def test_typescript_jsdoc(self) -> None:
        source = b'/**\n * Module docs.\n */\n\nimport os from "os";\n'
        assert self._extract("typescript", source) == "Module docs."

    def test_c_doxygen(self) -> None:
        source = b"/**\n * File docs.\n */\n#include <stdio.h>\n"
        assert self._extract("c", source) == "File docs."

    def test_cpp_doxygen(self) -> None:
        source = b"/**\n * File docs.\n */\n#include <cstdio>\n"
        assert self._extract("cpp", source) == "File docs."

    def test_csharp_xml_doc(self) -> None:
        source = b"/// <summary>File docs.</summary>\nnamespace N;\n"
        assert self._extract("c_sharp", source) == "<summary>File docs.</summary>"

    def test_scala_scaladoc(self) -> None:
        source = b"/**\n * Package docs.\n */\npackage x\n"
        assert self._extract("scala", source) == "Package docs."

    def test_dart_library_doc(self) -> None:
        got = self._extract("dart", b"/// Library docs.\n/// More.\nlibrary x;\n")
        assert got == "Library docs.\nMore."

    def test_lua_ldoc(self) -> None:
        got = self._extract("lua", b"--- Module docs.\nlocal M = {}\n")
        assert got == "Module docs."

    def test_php_docblock_after_open_tag(self) -> None:
        """The `<?php` tag is the first child; the doc comment follows it."""
        source = b"<?php\n/**\n * File docs.\n */\ndeclare(strict_types=1);\n"
        assert self._extract("php", source) == "File docs."

    # --- negatives: what must NOT be read as module documentation ---

    def test_rust_item_doc_is_not_a_module_doc(self) -> None:
        """`///` documents the NEXT ITEM; only `//!` documents the module."""
        source = b"/// Docs for the function.\nfn f() {}\n"
        assert self._extract("rust", source) is None

    def test_rust_plain_comment(self) -> None:
        assert self._extract("rust", b"// just a note\nfn f() {}\n") is None

    def test_go_detached_comment_is_a_licence_header(self) -> None:
        """A blank line before `package` means it is not the package doc."""
        source = b"// Copyright 2020 the authors.\n\npackage m\n"
        assert self._extract("go", source) is None

    def test_javascript_plain_comment(self) -> None:
        source = b'// eslint-disable-next-line\nimport x from "y";\n'
        assert self._extract("javascript", source) is None

    def test_javascript_licence_block_is_not_a_doc(self) -> None:
        """A bare `/*` block is a licence header, not JSDoc."""
        source = b'/* Copyright 2020 Foo Inc. */\nimport x from "y";\n'
        assert self._extract("javascript", source) is None

    def test_lua_plain_comment(self) -> None:
        assert self._extract("lua", b"-- a note\nlocal M = {}\n") is None

    def test_dart_plain_comment(self) -> None:
        assert self._extract("dart", b"// a note\nlibrary x;\n") is None

    def test_c_plain_comment(self) -> None:
        assert self._extract("c", b"// a note\n#include <stdio.h>\n") is None

    def test_sql_has_no_convention(self) -> None:
        """A leading `--` in SQL is as likely to be commented-out code."""
        assert self._extract("sql", b"-- File docs.\nSELECT 1;\n") is None

    def test_empty_file_every_language(self) -> None:
        for lang in ("rust", "go", "java", "javascript", "c", "lua", "dart"):
            assert self._extract(lang, b"") is None, lang

    def test_code_first_is_not_a_doc(self) -> None:
        assert self._extract("rust", b"fn f() {}\n") is None
        assert self._extract("go", b"package m\n") is None
        assert self._extract("javascript", b'import x from "y";\n') is None

    # --- blank lines end a doc block ---

    def test_rust_blank_line_ends_the_block(self) -> None:
        """A blank line separates the module doc from a later comment.

        Regression: Rust's `line_comment` includes its trailing newline, so
        `end_point` is already the next row and a gap measured from it counted
        one line short -- the two paragraphs read as adjacent and were joined.
        """
        source = b"//! First para.\n\n//! Second para.\nfn f() {}\n"
        assert self._extract("rust", source) == "First para."

    def test_rust_adjacent_lines_still_join(self) -> None:
        """The control for the regression above: no blank line, so they join."""
        source = b"//! A.\n//! B.\n//! C.\nfn f() {}\n"
        assert self._extract("rust", source) == "A.\nB.\nC."

    def test_dart_blank_line_ends_the_block(self) -> None:
        source = b"/// A.\n\n/// B.\nlibrary x;\n"
        assert self._extract("dart", source) == "A."

    def test_go_comment_detached_by_blank_line_within_block(self) -> None:
        """The block ends at the blank line, leaving it detached from `package`."""
        assert self._extract("go", b"// A.\n\n// B.\npackage m\n") is None

    # --- review findings: things that must not suppress or fake a doc ---

    def test_shebang_does_not_suppress_the_docstring(self) -> None:
        """A CLI entry point has both a shebang and a module doc.

        Each grammar names the shebang differently (`hash_bang_line`,
        `shebang`, `shebang_directive`); an unskipped one leaves the first
        child a non-comment and the documentation was silently dropped.
        """
        cases = [
            ("javascript", b"#!/usr/bin/env node\n/**\n * Docs.\n */\nlet x = 1;\n"),
            ("typescript", b"#!/usr/bin/env ts-node\n/**\n * Docs.\n */\nlet x = 1;\n"),
            ("rust", b"#!/usr/bin/env rust\n//! Docs.\nfn f() {}\n"),
            ("lua", b"#!/usr/bin/env lua\n--- Docs.\nlocal M = {}\n"),
            ("php", b"#!/usr/bin/env php\n<?php\n/** Docs. */\n"),
            ("c_sharp", b"#!/usr/bin/env dotnet\n/// Docs.\nnamespace N;\n"),
        ]
        for lang, source in cases:
            assert self._extract(lang, source) == "Docs.", lang

    def test_go_directives_are_not_documentation(self) -> None:
        """`//go:generate`, linter pragmas and the generated banner are input
        to tooling, not a description of the file."""
        assert (
            self._extract("go", b"//go:generate mockgen -source=x.go\npackage t\n")
            is None
        )
        assert self._extract("go", b"//nolint:gochecknoglobals\npackage t\n") is None
        source = b"// Code generated by protoc-gen-go. DO NOT EDIT.\npackage gen\n"
        assert self._extract("go", source) is None

    def test_a_real_doc_after_a_directive_is_still_found(self) -> None:
        """Directives are skipped, not treated as the end of the comment."""
        source = b"//go:generate x\n// Package tool does things.\npackage tool\n"
        assert self._extract("go", source) == "Package tool does things."

    def test_typescript_triple_slash_reference_is_a_directive(self) -> None:
        """`///` in TS is `<reference/>`, not a doc convention; `/**` is."""
        source = b'/// <reference types="node" />\nexport const x = 1;\n'
        assert self._extract("typescript", source) is None
        assert self._extract("javascript", b"/// Not a doc.\nlet x = 1;\n") is None

    def test_separator_rules_are_not_documentation(self) -> None:
        """A line of repeated delimiters is decoration, not prose."""
        source = b"--------------------\n-- ordinary note\nlocal M = {}\n"
        assert self._extract("lua", source) is None
        assert self._extract("c_sharp", b"////////////////\nnamespace N;\n") is None

    def test_doc_after_a_separator_rule_is_still_found(self) -> None:
        """A separator opening the block is skipped, not fatal."""
        source = b"---------\n--- Real docs.\nlocal M = {}\n"
        assert self._extract("lua", source) == "Real docs."

    def test_dart_block_comment_doc(self) -> None:
        """Dart labels a `/** */` doc `documentation_comment`, not `comment`."""
        source = b"/**\n * Library docs.\n */\nlibrary x;\n"
        assert self._extract("dart", source) == "Library docs."

    # --- documentation that belongs to a declaration, not to the file ---

    @pytest.mark.parametrize(
        ("lang", "source"),
        [
            ("java", b"/** Class docs */\nclass C {}\n"),
            ("scala", b"/** Class docs */\nclass C\n"),
            ("c", b"/** Func docs */\nint f(void) { return 0; }\n"),
            ("cpp", b"/** Class docs */\nclass C {};\n"),
            ("c_sharp", b"/** Class docs */\nclass C {}\n"),
            ("php", b"<?php\n/** Class docs */\nclass C {}\n"),
            ("javascript", b"/** Class docs */\nclass C {}\n"),
            ("typescript", b"/** Iface docs */\ninterface I {}\n"),
            ("dart", b"/// Class docs.\nclass C {}\n"),
        ],
    )
    def test_declaration_doc_is_not_the_module_doc(
        self, lang: str, source: bytes
    ) -> None:
        """A doc comment touching a declaration documents it, not the file.

        Recording `/** Class docs */` on the Module node attributes a class's
        own description to its file, which reads as file-level context to
        every consumer of the graph.
        """
        assert self._extract(lang, source) is None

    @pytest.mark.parametrize(
        ("lang", "source", "expected"),
        [
            ("java", b"/** File docs */\npackage p;\n", "File docs"),
            ("scala", b"/** File docs */\npackage p\n", "File docs"),
            ("c", b"/** File docs */\n#include <s.h>\n", "File docs"),
            ("cpp", b"/** File docs */\n#include <s>\n", "File docs"),
            ("c_sharp", b"/** File docs */\nusing S;\n", "File docs"),
            (
                "php",
                b"<?php\n/** File docs */\ndeclare(strict_types=1);\n",
                "File docs",
            ),
            ("javascript", b"/** File docs */\nlet x = 1;\n", "File docs"),
            ("javascript", b"/** File docs */\nexport const x = 1;\n", "File docs"),
            ("typescript", b"/** File docs */\nconst c = 1;\n", "File docs"),
            ("dart", b"/// File docs.\nvar x = 1;\n", "File docs."),
            ("dart", b"/// File docs.\nimport 'a.dart';\n", "File docs."),
            ("lua", b"--- File docs.\nlocal M = {}\n", "File docs."),
            ("rust", b"//! Crate docs.\nfn f() {}\n", "Crate docs."),
        ],
    )
    def test_file_doc_above_ordinary_code_is_still_found(
        self, lang: str, source: bytes, expected: str
    ) -> None:
        """The other direction: ordinary code below a doc must not suppress it.

        The check is a deny-list of declaration types rather than an allow-list
        of legal followers, because any statement may open a documented file.
        An allow-list drops the doc for every construct nobody listed.
        """
        assert self._extract(lang, source) == expected

    @pytest.mark.parametrize(
        ("lang", "source", "expected"),
        [
            ("java", b"/** File docs */\n\nclass C {}\n", "File docs"),
            ("javascript", b"/** File docs */\n\nclass C {}\n", "File docs"),
            ("dart", b"/// File docs.\n\nclass C {}\n", "File docs."),
        ],
    )
    def test_detached_doc_above_a_declaration_is_a_file_doc(
        self, lang: str, source: bytes, expected: str
    ) -> None:
        """The blank line is the distinction, as it is for Go's package comment."""
        assert self._extract(lang, source) == expected

    # --- a bare marker line is a paragraph break, not a separator ---

    def test_go_package_comment_survives_a_blank_doc_line(self) -> None:
        """The standard Go package comment form, which a bare `//` must not end.

        `_is_separator` accepting a bare `//` dropped this entirely: the block
        ended at the first line, so the anchor check no longer saw `package`.
        """
        source = b"// Package m does things.\n//\n// More details.\npackage m\n"
        assert self._extract("go", source) == "Package m does things.\n\nMore details."

    def test_rust_crate_doc_survives_a_blank_doc_line(self) -> None:
        source = b"//! First para.\n//!\n//! Second para.\n"
        assert self._extract("rust", source) == "First para.\n\nSecond para."

    def test_rust_crate_doc_after_an_inner_attribute(self) -> None:
        """`#![no_std]` legally precedes the crate doc."""
        assert self._extract("rust", b"#![no_std]\n//! Crate docs.\n") == "Crate docs."

    @pytest.mark.parametrize(
        ("lang", "source"),
        [
            ("javascript", b"/** Class docs */\nexport class C {}\n"),
            ("javascript", b"/** Fn docs */\nexport function f() {}\n"),
            ("javascript", b"/** Def docs */\nexport default class D {}\n"),
            ("typescript", b"/** Iface docs */\nexport interface I {}\n"),
            ("typescript", b"/** Type docs */\nexport type T = number;\n"),
            ("typescript", b"/** Enum docs */\nexport enum E { A }\n"),
        ],
    )
    def test_exported_declaration_doc_is_not_the_module_doc(
        self, lang: str, source: bytes
    ) -> None:
        """`export` wraps the declaration; the doc still belongs to it.

        `export class C {}` and `export const x = 1` are both
        `export_statement`, so the wrapper's type decides nothing. The exported
        declaration is its last named child, and that is what the deny-list is
        tested against -- `export const` and `export {}` unwrap to nodes that
        are not declarations and so remain file documentation.
        """
        assert self._extract(lang, source) is None

    @pytest.mark.parametrize(
        ("lang", "source"),
        [
            ("scala", b"/** Ext docs */\nextension (x: Int) { def double = x * 2 }\n"),
            ("java", b"/** Module docs */\nmodule m {}\n"),
            ("dart", b"/// Ext docs.\nextension type ET(int i) {}\n"),
            ("cpp", b'/** Linkage docs */\nextern "C" { int h(); }\n'),
        ],
    )
    def test_less_common_declaration_forms_are_denied(
        self, lang: str, source: bytes
    ) -> None:
        """Spellings the first pass of the deny-list missed.

        C#'s file-scoped `namespace N;` is the modern default and parses as
        `file_scoped_namespace_declaration`, a different node from the braced
        form; the rest are ordinary declarations whose grammar names do not
        resemble their siblings'.
        """
        assert self._extract(lang, source) is None

    @pytest.mark.parametrize(
        ("lang", "source"),
        [
            ("java", b"/** Class docs */ class C {}\n"),
            ("javascript", b"/** Class docs */ class C {}\n"),
            ("c", b"/** Fn docs */ int f(void) { return 0; }\n"),
        ],
    )
    def test_doc_on_the_same_line_as_its_declaration(
        self, lang: str, source: bytes
    ) -> None:
        """Adjacency is not only the line below: the same line is closer still."""
        assert self._extract(lang, source) is None

    def test_same_line_ordinary_code_keeps_the_file_doc(self) -> None:
        """The same-line rule must not swallow a doc above ordinary code."""
        assert self._extract("javascript", b"/** File docs */ let x = 1;\n") == (
            "File docs"
        )

    @pytest.mark.parametrize(
        ("lang", "source", "expected"),
        [
            ("javascript", b"/** File docs */\nexport const x = 1;\n", "File docs"),
            ("typescript", b"/** File docs */\nexport {};\n", "File docs"),
        ],
    )
    def test_export_of_a_non_declaration_is_still_a_file_doc(
        self, lang: str, source: bytes, expected: str
    ) -> None:
        """`export const` / `export {}` unwrap to nodes that are not declarations."""
        assert self._extract(lang, source) == expected

    def test_file_scoped_namespace_keeps_the_file_doc(self) -> None:
        """`namespace N;` scopes the file; `namespace N { }` is a block.

        The file-scoped form has no body, so the file's declarations are its
        siblings rather than its children and a doc above it describes the
        file. The braced form contains them, so a doc above it documents the
        block. Same construct in C#, opposite answers here.
        """
        assert self._extract("c_sharp", b"/** File docs */\nnamespace N;\n") == (
            "File docs"
        )
        assert self._extract("c_sharp", b"/** Ns docs */\nnamespace N {}\n") is None

    def test_crlf_does_not_shift_the_documentation_row(self) -> None:
        """A `\\r` kept in the node text is not a newline.

        Dart's `documentation_comment` includes the `\\r` of a CRLF line but
        still ends on its own row, so subtracting a row for any trailing
        whitespace put the doc an impossible row above itself: it truncated a
        multi-line doc and defeated the declaration check.
        """
        assert self._extract("dart", b"/// A.\r\n/// B.\r\nvar x = 1;\r\n") == "A.\nB."
        assert self._extract("dart", b"/// Class docs.\r\nclass C {}\r\n") is None
        assert (
            self._extract("dart", b"/// File docs.\r\nvar x = 1;\r\n") == "File docs."
        )

    @pytest.mark.parametrize(
        "source",
        [
            b"/**/\npackage p;\n",
            b"/***/\npackage p;\n",
            b"/****/\npackage p;\n",
            b"/* */\npackage p;\n",
        ],
    )
    def test_an_empty_block_comment_is_not_documentation(self, source: bytes) -> None:
        """An empty comment must leave the docstring unset, not set to `/`.

        `_BLOCK_OPEN` takes every asterisk of `/**/`, so the closing delimiter
        arrives at `_strip_block_close` as a bare `/` with no asterisk in front
        of it. Left there, that slash became the file's documentation.
        """
        assert self._extract("java", source) is None
