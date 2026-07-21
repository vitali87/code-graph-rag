# ast-grep pattern-driven language tier (issue #414). Languages without a
# tree-sitter LanguageSpec (here: Ruby) are extracted structurally from
# per-language YAML pattern configs, emitting the same Module/Function/Class
# nodes and DEFINES/IMPORTS relationships the tree-sitter path emits. These
# tests index a real .rb file end to end and assert those nodes/edges land.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

FUNCTION = cs.NodeLabel.FUNCTION.value
CLASS = cs.NodeLabel.CLASS.value
MODULE = cs.NodeLabel.MODULE.value
DEFINES = cs.RelationshipType.DEFINES.value
IMPORTS = cs.RelationshipType.IMPORTS.value

RUBY = (
    'require "json"\n'
    'require_relative "helper"\n'
    "\n"
    "module Greeter\n"
    "  class Formal\n"
    "    def self.build\n"
    "      new\n"
    "    end\n"
    "    def greet(name)\n"
    '      "Hello, #{name}"\n'
    "    end\n"
    "  end\n"
    "end\n"
    "\n"
    "def bare_method\n"
    "  42\n"
    "end\n"
)


def _run(tmp_path: Path, files: dict[str, str]) -> MagicMock:
    parsers, queries = load_parsers()
    for rel, content in files.items():
        (tmp_path / rel).write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock, repo_path=tmp_path, parsers=parsers, queries=queries
    ).run()
    return mock


def _node_names(mock: MagicMock, label: str) -> set[str]:
    return {
        c.args[1].get(cs.KEY_NAME)
        for c in mock.ensure_node_batch.call_args_list
        if str(c.args[0]) == label
    }


def _rels(mock: MagicMock, rel_type: str) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == rel_type
    }


def test_ruby_functions_extracted(tmp_path: Path) -> None:
    mock = _run(tmp_path, {"greeter.rb": RUBY})
    names = _node_names(mock, FUNCTION)
    assert {"greet", "build", "bare_method"} <= names, names


def test_ruby_classes_and_modules_extracted(tmp_path: Path) -> None:
    mock = _run(tmp_path, {"greeter.rb": RUBY})
    names = _node_names(mock, CLASS)
    assert {"Formal", "Greeter"} <= names, names


def test_ruby_module_node_emitted(tmp_path: Path) -> None:
    mock = _run(tmp_path, {"greeter.rb": RUBY})
    assert "greeter.rb" in _node_names(mock, MODULE)


def test_ruby_defines_edges(tmp_path: Path) -> None:
    mock = _run(tmp_path, {"greeter.rb": RUBY})
    defined = {to for _from, to in _rels(mock, DEFINES)}
    assert any(qn.endswith(".bare_method") for qn in defined), defined
    assert any(qn.endswith(".Formal") for qn in defined), defined


def test_ruby_imports_edges(tmp_path: Path) -> None:
    mock = _run(tmp_path, {"greeter.rb": RUBY})
    targets = {to for _from, to in _rels(mock, IMPORTS)}
    assert any(t.endswith("json") for t in targets), targets
    assert any(t.endswith("helper") for t in targets), targets


def test_class_and_method_on_same_line_both_land(tmp_path: Path) -> None:
    # one-liner: class and def share a start line; per-label dedup must
    # keep both instead of the function claiming the line for the class too.
    mock = _run(tmp_path, {"oneliner.rb": "class Boxed; def unwrap; end; end\n"})
    assert "unwrap" in _node_names(mock, FUNCTION)
    assert "Boxed" in _node_names(mock, CLASS)


def test_shipped_ruby_config_loads() -> None:
    from codebase_rag.parsers.ast_grep_tier import load_pattern_configs

    configs = load_pattern_configs()
    assert ".rb" in configs
    assert configs[".rb"].ast_grep_id == "ruby"


def test_config_missing_required_keys_raises(tmp_path: Path, monkeypatch) -> None:
    from codebase_rag.parsers import ast_grep_tier

    (tmp_path / "broken.yaml").write_text("functions: ['def $NAME']\n")
    monkeypatch.setattr(ast_grep_tier, "_PATTERNS_DIR", tmp_path)
    try:
        ast_grep_tier.load_pattern_configs()
    except ValueError as exc:
        assert "required" in str(exc)
    else:
        raise AssertionError("expected ValueError for missing extensions/ast_grep_id")


def test_strip_quotes() -> None:
    from codebase_rag.parsers.ast_grep_tier import _strip_quotes

    assert _strip_quotes('"json"') == "json"
    assert _strip_quotes("'json'") == "json"
    assert _strip_quotes("json") == "json"
