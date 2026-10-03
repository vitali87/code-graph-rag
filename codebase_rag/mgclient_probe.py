"""One mgclient session for a Memgraph probe, in a process of its own.

On Windows, pymgclient's extension is a MinGW build that prints its transport
errors through msvcrt.dll, while Python's os module works on the UCRT. Both C
runtimes start with fd 2 naming the same OS handle, and moving fd 2 in either
one closes that handle under the other, so cgr's own process cannot capture
what mgclient prints there (issue #2356). This process is started with a pipe
as stderr, and nothing in it moves or closes a descriptor, so whatever
mgclient prints reaches the parent, which logs it.

The request arrives on stdin, so a login never appears on a command line, and
the report leaves on stdout. Only the stdlib and mgclient are imported:
importing `codebase_rag.stack` would cost each probe its settings and manager.
"""

from __future__ import annotations

import ctypes
import json
import sys

import mgclient

from .types_defs import MgclientProbeReport, MgclientProbeRequest

# pymgclient 1.6 re-exports its C extension through `import *`, which a type
# checker cannot see into, so the exception type is bound once here.
_MgclientError: type[Exception] = mgclient.Error  # ty: ignore[unresolved-attribute]


def run(request: MgclientProbeRequest) -> MgclientProbeReport:
    # The calls the in-process probes in codebase_rag/stack/health.py make,
    # in the same order and with the same arguments.
    host, port = request["host"], request["port"]
    try:
        if credentials := request["credentials"]:
            username, password = credentials
            conn = mgclient.connect(
                host=host, port=port, username=username, password=password
            )
        else:
            conn = mgclient.connect(host=host, port=port)
    except _MgclientError as e:
        return MgclientProbeReport(succeeded=False, connect_error=str(e))
    except OSError:
        return MgclientProbeReport(succeeded=False, connect_error=None)
    try:
        if (query := request["query"]) is not None:
            cursor = conn.cursor()
            cursor.execute(query)
            cursor.fetchall()
    except (_MgclientError, OSError):
        return MgclientProbeReport(succeeded=False, connect_error=None)
    finally:
        conn.close()
    return MgclientProbeReport(succeeded=True, connect_error=None)


def main() -> int:
    request: MgclientProbeRequest = json.loads(sys.stdin.read())
    try:
        report = run(request)
    finally:
        # msvcrt.dll keeps what fprintf(stderr) writes in a buffer while fd 2
        # is a pipe rather than a console, and that is how mgclient reports a
        # connection the server closed. python.exe is linked against the
        # UCRT, whose streams its exit flushes; nothing in it asks msvcrt.dll
        # to flush its own, so that is done here, even after a failure.
        ctypes.CDLL(request["c_runtime"]).fflush(None)
    sys.stdout.write(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
