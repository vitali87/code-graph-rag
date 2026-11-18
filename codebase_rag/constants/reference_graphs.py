# Reference graphs of pinned public repositories (issue #3629): each one is
# indexed by whatever `cgr` is installed, exported, and kept as sorted text,
# so a change to what the indexer records shows up in a pull request as a
# change to that text, and the Rust rewrite can be held to it.
from __future__ import annotations

REFERENCE_DIR = "fixtures/reference_graphs"
REFERENCE_MANIFEST = "repos.json"
REFERENCE_GRAPH_SUFFIX = ".graph"
REFERENCE_REGENERATE = "uv run python scripts/reference_graphs.py record"
REFERENCE_CACHE_DIRNAME = "cgr-reference-repos"
REFERENCE_GIT_URL = "https://github.com/{repository}"
# A checkout is complete once this file exists in it; an interrupted fetch
# leaves none, and the next run starts it over.
REFERENCE_READY_MARKER = ".cgr-reference-ready"
REFERENCE_INDEX_TIMEOUT_S = 900
REFERENCE_FETCH_TIMEOUT_S = 300
REFERENCE_DIFF_LINES = 200

# What every reference index runs with, whatever the machine has installed:
# the embedded graph, no embeddings, and the frontends that need no compiler,
# so a JDK, Go or libclang on PATH cannot change the graph.
REFERENCE_ENV = {
    "GRAPH_BACKEND": "sqlite",
    "CGR_SKIP_EMBEDDINGS": "true",
    "CPP_FRONTEND": "treesitter",
    "CSHARP_FRONTEND": "treesitter",
    "GO_FRONTEND": "treesitter",
    "JAVA_FRONTEND": "heuristic",
    "PYTHON_FRONTEND": "heuristic",
}
# Settings that would point the index somewhere other than its own home.
REFERENCE_UNSET_ENV = (
    "SQLITE_GRAPH_PATH",
    "SQLITE_VECTOR_PATH",
    "CGR_VECTOR_STORE_BACKEND",
    "LOMBOK_JAR",
)
REFERENCE_HOME_ENV = "CGR_HOME"
REFERENCE_SEED_ENV = "PYTHONHASHSEED"

# Properties whose value is the time of the run, not a fact about the code.
REFERENCE_VOLATILE_PROPERTIES = frozenset({"last_synced_at"})
# Stands for the checkout's own directory in paths, so a graph reads the
# same wherever the repository was checked out.
REFERENCE_ROOT_PLACEHOLDER = "<root>"
REFERENCE_NODE_TAG = "N"
REFERENCE_EDGE_TAG = "E"
REFERENCE_FIELD_SEPARATOR = "\t"
REFERENCE_LABEL_SEPARATOR = ":"

REFERENCE_EXIT_DIFFERENT = 1
REFERENCE_EXIT_FAILED = 2
