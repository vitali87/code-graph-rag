# Symbols from ast-grep tier languages carry is_exported=True (the tier has no
# visibility analysis and marks them exported so dead-code does not false-flag
# every one). That makes them unconditional roots, so dead-code analysis can
# never report them -- correct, but silently zero-recall: a Ruby-heavy repo
# gets "no dead code found" with no hint that its Ruby was never analyzed.
# These cover the count that makes the exemption visible in the report.
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.cli import app
from codebase_rag.dead_code import (
    collect_dead_code,
    count_structural_tier_symbols,
    default_dead_code_config,
)
from codebase_rag.types_defs import PropertyValue, ResultRow

_FUNCTION = cs.NodeLabel.FUNCTION.value
_METHOD = cs.NodeLabel.METHOD.value
_CLASS = cs.NodeLabel.CLASS.value
_MODULE = cs.NodeLabel.MODULE.value
_CALLS = cs.RelationshipType.CALLS.value


class FakeIngestor:
    def __init__(self, nodes: list[ResultRow], rels: list[ResultRow]) -> None:
        self._nodes = nodes
        self._rels = rels

    def fetch_all(
        self, query: str, params: dict[str, PropertyValue] | None = None
    ) -> list[ResultRow]:
        if query == cq.CYPHER_DEAD_CODE_NODES:
            return self._nodes
        return self._rels


def _node(
    label: str, qn: str, name: str, path: str, *, is_exported: bool = False
) -> ResultRow:
    return {
        "label": label,
        "qualified_name": qn,
        "name": name,
        "path": path,
        "start_line": 1,
        "end_line": 2,
        "decorators": [],
        "is_exported": is_exported,
        "overrides_external": False,
    }


def _ruby_and_python_nodes() -> list[ResultRow]:
    # Ruby nodes as AstGrepTier._emit_definition writes them: is_exported=True.
    return [
        _node(_MODULE, "proj.lib.orders", "orders.rb", "lib/orders.rb"),
        _node(
            _FUNCTION,
            "proj.lib.orders.process_order",
            "process_order",
            "lib/orders.rb",
            is_exported=True,
        ),
        _node(
            _CLASS,
            "proj.lib.orders.Order",
            "Order",
            "lib/orders.rb",
            is_exported=True,
        ),
        # Method is counted alongside Function and Class, so it belongs in the
        # fixture: without it, dropping Method from the counted labels would
        # not fail a single test.
        _node(
            _METHOD,
            "proj.lib.orders.Order.total",
            "total",
            "lib/orders.rb",
            is_exported=True,
        ),
        _node(_MODULE, "proj.app", "app.py", "app.py"),
        _node(_FUNCTION, "proj.app.orphan", "orphan", "app.py"),
    ]


class TestCountStructuralTierSymbols:
    def test_counts_only_structural_tier_symbols(self) -> None:
        # The Python orphan is analyzable and must not inflate the notice; the
        # Ruby module node is not a candidate symbol either.
        count = count_structural_tier_symbols(_ruby_and_python_nodes())

        assert count == 3

    def test_zero_when_no_structural_tier_files(self) -> None:
        nodes = [
            _node(_MODULE, "proj.app", "app.py", "app.py"),
            _node(_FUNCTION, "proj.app.orphan", "orphan", "app.py"),
        ]

        assert count_structural_tier_symbols(nodes) == 0

    def test_zero_when_ast_grep_extra_missing(self) -> None:
        # Without the [ast-grep] extra the tier emits nothing, so there is
        # nothing to disclaim: degrade to no notice rather than raising.
        from codebase_rag.parsers import ast_grep_tier

        # structural_tier_extensions() is cached, so clear it around the patch
        # to exercise the real load path in both directions.
        ast_grep_tier.structural_tier_extensions.cache_clear()
        try:
            with patch.object(
                ast_grep_tier,
                "load_pattern_configs",
                side_effect=ImportError("no yaml"),
            ):
                assert count_structural_tier_symbols(_ruby_and_python_nodes()) == 0
        finally:
            ast_grep_tier.structural_tier_extensions.cache_clear()

    def test_counts_tier_symbols_without_ast_grep_py_installed(self) -> None:
        # Partial install (pyyaml present, ast_grep_py absent) must NOT gate the
        # count on the local import: the graph is shared, so a repo indexed
        # where the extra WAS installed has real tier symbols in it. Reporting
        # 0 here would hide them, which is the dishonest report this exists to
        # prevent. Locally-indexed repos are unaffected -- a disabled tier
        # emits no nodes, so there is nothing to count either way.
        from codebase_rag.parsers import ast_grep_tier

        ast_grep_tier.structural_tier_extensions.cache_clear()
        try:
            with patch.dict(sys.modules, {"ast_grep_py": None}):
                assert count_structural_tier_symbols(_ruby_and_python_nodes()) == 3
        finally:
            ast_grep_tier.structural_tier_extensions.cache_clear()

    def test_counts_are_independent_of_reported_dead_code(self) -> None:
        # The exempted Ruby symbols stay unreported (the exemption is correct);
        # only the Python orphan is dead. The notice is what surfaces the gap.
        nodes = _ruby_and_python_nodes()
        config = default_dead_code_config(include_tests=True, include_classes=True)

        rows = collect_dead_code(FakeIngestor(nodes, []), "proj", config)

        assert [row["qualified_name"] for row in rows] == ["proj.app.orphan"]
        assert count_structural_tier_symbols(nodes) == 3

    def test_extensions_track_the_tier_pattern_configs(self) -> None:
        # The count must follow whatever languages the tier ships, so adding a
        # new ast_grep_patterns/*.yaml needs no change here.
        from codebase_rag.parsers.ast_grep_tier import structural_tier_extensions

        extensions = structural_tier_extensions()

        assert ".rb" in extensions
        assert ".py" not in extensions


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain_text(output: str) -> str:
    # Strip styling and collapse rich's wrap breaks so assertions match the
    # message as written rather than as laid out for the terminal width.
    return " ".join(_ANSI_RE.sub("", output).split())


