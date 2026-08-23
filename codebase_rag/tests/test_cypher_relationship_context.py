from codebase_rag.constants import CYPHER_DEFAULT_LIMIT
from codebase_rag.cypher_queries import CYPHER_EXAMPLE_FUNCTION_CALLERS
from codebase_rag.prompts import (
    build_cypher_system_prompt,
    build_local_cypher_system_prompt,
)


class TestCallerExampleQuery:
    def test_matches_calls_edge_explicitly(self) -> None:
        assert "-[r:CALLS]->" in CYPHER_EXAMPLE_FUNCTION_CALLERS

    def test_returns_relationship_type(self) -> None:
        assert "type(r) AS relationship" in CYPHER_EXAMPLE_FUNCTION_CALLERS

    def test_caller_end_is_unlabeled(self) -> None:
        # Module-level code also holds call sites; labeling the caller end
        # would silently drop those rows.
        assert "MATCH (caller)-" in CYPHER_EXAMPLE_FUNCTION_CALLERS

    def test_callee_uses_multi_label_match(self) -> None:
        assert "callee:Function|Method" in CYPHER_EXAMPLE_FUNCTION_CALLERS

    def test_result_rows_are_bounded(self) -> None:
        assert f"LIMIT {CYPHER_DEFAULT_LIMIT}" in CYPHER_EXAMPLE_FUNCTION_CALLERS

    def test_callee_filter_uses_qualified_name_suffix(self) -> None:
        # Bare `name` equality both breaks the prompt's own ENDS-WITH rule and
        # conflates every same-named symbol (Greptile review on PR #1386).
        assert (
            "callee.qualified_name ENDS WITH '.process_payment'"
            in CYPHER_EXAMPLE_FUNCTION_CALLERS
        )
        assert "callee.name" not in CYPHER_EXAMPLE_FUNCTION_CALLERS

    def test_returns_callee_identity(self) -> None:
        # With several same-named callables, each caller row must say WHOSE
        # caller it is (Greptile review on PR #1386).
        assert (
            "callee.qualified_name AS callee_qualified_name"
            in CYPHER_EXAMPLE_FUNCTION_CALLERS
        )


class TestCypherPromptRelationshipGuidance:
    def test_main_prompt_contains_caller_pattern_example(self) -> None:
        assert CYPHER_EXAMPLE_FUNCTION_CALLERS in build_cypher_system_prompt()

    def test_main_prompt_requires_relationship_in_return(self) -> None:
        prompt = build_cypher_system_prompt()
        assert "Match the asked-about relationship explicitly and RETURN it" in prompt
        assert "type(r) AS relationship" in prompt

    def test_main_prompt_prefers_multi_label_matches(self) -> None:
        assert "Prefer multi-label matches" in build_cypher_system_prompt()

    def test_local_prompt_contains_caller_pattern_example(self) -> None:
        assert CYPHER_EXAMPLE_FUNCTION_CALLERS in build_local_cypher_system_prompt()

    def test_local_prompt_requires_relationship_in_return(self) -> None:
        assert "RETURN THE RELATIONSHIP TYPE" in build_local_cypher_system_prompt()

    def test_local_prompt_short_name_rule_allows_qualified_suffix(self) -> None:
        # The local prompt must not contradict the caller example: a flat
        # "never qualified_name for short names" rule invites name-equality
        # caller queries that conflate same-named symbols (CodeRabbit review
        # on PR #1386).
        prompt = build_local_cypher_system_prompt()
        assert "ENDS WITH '.VatManager'" in prompt
        assert "NOT `qualified_name`" not in prompt
