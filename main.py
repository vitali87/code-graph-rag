#!/usr/bin/env python3

if __name__ == "__main__":
    import sys

    from codebase_rag.constants import MGCLIENT_PROBE_CHILD_ARG

    # The frozen binary is its own interpreter: on Windows each Memgraph
    # probe starts it again with this argument (codebase_rag/stack/health.py).
    if sys.argv[1:] == [MGCLIENT_PROBE_CHILD_ARG]:
        from codebase_rag import mgclient_probe

        sys.exit(mgclient_probe.main())

    from codebase_rag.cli import app

    app()
