# Structural support for the tree-sitter languages cgr had no LanguageSpec
# for (issue #1352): Kotlin, Swift, Solidity, Bash, Elixir, Haskell and Nix
# are extracted by the ast-grep tier from the shipped YAML pattern configs.
# Each test indexes a real source file end to end and asserts the
# Module/Function/Class nodes and DEFINES/IMPORTS edges land, with emphasis
# on the forms a naive pattern would MISS (modifiers, guards, zero-arg defs)
# and the forms it would WRONGLY claim (type signatures, plain attributes).
from __future__ import annotations

from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

FUNCTION = cs.NodeLabel.FUNCTION.value
CLASS = cs.NodeLabel.CLASS.value
MODULE = cs.NodeLabel.MODULE.value
EXTERNAL_MODULE = cs.NodeLabel.EXTERNAL_MODULE.value
DEFINES = cs.RelationshipType.DEFINES.value
IMPORTS = cs.RelationshipType.IMPORTS.value


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


def _node_name_counts(mock: MagicMock, label: str) -> Counter[str]:
    """Emission counts per name, to catch a node emitted more than once."""
    return Counter(
        c.args[1].get(cs.KEY_NAME)
        for c in mock.ensure_node_batch.call_args_list
        if str(c.args[0]) == label
    )


def _import_targets(mock: MagicMock) -> set[str]:
    return {
        to
        for _from, to in {
            (c.args[0][2], c.args[2][2])
            for c in mock.ensure_relationship_batch.call_args_list
            if str(c.args[1]) == IMPORTS
        }
    }


def _defined(mock: MagicMock) -> set[str]:
    return {
        c.args[2][2]
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == DEFINES
    }


KOTLIN = """package com.example.app
import kotlinx.coroutines.launch

class Greeter(val name: String) {
    fun greet(x: String): String = "hi"
    suspend fun load() {}
    private suspend fun deep() {}
    override fun run() {}
    companion object {
        fun build(): Greeter = Greeter("a")
    }
}
interface Speaker { fun speak() }
object Registry { fun get() = 1 }
data class Point(val x: Int)
enum class Color { RED }
fun topLevel(a: Int): Int = a
"""


def test_kotlin_functions_including_modifiers(tmp_path: Path) -> None:
    # the point of the kind rule: modifiers sit outside any fixed pattern,
    # so `suspend`/`private suspend`/`override` must still be captured.
    names = _node_names(_run(tmp_path, {"Greeter.kt": KOTLIN}), FUNCTION)
    assert {
        "greet",
        "load",
        "deep",
        "run",
        "build",
        "speak",
        "get",
        "topLevel",
    } <= names, names


def test_kotlin_classes_interfaces_objects_and_enums(tmp_path: Path) -> None:
    names = _node_names(_run(tmp_path, {"Greeter.kt": KOTLIN}), CLASS)
    assert {"Greeter", "Speaker", "Registry", "Point", "Color"} <= names, names


def test_kotlin_imports(tmp_path: Path) -> None:
    targets = _import_targets(_run(tmp_path, {"Greeter.kt": KOTLIN}))
    assert "kotlinx.coroutines.launch" in targets, targets


SWIFT = """import Foundation

class Greeter {
    func greet(name: String) -> String { return "hi" }
    static func build() -> Greeter { return Greeter() }
    init() {}
}
struct Point {
    var x: Int
    func mag() -> Int { return x }
}
protocol Speaker { func speak() }
enum Color { case red }
extension Greeter { func extra() {} }
func topLevel(a: Int) -> Int { return a }
"""


def test_swift_functions_and_initializers(tmp_path: Path) -> None:
    names = _node_names(_run(tmp_path, {"Greeter.swift": SWIFT}), FUNCTION)
    assert {"greet", "build", "init", "mag", "speak", "extra", "topLevel"} <= names, (
        names
    )


def test_swift_types(tmp_path: Path) -> None:
    # class/struct/enum/extension share one grammar kind; protocol is its own
    names = _node_names(_run(tmp_path, {"Greeter.swift": SWIFT}), CLASS)
    assert {"Greeter", "Point", "Color", "Speaker"} <= names, names


def test_swift_imports(tmp_path: Path) -> None:
    assert "Foundation" in _import_targets(_run(tmp_path, {"G.swift": SWIFT}))


