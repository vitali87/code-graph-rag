"""Calls that name their target in a string argument."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.parsers.string_call import (
    StringCallSpec,
    load_string_call_specs,
    string_call_target,
)

SPECS = (StringCallSpec(callee="callSp", arg_index=0),)


def _write_config(tmp_path: Path, body: str) -> Path:
    (tmp_path / ".cgr.toml").write_text(body, encoding="utf-8")
    return tmp_path


def _ts_call_node(source: str):
    """The first call_expression in a TypeScript snippet."""
    tree_sitter = pytest.importorskip("tree_sitter")
    tree_sitter_typescript = pytest.importorskip("tree_sitter_typescript")

    language = tree_sitter.Language(tree_sitter_typescript.language_typescript())
    tree = tree_sitter.Parser(language).parse(source.encode())

    def walk(node):
        if node.type == "call_expression":
            yield node
        for child in node.children:
            yield from walk(child)

    return next(walk(tree.root_node))


class TestLoadStringCallSpecs:
    def test_no_config_file_disables_the_feature(self, tmp_path: Path) -> None:
        assert load_string_call_specs(tmp_path) == ()

    def test_reads_declared_dispatchers(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            '[[string_calls]]\ncallee = "callSp"\narg_index = 0\n'
            '[[string_calls]]\ncallee = "callProc"\narg_index = 1\n',
        )
        specs = load_string_call_specs(root)
        assert specs == (
            StringCallSpec(callee="callSp", arg_index=0),
            StringCallSpec(callee="callProc", arg_index=1),
        )

    def test_arg_index_defaults_to_first_argument(self, tmp_path: Path) -> None:
        root = _write_config(tmp_path, '[[string_calls]]\ncallee = "callSp"\n')
        assert load_string_call_specs(root) == (StringCallSpec("callSp", 0),)

    def test_malformed_config_disables_instead_of_failing(self, tmp_path: Path) -> None:
        # An unreadable config must not abort an ingestion: the feature is an
        # optional refinement, never a prerequisite.
        root = _write_config(tmp_path, "[[string_calls]\ncallee =")
        assert load_string_call_specs(root) == ()

    def test_entries_without_a_callee_are_skipped(self, tmp_path: Path) -> None:
        root = _write_config(tmp_path, "[[string_calls]]\narg_index = 2\n")
        assert load_string_call_specs(root) == ()

    def test_config_path_env_overrides_the_repository_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A read-only or third-party checkout cannot carry a config of its own.
        external = tmp_path / "external.toml"
        external.write_text('[[string_calls]]\ncallee = "fromEnv"\n', encoding="utf-8")
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setenv("CGR_CONFIG_PATH", str(external))
        assert load_string_call_specs(repo) == (StringCallSpec("fromEnv", 0),)


class TestStringCallTarget:
    def test_resolves_the_named_routine(self) -> None:
        node = _ts_call_node('callSp("usp_invoice_list", params);')
        assert string_call_target(node, "callSp", SPECS) == "usp_invoice_list"

    def test_resolves_when_awaited(self) -> None:
        # The call name arrives decorated by the surrounding expression; in
        # async code virtually every dispatcher call is awaited.
        node = _ts_call_node('await callSp("usp_invoice_list");')
        assert string_call_target(node, "await callSp", SPECS) == "usp_invoice_list"

    def test_resolves_through_a_receiver(self) -> None:
        node = _ts_call_node('db.callSp("usp_invoice_list");')
        assert string_call_target(node, "db.callSp", SPECS) == "usp_invoice_list"

    def test_schema_qualified_target_reduces_to_the_routine_name(self) -> None:
        # The definition side registers the routine under its own name.
        node = _ts_call_node('callSp("app.usp_invoice_list");')
        assert string_call_target(node, "callSp", SPECS) == "usp_invoice_list"

    def test_honours_the_declared_argument_index(self) -> None:
        node = _ts_call_node('callProc(conn, "usp_invoice_list");')
        specs = (StringCallSpec(callee="callProc", arg_index=1),)
        assert string_call_target(node, "callProc", specs) == "usp_invoice_list"

    def test_undeclared_dispatcher_is_ignored(self) -> None:
        node = _ts_call_node('someOtherCall("usp_invoice_list");')
        assert string_call_target(node, "someOtherCall", SPECS) is None

    def test_non_literal_argument_resolves_to_nothing(self) -> None:
        # A variable names its target at runtime; guessing invents edges.
        node = _ts_call_node("callSp(spName);")
        assert string_call_target(node, "callSp", SPECS) is None

    def test_interpolated_template_resolves_to_nothing(self) -> None:
        node = _ts_call_node("callSp(`usp_${entity}_list`);")
        assert string_call_target(node, "callSp", SPECS) is None

    def test_missing_argument_resolves_to_nothing(self) -> None:
        node = _ts_call_node("callSp();")
        assert string_call_target(node, "callSp", SPECS) is None

    def test_no_specs_resolves_to_nothing(self) -> None:
        node = _ts_call_node('callSp("usp_invoice_list");')
        assert string_call_target(node, "callSp", ()) is None
