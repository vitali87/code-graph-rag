# Reference graphs of pinned public repositories (issue #3629): each one is
# indexed by whatever `cgr` is installed, exported, and kept as sorted text,
# so a change to what the indexer records shows up in a pull request as a
# change to that text, and the Rust rewrite can be held to it.
from __future__ import annotations

from .lang_tooling import TIER_EXTRAS, LanguageTier

REFERENCE_DIR = "fixtures/reference_graphs"
REFERENCE_MANIFEST = "repos.json"
REFERENCE_GRAPH_SUFFIX = ".graph"
# The install the graphs are recorded and checked in: every tier's extra, so
# every grammar and the ast-grep tier (`make install`). Without the ast-grep
# extra, Ruby and shell files give no nodes. The CI job installs exactly
# these (test_reference_graphs.py checks it).
REFERENCE_EXTRAS = tuple(dict.fromkeys(TIER_EXTRAS[tier] for tier in LanguageTier))
_REFERENCE_EXTRA_FLAGS = " ".join(f"--extra {extra}" for extra in REFERENCE_EXTRAS)
REFERENCE_INSTALL = f"uv sync {_REFERENCE_EXTRA_FLAGS}"
REFERENCE_REGENERATE = (
    f"uv run {_REFERENCE_EXTRA_FLAGS} python scripts/reference_graphs.py record"
)
REFERENCE_CACHE_DIRNAME = "cgr-reference-repos"
REFERENCE_GIT_URL = "https://github.com/{repository}"
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

# Languages with no repository of their own yet: TSX is the TypeScript
# grammar's JSX dialect, and the SQL indexer reads only stored routines,
# which none of the small repositories chosen so far has.
REFERENCE_EXEMPT_LANGUAGES = frozenset({"tsx", "sql"})

# What the install could parse when the graphs were recorded, kept beside
# them one `<tier>\t<name>` line each: the tree-sitter languages that load,
# the languages the ast-grep tier runs, the document tier and the mode each
# frontend setting resolves to. A machine missing one gives that language's
# files no nodes, which would read as hundreds of lost lines; `check`
# compares this first and names what is missing instead.
REFERENCE_ENVIRONMENT_FILE = "environment.txt"
REFERENCE_ENVIRONMENT_COMMAND = "environment"
REFERENCE_IN_PROCESS_FLAG = "--in-process"
REFERENCE_FRONTEND_TIER = "frontend"
REFERENCE_FRONTEND_ENTRY = "{setting}={mode}"
REFERENCE_PROBE_TIMEOUT_S = 300
REFERENCE_PROBE_FAILED = "`{command}` exited {code}\n{stderr}"
REFERENCE_NO_ENVIRONMENT = (
    "There is no {path}, the record of what the fixtures were recorded with; "
    "re-record them all with `{regenerate}`."
)
REFERENCE_CANNOT_PARSE = (
    "this machine cannot parse: {entries}; the fixtures were recorded with them"
)
REFERENCE_ALSO_PARSES = (
    "this machine also parses: {entries}; the fixtures were recorded without them"
)
REFERENCE_ENVIRONMENT_DIFFERS = (
    "{differences}. Install what they were recorded with (`{install}`), "
    "or re-record them all with `{regenerate}`."
)
# `probe` reads what this Python parses, so environment.txt and the graphs
# describe one install only when the index runs under this Python too.
REFERENCE_CGR_PROGRAM = "cgr"
REFERENCE_OTHER_CGR_RECORD = (
    "`{cgr}` does not run under this Python ({python}): environment.txt, read "
    "under this Python, would describe another install than the graphs. "
    "Record with this Python's cgr: `{regenerate}`."
)
REFERENCE_OTHER_CGR_CHECK = (
    "`{cgr}` does not run under this Python, so what it parses is not "
    "compared with {path}; only its graphs are."
)
REFERENCE_TIER_ENTRIES = "{names} ({tier})"
REFERENCE_TIER_MISSING = "{names} ({tier} missing)"
REFERENCE_LIST_SEPARATOR = ", "
REFERENCE_GROUP_SEPARATOR = "; "

REFERENCE_EXIT_DIFFERENT = 1
REFERENCE_EXIT_FAILED = 2
