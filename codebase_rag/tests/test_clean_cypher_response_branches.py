"""Whole-output pins for `_clean_cypher_response`'s branches.

`test_llm_service_unit.py::TestCleanCypherResponse` already drives this
function, but almost every assertion there checks ONE property of the result
(`endswith(";")`, `"```" not in result`). An assertion like that is satisfied
by the working code and by a broken version that mangles the query and still
terminates it, so those tests pin termination rather than cleaning.

This file asserts the WHOLE returned string for each branch, so a refactor that
drops a step, reorders two of them, or returns a differently-mangled query
fails here. Written against the pre-#1669 implementation and unchanged by the
refactor, so it characterises shipped behaviour rather than describing the new
structure.

Several pinned outputs are quirks rather than designed behaviour: an
unterminated fence keeps its backticks, and empty input becomes a bare
semicolon. They are pinned as-is deliberately -- the refactor must not change
behaviour, and a later decision to change any of them should have to edit a
test that says so.
"""

from __future__ import annotations

import pytest

from codebase_rag.services.llm import _clean_cypher_response

MATCH = "MATCH (n) RETURN n"
MATCH_SEMI = "MATCH (n) RETURN n;"


class TestFencedBlock:
    def test_a_bare_fence_yields_the_inner_query(self) -> None:
        assert _clean_cypher_response("```MATCH (n) RETURN n```") == MATCH_SEMI

    def test_a_cypher_tagged_fence_drops_the_tag(self) -> None:
        assert (
            _clean_cypher_response("```cypher\nMATCH (n) RETURN n\n```") == MATCH_SEMI
        )

    def test_the_fence_tag_match_is_case_insensitive(self) -> None:
        assert (
            _clean_cypher_response("```CYPHER\nMATCH (n) RETURN n\n```") == MATCH_SEMI
        )

    def test_prose_before_the_fence_is_discarded(self) -> None:
        assert (
            _clean_cypher_response("Here you go:\n```cypher\nMATCH (n) RETURN n\n```")
            == MATCH_SEMI
        )

    def test_an_unterminated_fence_is_left_alone(self) -> None:
        # Only two parts, so the fence branch declines it -- and because the
        # string DID contain "```" the markdown branch never runs either, so
        # the backticks survive. Pinned as the shipped behaviour.
        assert (
            _clean_cypher_response("```MATCH (n) RETURN n") == "```MATCH (n) RETURN n;"
        )

    def test_an_empty_fence_yields_only_the_terminator(self) -> None:
        assert _clean_cypher_response("``````") == ";"


class TestMarkdownStripping:
    def test_a_bold_label_and_its_colon_are_removed(self) -> None:
        assert _clean_cypher_response("**Query:** MATCH (n) RETURN n") == MATCH_SEMI

    def test_a_bold_span_without_a_colon_is_removed(self) -> None:
        assert _clean_cypher_response("**Note** MATCH (n) RETURN n") == MATCH_SEMI

    def test_a_colon_outside_the_bold_span_is_eaten(self) -> None:
        # The shape the colon-eating branch exists for. In "**Query:**" the
        # colon is INSIDE the span and disappears with it either way, so that
        # spelling cannot tell whether the branch works; only a colon after
        # the closing "**" can.
        assert _clean_cypher_response("**Query**: MATCH (n) RETURN n") == MATCH_SEMI

    def test_only_one_colon_is_eaten(self) -> None:
        # Exactly one, not "every leading colon" -- pins the += 1.
        assert _clean_cypher_response("**Q**::Q") == ":Q;"

    def test_a_colon_is_eaten_for_each_bold_span(self) -> None:
        assert _clean_cypher_response("**A**: **B**: MATCH (n) RETURN n") == MATCH_SEMI

    def test_two_bold_labels_are_both_removed(self) -> None:
        assert _clean_cypher_response("**A:** **B:** MATCH (n) RETURN n") == MATCH_SEMI

    def test_an_unterminated_bold_span_stops_the_loop(self) -> None:
        # No closing "**", so the loop breaks rather than spinning forever.
        assert (
            _clean_cypher_response("**Query MATCH (n) RETURN n")
            == "**Query MATCH (n) RETURN n;"
        )

    def test_inline_backticks_are_stripped(self) -> None:
        assert _clean_cypher_response("`MATCH (n) RETURN n`") == MATCH_SEMI

    def test_a_leading_cypher_word_is_removed(self) -> None:
        assert _clean_cypher_response("cypher MATCH (n) RETURN n") == MATCH_SEMI

    def test_the_leading_cypher_word_match_is_case_insensitive(self) -> None:
        assert _clean_cypher_response("CYPHER MATCH (n) RETURN n") == MATCH_SEMI

    def test_bold_is_stripped_before_the_cypher_prefix(self) -> None:
        # Pins the ORDER: the prefix check runs on the post-bold text, so it
        # sees "cypher MATCH..." only because the bold span went first.
        assert _clean_cypher_response("**Q:** cypher MATCH (n) RETURN n") == MATCH_SEMI


class TestTermination:
    def test_a_missing_semicolon_is_added(self) -> None:
        assert _clean_cypher_response(MATCH) == MATCH_SEMI

    def test_an_existing_semicolon_is_not_doubled(self) -> None:
        assert _clean_cypher_response(MATCH_SEMI) == MATCH_SEMI

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert _clean_cypher_response("   MATCH (n) RETURN n   ") == MATCH_SEMI

    @pytest.mark.parametrize("text", ["", "   "])
    def test_empty_input_yields_only_the_terminator(self, text: str) -> None:
        assert _clean_cypher_response(text) == ";"

    def test_a_multiline_query_keeps_its_newlines(self) -> None:
        query = "MATCH (n)\nWHERE n.type = 'class'\nRETURN n.name"
        assert _clean_cypher_response(query) == query + ";"