SOLIDITY = """pragma solidity ^0.8.0;
import "./Base.sol";

contract Token is Base {
    function transfer(address to) public returns (bool) { return true; }
    function _helper() internal pure returns (uint) { return 1; }
    constructor() {}
    modifier onlyOwner() { _; }
}
interface IThing { function doIt() external; }
library Math { function add(uint a) internal pure returns (uint) { return a; } }
"""


def test_solidity_functions_modifiers_and_constructor(tmp_path: Path) -> None:
    names = _node_names(_run(tmp_path, {"Token.sol": SOLIDITY}), FUNCTION)
    assert {"transfer", "_helper", "doIt", "add", "onlyOwner"} <= names, names


def test_solidity_contract_interface_and_library(tmp_path: Path) -> None:
    names = _node_names(_run(tmp_path, {"Token.sol": SOLIDITY}), CLASS)
    assert {"Token", "IThing", "Math"} <= names, names


def test_solidity_import_path_is_unquoted(tmp_path: Path) -> None:
    # the imported path is a string literal; the tier strips the quotes
    assert "./Base.sol" in _import_targets(_run(tmp_path, {"T.sol": SOLIDITY}))


BASH = """#!/usr/bin/env bash
source ./lib.sh

greet() {
  echo "hi"
}
function build {
  echo "b"
}
function deploy() {
  echo "d"
}
"""


def test_bash_all_three_function_spellings(tmp_path: Path) -> None:
    names = _node_names(_run(tmp_path, {"deploy.sh": BASH}), FUNCTION)
    assert {"greet", "build", "deploy"} <= names, names


def test_bash_source_import(tmp_path: Path) -> None:
    assert "./lib.sh" in _import_targets(_run(tmp_path, {"deploy.sh": BASH}))


ELIXIR = """defmodule MyApp.Greeter do
  import Logger
  alias MyApp.Helper

  def greet(name), do: "hi"
  defp secret(), do: 1
  defmacro mac(x), do: x

  def zero_arg do
    1
  end

  def guarded(x) when is_integer(x) do
    x
  end
end

defprotocol Shape do
  def area(s)
end
"""


def test_elixir_defs_including_zero_arg_and_guarded(tmp_path: Path) -> None:
    # zero-arg and guarded defs match no parenthesised pattern; they come
    # from the do-block fallback whose capture the tier trims to the name.
    names = _node_names(_run(tmp_path, {"greeter.ex": ELIXIR}), FUNCTION)
    assert {"greet", "secret", "mac", "zero_arg", "guarded", "area"} <= names, names


def test_elixir_modules_and_protocols(tmp_path: Path) -> None:
    names = _node_names(_run(tmp_path, {"greeter.ex": ELIXIR}), CLASS)
    assert {"MyApp.Greeter", "Shape"} <= names, names


def test_elixir_import_alias_edges(tmp_path: Path) -> None:
    targets = _import_targets(_run(tmp_path, {"greeter.ex": ELIXIR}))
    assert {"Logger", "MyApp.Helper"} <= targets, targets


HASKELL = """module MyApp.Greeter where
import Data.List

data Shape = Circle Double | Square Double
newtype Wrapper = Wrapper Int
type Alias = String
class Speaker a where
  speak :: a -> String

greet :: String -> String
greet name = "hi"

main :: IO ()
main = putStrLn "x"
"""


def test_haskell_equations_and_nullary_binds(tmp_path: Path) -> None:
    names = _node_names(_run(tmp_path, {"Greeter.hs": HASKELL}), FUNCTION)
    assert {"greet", "main"} <= names, names


def test_haskell_type_signature_is_not_a_function(tmp_path: Path) -> None:
    # `speak :: a -> String` parses as a `function` node too; without the
    # has_child guard the type variable `a` would land as a Function.
    names = _node_names(_run(tmp_path, {"Greeter.hs": HASKELL}), FUNCTION)
    assert "a" not in names, names
    assert "String" not in names, names


def test_haskell_types_and_classes(tmp_path: Path) -> None:
    names = _node_names(_run(tmp_path, {"Greeter.hs": HASKELL}), CLASS)
    assert {"Shape", "Wrapper", "Alias", "Speaker"} <= names, names


