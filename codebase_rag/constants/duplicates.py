# Structural clone detection: skeleton-token classification and analysis
# defaults. Any edit here changes the fingerprints produced for unchanged
# sources, and trips the stale-graph warning through the parser fingerprint
# (constants/ is one of PARSER_FINGERPRINT_SOURCE_DIRS), which is exactly the
# invalidation the feature relies on.

# Placeholder tokens for the blanked node classes. Control-character prefixes
# cannot collide with a tree-sitter node type or an unnamed token's text.
AST_FP_ID_TOKEN = "\x01ID"
AST_FP_LIT_TOKEN = "\x02LIT"

# A node whose type contains this substring never contributes to a
# fingerprint: comments are free-form and renaming-adjacent by nature.
AST_FP_COMMENT_SUBSTRING = "comment"

# Identifier-like leaves collapse to AST_FP_ID_TOKEN. The substring covers the
# tree-sitter convention (`identifier`, `field_identifier`, ...); the extras
# are grammars that deviate from it.
AST_FP_ID_TYPE_SUBSTRING = "identifier"
AST_FP_ID_TYPE_EXTRAS = frozenset(
    {"name", "word", "variable_name", "simple_identifier", "label"}
)

# Literal-like nodes collapse to AST_FP_LIT_TOKEN and are NOT descended into,
# so string internals (quotes, fragments, interpolation) never leak structure.
AST_FP_LIT_TYPE_SUBSTRINGS = ("literal", "string", "number")
AST_FP_LIT_TYPE_EXTRAS = frozenset(
    {
        "integer",
        "float",
        "true",
        "false",
        "null",
        "nil",
        "none",
        "char",
        "escape_sequence",
        "heredoc_body",
    }
)

# Unnamed tokens in this set are pure punctuation the tree structure already
# encodes; keeping them would only add noise. Unnamed tokens NOT listed here
# (operators, keywords) stay in the skeleton so `a + b` and `a - b` differ.
AST_FP_PUNCT_TYPES = frozenset(
    {"(", ")", "{", "}", "[", "]", ",", ";", ":", ".", "->", "=>"}
)

# A subtree is a "branch" (statement-level unit for near-duplicate overlap)
# when its parent is a block-ish node. The substrings cover `block`,
# `statement_block`, `indented_block`, `function_body`, `template_body`, ...;
# the extras are the block containers named neither way.
AST_FP_BLOCK_PARENT_SUBSTRINGS = ("block", "body")
AST_FP_BLOCK_PARENT_EXTRAS = frozenset({"statement_list", "compound_statement"})

# Branches below this skeleton-node count (`return x`, `break`) are shared by
# nearly every function and would dominate the candidate index.
AST_FP_MIN_BRANCH_NODES = 5

# Byte separators framing a node's token and child digests in the Merkle hash.
AST_FP_HASH_OPEN = b"("
AST_FP_HASH_CLOSE = b")"
AST_FP_DIGEST_SIZE = 8

# Analysis defaults (CLI/tool overridable).
DUPLICATES_DEFAULT_THRESHOLD = 0.8
DUPLICATES_DEFAULT_MIN_NODES = 15
# A branch fingerprint shared by more functions than this is not
# discriminative (a common guard clause); its posting list generates no
# candidate pairs.
DUPLICATES_HOT_FINGERPRINT_CAP = 50
# Hard budget on materialized similar groups: a pathological threshold graph
# (Moon-Moser shape) has exponentially many maximal cliques, so enumeration
# stops here and the scan reports truncation instead of hanging.
DUPLICATES_MAX_SIMILAR_GROUPS = 1000

KIND_EXACT = "exact"
KIND_SIMILAR = "similar"

# Cypher return alias for the not-analyzed symbol count.
KEY_SKIPPED = "skipped"

# JSON report envelope fields: the group list plus scan-completeness
# metadata, so a saved artifact never reads as a complete scan when symbols
# went unanalyzed or similar-group enumeration hit its cap.
KEY_DUPLICATE_GROUPS = "groups"
KEY_SKIPPED_SYMBOLS = "skipped_symbols"
KEY_TRUNCATED = "truncated"

# find_duplicate_code agentic tool output.
MSG_DUPLICATES_NO_PROJECTS = (
    "No projects are indexed in the graph; index a repository first."
)
MSG_DUPLICATES_AMBIGUOUS = (
    "Multiple projects are indexed: {projects}. Pass project=<name> to pick one."
)
MSG_DUPLICATES_NONE = (
    "No duplicated functions or methods found in project '{project}' "
    "(threshold {threshold}, min size {min_size})."
)
MSG_DUPLICATES_HEADER = (
    "Found {count} duplicate group(s) in project '{project}' "
    "(largest first; 'exact' groups are certain copies, 'similar' carry a score):"
)
MSG_DUPLICATES_GROUP = "{number}. {kind} ({similarity:.0%} similar):"
MSG_DUPLICATES_MEMBER = "   - {qualified_name}  {path}:{start}-{end}"
MSG_DUPLICATES_TRUNCATED = "... {count} more group(s); raise limit to see them."
MSG_DUPLICATES_SKIPPED = (
    "{count} symbol(s) had no structural fingerprint and were not analyzed."
)
MSG_DUPLICATES_GROUPS_TRUNCATED = (
    "Similar-group enumeration reached its cap; some qualifying groups may be "
    "missing. Narrow the scan with a higher threshold or min_size."
)
MSG_DUPLICATES_UNKNOWN_PROJECT = (
    "Project '{project}' is not indexed. Indexed projects: {projects}."
)
MSG_DUPLICATES_BAD_THRESHOLD = (
    "threshold must be between 0 and 1 inclusive; got {threshold}."
)
MSG_DUPLICATES_BAD_MIN_SIZE = "min_size must be at least 1; got {min_size}."
MSG_DUPLICATES_BAD_LIMIT = "limit must be at least 1; got {limit}."
