"""Run `cgr watch` from a source checkout: `python realtime_updater.py REPO`.

The watcher ships with the package as `cgr watch` (`codebase_rag/watch.py`,
issue #2433); this script only stays so a contributor's habit keeps working.
The repository given first becomes `--repo-path`, and every other option is
passed to `cgr watch` unchanged. Memgraph is reached through MEMGRAPH_HOST and
MEMGRAPH_PORT, as for every other cgr command.
"""

import sys

from codebase_rag.cli import app
from codebase_rag.watch import script_args

if __name__ == "__main__":
    app(script_args(sys.argv[1:]))
