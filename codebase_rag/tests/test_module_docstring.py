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
