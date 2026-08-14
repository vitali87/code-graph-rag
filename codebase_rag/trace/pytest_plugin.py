"""Pytest plugin exposing the cgr call-graph tracer.

Inert unless ``--cgr-trace`` is passed. Each test's node id becomes the
workload provenance for the calls it triggers, and the aggregated trace is
written once at session end.

The pytest11 entry point makes pytest import this module at startup in every
session, tracing or not, and before coverage tooling initialises. All tracer
machinery is therefore imported lazily inside the hooks: untraced sessions
pay nothing, and import-time lines are not misattributed as uncovered.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .tracer import CallGraphTracer

_OPT_TRACE = "--cgr-trace"
_OPT_OUTPUT = "--cgr-trace-output"
_OPT_REPO = "--cgr-trace-repo"
_STASH_KEY: pytest.StashKey[CallGraphTracer] = pytest.StashKey()

_HELP_TRACE = "Record a cgr runtime call trace for this test session."
_HELP_OUTPUT = "Where to write the trace file (default: %(default)s)."
_HELP_REPO = "Repository root to scope tracing to (default: pytest rootdir)."


def pytest_addoption(parser: pytest.Parser) -> None:
    from .. import constants as cs

    group = parser.getgroup(cs.TRACE_TOOL_NAME)
    group.addoption(_OPT_TRACE, action="store_true", default=False, help=_HELP_TRACE)
    group.addoption(
        _OPT_OUTPUT,
        default=cs.TRACE_DEFAULT_OUTPUT,
        help=_HELP_OUTPUT,
    )
    group.addoption(_OPT_REPO, default=None, help=_HELP_REPO)


def pytest_configure(config: pytest.Config) -> None:
    if not config.getoption(_OPT_TRACE):
        return
    from .tracer import CallGraphTracer

    repo_opt = config.getoption(_OPT_REPO)
    repo_root = Path(repo_opt) if repo_opt else Path(str(config.rootpath))
    tracer = CallGraphTracer(repo_root)
    config.stash[_STASH_KEY] = tracer
    tracer.start()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(
    item: pytest.Item, nextitem: pytest.Item | None
) -> Iterator[object]:
    tracer = item.config.stash.get(_STASH_KEY, None)
    if tracer is not None:
        tracer.set_workload(item.nodeid)
    try:
        return (yield)
    finally:
        if tracer is not None:
            tracer.set_workload(None)


class _TraceOutputConfig(Protocol):
    """The slice of ``pytest.Config`` that output-path resolution reads."""

    def getoption(self, name: str) -> object: ...


def _output_path(config: _TraceOutputConfig) -> Path:
    output = Path(str(config.getoption(_OPT_OUTPUT)))
    # Under pytest-xdist every worker process traces its own interpreter and
    # writes at session end; a shared name would leave only the last worker's
    # trace. Ingest all per-worker files to cover the full run.
    workerinput = getattr(config, "workerinput", None)
    worker = workerinput.get("workerid") if isinstance(workerinput, dict) else None
    if worker:
        output = output.with_name(f"{output.stem}-{worker}{output.suffix}")
    return output


def pytest_sessionfinish(session: pytest.Session) -> None:
    tracer = session.config.stash.get(_STASH_KEY, None)
    if tracer is None or not tracer.active:
        return
    from .. import constants as cs

    tracer.stop()
    output = _output_path(session.config)
    count = tracer.write(output)
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(
            f"{cs.TRACE_TOOL_NAME}: wrote {count} call records to {output}"
        )


def pytest_unconfigure(config: pytest.Config) -> None:
    tracer = config.stash.get(_STASH_KEY, None)
    if tracer is not None and tracer.active:
        tracer.stop()
