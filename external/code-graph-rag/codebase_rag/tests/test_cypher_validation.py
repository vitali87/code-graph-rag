import re

import pytest

from codebase_rag import constants as cs
from codebase_rag import exceptions as ex
from codebase_rag.services.llm import (
    _build_keyword_pattern,
    _validate_cypher_read_only,
)


class TestBuildKeywordPattern:
    def test_single_word_uses_word_boundaries(self) -> None:
        pattern = _build_keyword_pattern("DELETE")
        assert pattern.search("DELETE n") is not None
        assert pattern.search("XDELETE") is None
        assert pattern.search("DELETEX") is None

    def test_multi_word_allows_whitespace_between_parts(self) -> None:
        pattern = _build_keyword_pattern("LOAD CSV")
        assert pattern.search("LOAD CSV") is not None
        assert pattern.search("LOAD  CSV") is not None
        assert pattern.search("LOAD\nCSV") is not None
        assert pattern.search("LOAD\t CSV") is not None

    def test_multi_word_allows_block_comment_between_parts(self) -> None:
        pattern = _build_keyword_pattern("LOAD CSV")
        assert pattern.search("LOAD/*bypass*/CSV") is not None
        assert pattern.search("LOAD /* comment */ CSV") is not None

    def test_multi_word_allows_single_line_comment_between_parts(self) -> None:
        pattern = _build_keyword_pattern("LOAD CSV")
        assert pattern.search("LOAD //comment\nCSV") is not None
        assert pattern.search("LOAD //\nCSV") is not None

    def test_multi_word_respects_word_boundaries(self) -> None:
        pattern = _build_keyword_pattern("LOAD CSV")
        assert pattern.search("PRELOAD CSV") is None
        assert pattern.search("LOAD CSVX") is None

    def test_single_word_is_case_sensitive_on_input(self) -> None:
        pattern = _build_keyword_pattern("DELETE")
        assert pattern.search("DELETE") is not None
        assert pattern.search("delete") is None

    def test_returns_compiled_pattern(self) -> None:
        pattern = _build_keyword_pattern("SET")
        assert isinstance(pattern, re.Pattern)

    def test_multi_word_has_dotall_flag(self) -> None:
        pattern = _build_keyword_pattern("CREATE INDEX")
        assert pattern.flags & re.DOTALL

    def test_all_dangerous_keywords_produce_valid_patterns(self) -> None:
        for kw in cs.CYPHER_DANGEROUS_KEYWORDS:
            pattern = _build_keyword_pattern(kw)
            assert pattern.search(kw) is not None


class TestValidateCypherReadOnly:
    def test_safe_match_query_passes(self) -> None:
        _validate_cypher_read_only("MATCH (n) RETURN n;")

    def test_safe_match_with_where_passes(self) -> None:
        _validate_cypher_read_only("MATCH (n:Function) WHERE n.name = 'foo' RETURN n;")

    def test_safe_optional_match_passes(self) -> None:
        _validate_cypher_read_only(
            "MATCH (a)-[:CALLS]->(b) OPTIONAL MATCH (b)-[:DEFINES]->(c) RETURN a, b, c;"
        )

    @pytest.mark.parametrize(
        "keyword",
        sorted(cs.CYPHER_DANGEROUS_KEYWORDS),
    )
    def test_rejects_all_dangerous_keywords(self, keyword: str) -> None:
        query = f"MATCH (n) {keyword} n;"
        with pytest.raises(ex.LLMGenerationError):
            _validate_cypher_read_only(query)

    def test_rejects_delete(self) -> None:
        with pytest.raises(ex.LLMGenerationError, match="DELETE"):
            _validate_cypher_read_only("MATCH (n) DELETE n;")

    def test_rejects_detach_delete(self) -> None:
        with pytest.raises(ex.LLMGenerationError):
            _validate_cypher_read_only("MATCH (n) DETACH DELETE n;")

    def test_rejects_drop(self) -> None:
        with pytest.raises(ex.LLMGenerationError, match="DROP"):
            _validate_cypher_read_only("MATCH (n) DROP INDEX idx;")

    def test_rejects_set(self) -> None:
        with pytest.raises(ex.LLMGenerationError, match="SET"):
            _validate_cypher_read_only("MATCH (n) SET n.name = 'x';")

    def test_rejects_merge(self) -> None:
        with pytest.raises(ex.LLMGenerationError, match="MERGE"):
            _validate_cypher_read_only("MERGE (n:Node {id: 1});")

    def test_rejects_create(self) -> None:
        with pytest.raises(ex.LLMGenerationError, match="CREATE"):
            _validate_cypher_read_only("CREATE (n:Node {name: 'test'});")

    def test_rejects_load_csv(self) -> None:
        with pytest.raises(ex.LLMGenerationError, match="LOAD CSV"):
            _validate_cypher_read_only(
                "LOAD CSV FROM 'http://evil.com/data.csv' AS row;"
            )

    def test_rejects_create_index(self) -> None:
        with pytest.raises(ex.LLMGenerationError, match="CREATE INDEX"):
            _validate_cypher_read_only("CREATE INDEX ON :Node(name);")

    def test_case_insensitive(self) -> None:
        with pytest.raises(ex.LLMGenerationError):
            _validate_cypher_read_only("match (n) delete n;")

    def test_rejects_block_comment_bypass(self) -> None:
        with pytest.raises(ex.LLMGenerationError):
            _validate_cypher_read_only("LOAD/*bypass*/CSV FROM 'http://evil.com';")

    def test_rejects_single_line_comment_bypass(self) -> None:
        with pytest.raises(ex.LLMGenerationError):
            _validate_cypher_read_only("LOAD //bypass\nCSV FROM 'http://evil.com';")

    def test_does_not_flag_substring_matches(self) -> None:
        _validate_cypher_read_only("MATCH (n) WHERE n.name = 'DATASET' RETURN n;")

    def test_does_not_flag_reset(self) -> None:
        _validate_cypher_read_only("MATCH (n) WHERE n.name = 'RESET' RETURN n;")

    def test_does_not_flag_created_at(self) -> None:
        _validate_cypher_read_only("MATCH (n) WHERE n.created_at > 0 RETURN n;")

    def test_error_includes_keyword_and_query(self) -> None:
        query = "MATCH (n) DELETE n;"
        with pytest.raises(ex.LLMGenerationError, match="DELETE") as exc_info:
            _validate_cypher_read_only(query)
        assert query in str(exc_info.value)

    def test_rejects_foreach(self) -> None:
        with pytest.raises(ex.LLMGenerationError, match="FOREACH"):
            _validate_cypher_read_only(
                "MATCH p=(a)-[*]->(b) FOREACH (n IN nodes(p) | SET n.marked = true);"
            )

    def test_rejects_remove(self) -> None:
        with pytest.raises(ex.LLMGenerationError, match="REMOVE"):
            _validate_cypher_read_only("MATCH (n) REMOVE n.prop;")

    def test_rejects_call(self) -> None:
        with pytest.raises(ex.LLMGenerationError, match="CALL"):
            _validate_cypher_read_only("CALL db.schema.visualization();")

    def test_rejects_create_constraint(self) -> None:
        with pytest.raises(ex.LLMGenerationError, match="CREATE CONSTRAINT"):
            _validate_cypher_read_only(
                "CREATE CONSTRAINT ON (n:Node) ASSERT n.id IS UNIQUE;"
            )

    def test_rejects_multiline_block_comment_bypass(self) -> None:
        with pytest.raises(ex.LLMGenerationError):
            _validate_cypher_read_only("LOAD/*\nbypass\n*/CSV FROM 'http://evil.com';")