def _mock_ingestor(nodes: list[ResultRow]) -> MagicMock:
    mock = MagicMock()
    mock.list_projects.return_value = ["proj"]

    def _fetch(
        query: str, params: dict[str, PropertyValue] | None = None
    ) -> list[ResultRow]:
        return nodes if query == cq.CYPHER_DEAD_CODE_NODES else []

    mock.fetch_all.side_effect = _fetch
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    return mock


class TestDeadCodeCommandNotice:
    def test_notice_shown_when_nothing_reported(self) -> None:
        # The case the notice exists for: every Ruby symbol is exempt, so the
        # report says "none found" and must not imply Ruby was covered.
        ruby_only = [
            _node(_MODULE, "proj.lib.orders", "orders.rb", "lib/orders.rb"),
            _node(
                _FUNCTION,
                "proj.lib.orders.process_order",
                "process_order",
                "lib/orders.rb",
                is_exported=True,
            ),
        ]
        with patch(
            "codebase_rag.cli.connect_memgraph", return_value=_mock_ingestor(ruby_only)
        ):
            result = CliRunner().invoke(app, ["dead-code"])

        assert result.exit_code == 0
        # Rich styles and wraps the line, so match on plain text with ANSI
        # escapes and inserted breaks removed.
        plain = _plain_text(result.output)
        assert cs.CLI_DEADCODE_NONE in plain
        assert "1 symbol(s) in structural-tier languages were not analyzed" in plain

    def test_no_notice_without_structural_tier_symbols(self) -> None:
        python_only = [
            _node(_MODULE, "proj.app", "app.py", "app.py"),
            _node(_FUNCTION, "proj.app.orphan", "orphan", "app.py"),
        ]
        with patch(
            "codebase_rag.cli.connect_memgraph",
            return_value=_mock_ingestor(python_only),
        ):
            result = CliRunner().invoke(app, ["dead-code"])

        assert result.exit_code == 0
        assert "structural-tier" not in _plain_text(result.output)

    def test_json_output_stays_parseable(self) -> None:
        # The notice must not contaminate machine-readable output.
        nodes = _ruby_and_python_nodes()
        with patch(
            "codebase_rag.cli.connect_memgraph", return_value=_mock_ingestor(nodes)
        ):
            result = CliRunner().invoke(app, ["dead-code", "--format", "json"])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert [row["qualified_name"] for row in payload] == ["proj.app.orphan"]

    def test_saved_table_report_carries_the_notice(self, tmp_path: Path) -> None:
        # A report read from a file (CI artifact, shared review) must carry the
        # same disclaimer as the console: otherwise the saved copy shows an
        # empty table and reads as "this code is clean".
        with patch(
            "codebase_rag.cli.connect_memgraph",
            return_value=_mock_ingestor(_ruby_and_python_nodes()),
        ):
            result = CliRunner().invoke(
                app, ["dead-code", "--output", str(tmp_path / "report.txt")]
            )

        assert result.exit_code == 0
        saved = _plain_text((tmp_path / "report.txt").read_text(encoding="utf-8"))
        assert "3 symbol(s) in structural-tier languages were not analyzed" in saved

    def test_saved_json_report_stays_parseable(self, tmp_path: Path) -> None:
        # The JSON artifact must remain machine-readable, so the notice belongs
        # only in the human-facing table format.
        out = tmp_path / "report.json"
        with patch(
            "codebase_rag.cli.connect_memgraph",
            return_value=_mock_ingestor(_ruby_and_python_nodes()),
        ):
            result = CliRunner().invoke(
                app, ["dead-code", "--format", "json", "--output", str(out)]
            )

        assert result.exit_code == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert [row["qualified_name"] for row in payload] == ["proj.app.orphan"]

    def test_notice_alone_does_not_fail_the_build(self) -> None:
        # The notice is informational: exempt symbols are not findings, so
        # --fail-on-found must still exit 0 when nothing is actually dead.
        # Otherwise indexing a Ruby file would break someone's CI gate.
        ruby_only = [
            _node(_MODULE, "proj.lib.orders", "orders.rb", "lib/orders.rb"),
            _node(
                _FUNCTION,
                "proj.lib.orders.process_order",
                "process_order",
                "lib/orders.rb",
                is_exported=True,
            ),
        ]
        with patch(
            "codebase_rag.cli.connect_memgraph", return_value=_mock_ingestor(ruby_only)
        ):
            result = CliRunner().invoke(app, ["dead-code", "--fail-on-found"])

        assert result.exit_code == 0
        assert "structural-tier" in _plain_text(result.output)

    def test_real_candidate_still_fails_the_build(self) -> None:
        # Guard the other direction: the notice must not mask a real finding.
        with patch(
            "codebase_rag.cli.connect_memgraph",
            return_value=_mock_ingestor(_ruby_and_python_nodes()),
        ):
            result = CliRunner().invoke(app, ["dead-code", "--fail-on-found"])

        assert result.exit_code == 1
