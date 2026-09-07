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
