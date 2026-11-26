"""What the shadow plugins and the tests that run them share.

A shadow plugin (`ignore_rules_shadow.py`, `discovery_shadow.py`,
`change_shadow.py`) compares the frame with Python wherever a test run asks
Python, and keeps each difference in a list. `ShadowFailures` turns that
list into failures: a difference fails the phase of the test that made it,
its setup, call or teardown, so one made in a fixture fails too; any
difference fails the session. Under xdist each worker hands its
differences to the controller, which fails its own session with them: the
controller decides the run's exit status, and does not read a worker's.

`child_env` is the environment a test runs pytest in for such a plugin:
the parent's, without the variables pytest and xdist set or read, so the
child runs its own files with its own options.
"""

from __future__ import annotations

import os
from collections.abc import Generator, Mapping

import pytest

SHOWN = 20
WORKER_OUTPUT_KEY = "{name}_differences"
FROM_WORKERS = "{name}: {count} differences on the workers"
PYTEST_ENV_PREFIX = "PYTEST_"


class ShadowFailures:
    """The hooks that fail a run on a shadow plugin's differences."""

    def __init__(self, name: str, differences: list[str], heading: str) -> None:
        self.name = name
        self._differences = differences
        self._heading = heading
        self._from_workers: list[str] = []
        self._key = WORKER_OUTPUT_KEY.format(name=name)

    def _phase(self) -> Generator[None, object, object]:
        before = len(self._differences)
        result = yield
        if found := self._differences[before:]:
            pytest.fail(self._heading + "\n".join(found[:SHOWN]), pytrace=False)
        return result

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_setup(
        self, item: pytest.Item
    ) -> Generator[None, object, object]:
        return (yield from self._phase())

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(self, item: pytest.Item) -> Generator[None, object, object]:
        return (yield from self._phase())

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_teardown(
        self, item: pytest.Item, nextitem: pytest.Item | None
    ) -> Generator[None, object, object]:
        return (yield from self._phase())

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node: object, error: object) -> None:
        # On the controller: a worker finished, its output read back.
        output = getattr(node, "workeroutput", None)
        if isinstance(output, Mapping):
            self._from_workers.extend(output.get(self._key, ()))

    def pytest_terminal_summary(
        self, terminalreporter: pytest.TerminalReporter
    ) -> None:
        if self._from_workers:
            terminalreporter.write_line(
                FROM_WORKERS.format(name=self.name, count=len(self._from_workers))
            )
            for difference in self._from_workers[:SHOWN]:
                terminalreporter.write_line(difference)

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        if (
            self._differences or self._from_workers
        ) and session.exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
        # On a worker: sent to the controller once this hook returns.
        output = getattr(session.config, "workeroutput", None)
        if isinstance(output, dict):
            output[self._key] = list(self._differences)


def child_env() -> dict[str, str]:
    """The environment for a child pytest: the parent's without pytest's and
    xdist's variables, among them `PYTEST_ADDOPTS`, whose options (a `-k`
    filter, say) would apply to the child's files too."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(PYTEST_ENV_PREFIX)
    }