def test_haskell_imports(tmp_path: Path) -> None:
    assert "Data.List" in _import_targets(_run(tmp_path, {"G.hs": HASKELL}))


NIX = """{ pkgs ? import <nixpkgs> {} }:
let
  helper = x: x + 1;
  greet = name: "hi ${name}";
in
{
  myPkg = pkgs.stdenv.mkDerivation { name = "p"; };
}
"""


def test_nix_lambda_bindings_are_functions(tmp_path: Path) -> None:
    names = _node_names(_run(tmp_path, {"default.nix": NIX}), FUNCTION)
    assert {"helper", "greet"} <= names, names


def test_nix_plain_attributes_are_not_functions(tmp_path: Path) -> None:
    # has_child: function_expression keeps non-lambda attributes out
    names = _node_names(_run(tmp_path, {"default.nix": NIX}), FUNCTION)
    assert "myPkg" not in names, names
    assert "name" not in names, names


def test_new_language_configs_load_and_map_extensions() -> None:
    from codebase_rag.parsers.ast_grep_tier import load_pattern_configs

    configs = load_pattern_configs()
    expected = {
        ".kt": "kotlin",
        ".kts": "kotlin",
        ".swift": "swift",
        ".sol": "solidity",
        ".sh": "bash",
        ".bash": "bash",
        ".ex": "elixir",
        ".exs": "elixir",
        ".hs": "haskell",
        ".nix": "nix",
    }
    for extension, ast_grep_id in expected.items():
        assert extension in configs, extension
        assert configs[extension].ast_grep_id == ast_grep_id, extension


def test_module_nodes_emitted_for_new_languages(tmp_path: Path) -> None:
    mock = _run(tmp_path, {"Greeter.kt": KOTLIN, "deploy.sh": BASH})
    names = _node_names(mock, MODULE)
    assert {"Greeter.kt", "deploy.sh"} <= names, names


def test_defines_edges_for_new_languages(tmp_path: Path) -> None:
    defined = _defined(_run(tmp_path, {"Greeter.kt": KOTLIN}))
    assert any(qn.endswith(".topLevel") for qn in defined), defined
    assert any(qn.endswith(".Greeter") for qn in defined), defined


def _load_from(tmp_path: Path, monkeypatch, yaml_text: str):
    from codebase_rag.parsers import ast_grep_tier

    (tmp_path / "lang.yaml").write_text(yaml_text, encoding="utf-8")
    monkeypatch.setattr(ast_grep_tier, "_PATTERNS_DIR", tmp_path)
    return ast_grep_tier.load_pattern_configs()


HEADER = 'ast_grep_id: ruby\nextensions: [".xx"]\n'


def test_plain_string_rule_still_parses_as_pattern(tmp_path: Path, monkeypatch) -> None:
    # back-compat: the original string-list format must keep working
    configs = _load_from(tmp_path, monkeypatch, HEADER + 'functions: ["def $NAME"]\n')
    rule = configs[".xx"].functions[0]
    assert rule.pattern == "def $NAME"
    assert rule.kind is None


def test_kind_rule_parses(tmp_path: Path, monkeypatch) -> None:
    configs = _load_from(
        tmp_path, monkeypatch, HEADER + "functions:\n  - kind: function_declaration\n"
    )
    rule = configs[".xx"].functions[0]
    assert rule.kind == "function_declaration"
    assert rule.pattern is None


def test_rule_with_both_pattern_and_kind_raises(tmp_path: Path, monkeypatch) -> None:
    try:
        _load_from(
            tmp_path,
            monkeypatch,
            HEADER + "functions:\n  - kind: fn\n    pattern: 'def $NAME'\n",
        )
    except ValueError as exc:
        assert "exactly one" in str(exc)
    else:
        raise AssertionError("expected ValueError for pattern+kind")


def test_rule_with_neither_pattern_nor_kind_raises(tmp_path: Path, monkeypatch) -> None:
    try:
        _load_from(tmp_path, monkeypatch, HEADER + "functions:\n  - name_child: x\n")
    except ValueError as exc:
        assert "exactly one" in str(exc)
    else:
        raise AssertionError("expected ValueError for empty rule")


