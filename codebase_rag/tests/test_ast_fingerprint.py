"""Unit tests for the structural clone-detection fingerprints.

Trees are parsed in-test; languages other than Python skip when their grammar
wheel is absent, mirroring the updater fixtures.
"""

from __future__ import annotations

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.language_spec import LANGUAGE_SPECS
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.ast_fingerprint import (
    compute_ast_fingerprint,
    fingerprint_props,
)

PARSERS, _ = load_parsers()


def _function_node(language: cs.SupportedLanguage, code: str) -> Node:
    if language not in PARSERS:
        pytest.skip(f"{language} parser not available")
    root = PARSERS[language].parse(code.encode()).root_node
    function_types = set(LANGUAGE_SPECS[language].function_node_types)
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in function_types:
            return node
        stack.extend(reversed(node.named_children))
    raise AssertionError(f"no function node in: {code}")


def _py(code: str) -> Node:
    return _function_node(cs.SupportedLanguage.PYTHON, code)


TOTAL_PRICE = """
def total_price(items):
    result = 0
    for item in items:
        result += item.price
    return result
"""

SUM_WEIGHTS = """
def sum_weights(boxes):
    acc = 0
    for box in boxes:
        acc += box.weight
    return acc
"""


class TestSkeletonEquivalence:
    def test_exact_copy_matches(self) -> None:
        first = compute_ast_fingerprint(_py(TOTAL_PRICE))
        second = compute_ast_fingerprint(_py(TOTAL_PRICE))
        assert first is not None
        assert second is not None
        assert first.fingerprint == second.fingerprint

    def test_renamed_identifiers_match(self) -> None:
        first = compute_ast_fingerprint(_py(TOTAL_PRICE))
        second = compute_ast_fingerprint(_py(SUM_WEIGHTS))
        assert first is not None
        assert second is not None
        assert first.fingerprint == second.fingerprint
        assert first.node_count == second.node_count

    def test_changed_literal_matches(self) -> None:
        base = compute_ast_fingerprint(_py("def f(x):\n    y = 1\n    return y\n"))
        other = compute_ast_fingerprint(
            _py('def f(x):\n    y = "hello"\n    return y\n')
        )
        assert base is not None
        assert other is not None
        assert base.fingerprint == other.fingerprint

    def test_added_comment_matches(self) -> None:
        base = compute_ast_fingerprint(_py("def f(x):\n    y = 1\n    return y\n"))
        other = compute_ast_fingerprint(
            _py("def f(x):\n    # a note\n    y = 1\n    return y\n")
        )
        assert base is not None
        assert other is not None
        assert base.fingerprint == other.fingerprint


class TestSkeletonDistinctions:
    def test_changed_operator_differs(self) -> None:
        plus = compute_ast_fingerprint(_py("def f(a, b):\n    return a + b\n"))
        minus = compute_ast_fingerprint(_py("def f(a, b):\n    return a - b\n"))
        assert plus is not None
        assert minus is not None
        assert plus.fingerprint != minus.fingerprint

    def test_added_statement_differs(self) -> None:
        base = compute_ast_fingerprint(_py(TOTAL_PRICE))
        edited = compute_ast_fingerprint(
            _py(
                TOTAL_PRICE.replace(
                    "    return result", "    result *= 2\n    return result"
                )
            )
        )
        assert base is not None
        assert edited is not None
        assert base.fingerprint != edited.fingerprint

    def test_bodiless_node_yields_none(self) -> None:
        module = PARSERS[cs.SupportedLanguage.PYTHON].parse(b"x = 1").root_node
        identifier = module.named_children[0].named_children[0]
        assert compute_ast_fingerprint(identifier) is None


class TestBranchFingerprints:
    def test_substantial_statements_become_branches(self) -> None:
        result = compute_ast_fingerprint(_py(TOTAL_PRICE))
        assert result is not None
        assert result.branch_fingerprints
        assert result.branch_fingerprints == sorted(result.branch_fingerprints)
        assert len(set(result.branch_fingerprints)) == len(result.branch_fingerprints)

    def test_trivial_body_has_no_branches(self) -> None:
        result = compute_ast_fingerprint(_py("def f():\n    return\n"))
        assert result is not None
        assert result.branch_fingerprints == []

    def test_edited_copy_shares_most_branches(self) -> None:
        base = compute_ast_fingerprint(_py(TOTAL_PRICE))
        edited = compute_ast_fingerprint(
            _py(SUM_WEIGHTS.replace("    return acc", "    acc *= 2\n    return acc"))
        )
        assert base is not None
        assert edited is not None
        shared = set(base.branch_fingerprints) & set(edited.branch_fingerprints)
        assert shared
        assert edited.fingerprint != base.fingerprint


