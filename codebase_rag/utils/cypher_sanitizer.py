import re

_CYPHER_LINE_COMMENT = re.compile(r"//[^\n]*")
_CYPHER_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_CYPHER_LABEL_UNION = re.compile(
    r":\s*\(\s*\w+\s*\|\s*\w+.*?\)|:\s*\w+\s*\|\s*\w+",
    re.IGNORECASE,
)
_CYPHER_EXISTS_PROP = re.compile(
    r"EXISTS\s*\(\s*([A-Za-z_][\w]*\.[A-Za-z_][\w]*)\s*\)", re.IGNORECASE
)


class CypherSanitizer:
    """Utility class to sanitize and validate Cypher queries for Memgraph."""

    @staticmethod
    def first_statement(query: str) -> str:
        parts = [part.strip() for part in query.split(";") if part.strip()]
        if not parts:
            return query.strip()
        return parts[0] + ";"

    @staticmethod
    def strip_comments(query: str) -> str:
        no_block = _CYPHER_BLOCK_COMMENT.sub("", query)
        return _CYPHER_LINE_COMMENT.sub("", no_block)

    @staticmethod
    def contains_label_union(query: str) -> bool:
        return _CYPHER_LABEL_UNION.search(query) is not None

    @staticmethod
    def contains_property_exists(query: str) -> bool:
        return _CYPHER_EXISTS_PROP.search(query) is not None

    @staticmethod
    def replace_property_exists(query: str) -> str:
        return _CYPHER_EXISTS_PROP.sub(lambda m: f"{m.group(1)} IS NOT NULL", query)

    @staticmethod
    def append_memgraph_constraints(nl_query: str) -> str:
        return (
            f"{nl_query}\n\n"
            "IMPORTANT: Output valid Memgraph Cypher. "
            "Do NOT use line or block comments (// or /* */). "
            "Do NOT use label unions like :A|B or :(A|B). "
            "Do NOT use EXISTS(property). Use `property IS NOT NULL` instead. "
            "Return a SINGLE Cypher statement only (no multiple MATCH/RETURN blocks). "
            "If you need an OR across labels, use WHERE (n:A OR n:B). "
            "Use only MATCH/WHERE/RETURN/LIMIT and keep syntax simple."
        )
