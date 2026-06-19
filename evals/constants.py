from enum import StrEnum

from codebase_rag import constants as cs

PY_SUFFIX = ".py"
MODULE_START_LINE = 0

SCORED_NODE_KINDS: tuple[cs.NodeLabel, ...] = (
    cs.NodeLabel.MODULE,
    cs.NodeLabel.CLASS,
    cs.NodeLabel.FUNCTION,
    cs.NodeLabel.METHOD,
)
SCORED_NODE_KIND_VALUES: frozenset[str] = frozenset(k.value for k in SCORED_NODE_KINDS)
SPANNED_NODE_KINDS: frozenset[str] = frozenset(
    {
        cs.NodeLabel.CLASS.value,
        cs.NodeLabel.FUNCTION.value,
        cs.NodeLabel.METHOD.value,
    }
)

SCORED_EDGE_TYPES: tuple[cs.RelationshipType, ...] = (
    cs.RelationshipType.DEFINES,
    cs.RelationshipType.DEFINES_METHOD,
)
SCORED_EDGE_TYPE_VALUES: frozenset[str] = frozenset(e.value for e in SCORED_EDGE_TYPES)

IGNORE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "build",
        "dist",
        "site",
        "node_modules",
        ".ruff_cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ty_cache",
    }
)
EGG_INFO_SUFFIX = ".egg-info"


class Category(StrEnum):
    NODE = "node"
    EDGE = "edge"


AGGREGATE_LABEL = "ALL"

CSV_FIELDS: tuple[str, ...] = (
    "category",
    "label",
    "tp",
    "fp",
    "fn",
    "precision",
    "recall",
    "f1",
)

DEFAULT_TARGET = "codebase_rag"
DEFAULT_OUT_DIR = "evals/results"
SCORES_FILENAME = "scores.csv"
DIFF_FILENAME = "diff.json"

NODE_REPR = "{kind} {file}:{start} {name}"
EDGE_REPR = "{rel} {pfile}:{pstart} -> {cfile}:{cstart}"
DIFF_NODE_PREFIX = "node:"
DIFF_EDGE_PREFIX = "edge:"

ROUND_DIGITS = 4