class TestFingerprintProps:
    def test_props_carry_all_three_keys(self) -> None:
        props = fingerprint_props(_py(TOTAL_PRICE))
        assert set(props) == {
            cs.KEY_AST_FINGERPRINT,
            cs.KEY_AST_FINGERPRINT_NODES,
            cs.KEY_AST_BRANCH_FINGERPRINTS,
        }
        assert isinstance(props[cs.KEY_AST_FINGERPRINT], str)
        assert len(props[cs.KEY_AST_FINGERPRINT]) == cs.AST_FP_DIGEST_SIZE * 2

    def test_bodiless_node_yields_empty_props(self) -> None:
        module = PARSERS[cs.SupportedLanguage.PYTHON].parse(b"x = 1").root_node
        identifier = module.named_children[0].named_children[0]
        assert fingerprint_props(identifier) == {}


MACRO_BACKTRACE = """
macro_rules! backtrace {
    () => {
        Some(crate::backtrace::Backtrace::capture())
    };
    ($err:expr) => {
        $err.backtrace()
    };
}
"""

MACRO_BACKTRACE_RENAMED = """
macro_rules! trace_or {
    () => {
        Some(crate::tracing::Snapshot::grab())
    };
    ($e:expr) => {
        $e.snapshot()
    };
}
"""

MACRO_SINGLE_ARM = """
macro_rules! backtrace {
    () => {
        Some(crate::backtrace::Backtrace::capture())
    };
}
"""


class TestMacroRulesFingerprints:
    def _macro(self, code: str) -> Node:
        """Parse Rust source and return its macro_definition node."""
        return _function_node(cs.SupportedLanguage.RUST, code)

    def test_macro_rules_definition_gets_a_fingerprint(self) -> None:
        """A macro_rules! definition fingerprints from its token trees."""
        result = compute_ast_fingerprint(self._macro(MACRO_BACKTRACE))
        assert result is not None
        assert result.node_count > 1

    def test_renamed_macro_with_identical_rules_matches(self) -> None:
        """Renaming the macro and its identifiers keeps the fingerprint."""
        first = compute_ast_fingerprint(self._macro(MACRO_BACKTRACE))
        second = compute_ast_fingerprint(self._macro(MACRO_BACKTRACE_RENAMED))
        assert first is not None
        assert second is not None
        assert first.fingerprint == second.fingerprint

    def test_macro_with_different_rules_differs(self) -> None:
        """Dropping an arm changes the fingerprint."""
        both_arms = compute_ast_fingerprint(self._macro(MACRO_BACKTRACE))
        one_arm = compute_ast_fingerprint(self._macro(MACRO_SINGLE_ARM))
        assert both_arms is not None
        assert one_arm is not None
        assert both_arms.fingerprint != one_arm.fingerprint

    def test_trait_method_signature_stays_unfingerprinted(self) -> None:
        """Bodiless trait signatures stay out of clone detection."""
        signature = _function_node(
            cs.SupportedLanguage.RUST,
            "trait T {\n    fn snapshot(&self) -> i32;\n}\n",
        )
        assert signature.type == "function_signature_item"
        assert compute_ast_fingerprint(signature) is None


CROSS_LANGUAGE_PAIRS: dict[cs.SupportedLanguage, tuple[str, str]] = {
    cs.SupportedLanguage.JS: (
        "function total(items) {\n  let r = 0;\n  for (const i of items) {\n    r += i.price;\n  }\n  return r;\n}\n",
        "function sumW(boxes) {\n  let a = 0;\n  for (const b of boxes) {\n    a += b.weight;\n  }\n  return a;\n}\n",
    ),
    cs.SupportedLanguage.JAVA: (
        "class A {\n  int total(int[] xs) {\n    int r = 0;\n    for (int x : xs) {\n      r += x;\n    }\n    return r;\n  }\n}\n",
        "class B {\n  int sumW(int[] ys) {\n    int a = 0;\n    for (int y : ys) {\n      a += y;\n    }\n    return a;\n  }\n}\n",
    ),
    cs.SupportedLanguage.GO: (
        "package p\n\nfunc Total(xs []int) int {\n\tr := 0\n\tfor _, x := range xs {\n\t\tr += x\n\t}\n\treturn r\n}\n",
        "package q\n\nfunc SumW(ys []int) int {\n\ta := 0\n\tfor _, y := range ys {\n\t\ta += y\n\t}\n\treturn a\n}\n",
    ),
    cs.SupportedLanguage.RUST: (
        "fn total(xs: &[i32]) -> i32 {\n    let mut r = 0;\n    for x in xs {\n        r += x;\n    }\n    r\n}\n",
        "fn sum_w(ys: &[i32]) -> i32 {\n    let mut a = 0;\n    for y in ys {\n        a += y;\n    }\n    a\n}\n",
    ),
}


class TestCrossLanguageSmoke:
    @pytest.mark.parametrize("language", sorted(CROSS_LANGUAGE_PAIRS))
    def test_renamed_copy_matches_within_language(
        self, language: cs.SupportedLanguage
    ) -> None:
        original, renamed = CROSS_LANGUAGE_PAIRS[language]
        first = compute_ast_fingerprint(_function_node(language, original))
        second = compute_ast_fingerprint(_function_node(language, renamed))
        assert first is not None
        assert second is not None
        assert first.fingerprint == second.fingerprint