def test_has_child_on_pattern_rule_raises(tmp_path: Path, monkeypatch) -> None:
    try:
        _load_from(
            tmp_path,
            monkeypatch,
            HEADER + "functions:\n  - pattern: 'def $NAME'\n    has_child: match\n",
        )
    except ValueError as exc:
        assert "has_child" in str(exc)
    else:
        raise AssertionError("expected ValueError for has_child on a pattern rule")


def test_name_head_on_kind_rule_raises(tmp_path: Path, monkeypatch) -> None:
    try:
        _load_from(
            tmp_path,
            monkeypatch,
            HEADER + "functions:\n  - kind: fn\n    name_head: true\n",
        )
    except ValueError as exc:
        assert "name_head" in str(exc)
    else:
        raise AssertionError("expected ValueError for name_head on a kind rule")


def test_leading_identifier_trims_signatures() -> None:
    from codebase_rag.parsers.ast_grep_tier import _leading_identifier

    assert _leading_identifier("guarded(x) when is_integer(x)") == "guarded"
    assert _leading_identifier("zero_arg") == "zero_arg"
    assert _leading_identifier("valid?") == "valid?"
    assert _leading_identifier("save!") == "save!"
    assert _leading_identifier("(oops)") is None


def test_non_list_rule_section_raises(tmp_path: Path, monkeypatch) -> None:
    try:
        _load_from(tmp_path, monkeypatch, HEADER + "functions: 'def $NAME'\n")
    except ValueError as exc:
        assert "must be a list" in str(exc)
    else:
        raise AssertionError("expected ValueError for a non-list rule section")


def test_rule_of_unsupported_type_raises(tmp_path: Path, monkeypatch) -> None:
    try:
        _load_from(tmp_path, monkeypatch, HEADER + "functions:\n  - 42\n")
    except ValueError as exc:
        assert "string or a mapping" in str(exc)
    else:
        raise AssertionError("expected ValueError for a non-string, non-mapping rule")


def test_two_same_line_declarations_both_land(tmp_path: Path) -> None:
    # definitions are deduped by (line, column); keying on line alone dropped
    # the second declaration whenever two shared a source line.
    mock = _run(tmp_path, {"S.kt": "fun first() {}; fun second() {}\n"})
    names = _node_names(mock, FUNCTION)
    assert {"first", "second"} <= names, names


def test_same_line_declarations_both_get_defines_edges(tmp_path: Path) -> None:
    defined = _defined(_run(tmp_path, {"S.kt": "fun first() {}; fun second() {}\n"}))
    assert any(qn.endswith(".first") for qn in defined), defined
    assert any(qn.endswith(".second") for qn in defined), defined


def test_name_child_on_pattern_rule_raises(tmp_path: Path, monkeypatch) -> None:
    # name_child is ignored by the pattern branch, so a config setting both
    # would silently do nothing; reject it instead.
    try:
        _load_from(
            tmp_path,
            monkeypatch,
            HEADER + "functions:\n  - pattern: 'def $NAME'\n    name_child: variable\n",
        )
    except ValueError as exc:
        assert "name_child" in str(exc)
    else:
        raise AssertionError("expected ValueError for name_child on a pattern rule")


KOTLIN_OBJECTS = """object Plain { fun a() = 1 }
private object Hidden { fun b() = 2 }
object Delegating : Service { fun c() = 3 }
"""


def test_kotlin_object_modifier_and_delegation_forms(tmp_path: Path) -> None:
    # a plain `object X { }` is an object_literal (pattern-only), while
    # modifier and delegation forms are object_declaration (kind-only), so
    # the config needs both rules to cover all three.
    # Counts, not a set: the object_declaration kind rule and the
    # object_literal pattern cover disjoint spellings today, so each name
    # lands once. Asserting counts pins that -- if either rule widens to
    # overlap the other, a set comparison would silently hide the resulting
    # duplicate emission.
    counts = _node_name_counts(_run(tmp_path, {"O.kt": KOTLIN_OBJECTS}), CLASS)
    assert counts == Counter({"Plain": 1, "Hidden": 1, "Delegating": 1}), counts


def test_kotlin_object_members_still_extracted(tmp_path: Path) -> None:
    counts = _node_name_counts(_run(tmp_path, {"O.kt": KOTLIN_OBJECTS}), FUNCTION)
    assert counts == Counter({"a": 1, "b": 1, "c": 1}), counts
