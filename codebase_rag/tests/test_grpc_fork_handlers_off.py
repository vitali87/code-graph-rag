"""The suite runs with gRPC's fork handlers off.

On macOS every subprocess a test starts is a real fork() of the pytest worker,
and once a milvus-lite test has started gRPC in that worker, gRPC's
pthread_atfork handlers run in each later child before it execs. Children on
main's macOS unit shards have died with no output at all: build-lock holders
that never printed "locked", a toolchain probe that exited non-zero, benchmark
children killed by SIGTRAP. Every child the suite spawns execs at once, so
the handlers do nothing for it but run code ahead of that exec; conftest
turns fork support off and gRPC registers none.
"""

import os


def test_grpc_fork_support_is_off_for_every_test() -> None:
    # gRPC reads the variable once, when it first initializes; conftest sets
    # it at import, before any test can import pymilvus and with it gRPC.
    assert os.environ.get("GRPC_ENABLE_FORK_SUPPORT") == "false"
