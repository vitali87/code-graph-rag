"""`make watch` leaves the Memgraph address to the watcher's own settings.

`realtime_updater.py` defaults `--host`/`--port` to `MEMGRAPH_HOST` and
`MEMGRAPH_PORT` from the environment or `.env`, as `cgr start` does. The make
target passed `--host localhost --port 7687` unless `HOST=`/`PORT=` were
given, so a user indexing into 7688 had the watcher write into whatever
graph was on 7687 (issue #2885).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="needs make")


def _watch_command(*make_vars: str, env: dict[str, str] | None = None) -> str:
    """The command `make watch` would run, without running it."""
    result = subprocess.run(
        ["make", "-n", "watch", "REPO_PATH=/tmp/repo", *make_vars],
        cwd=_REPO_ROOT,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        check=True,
    )
    return " ".join(result.stdout.split())


def test_the_environment_port_is_not_overridden() -> None:
    command = _watch_command(env={"MEMGRAPH_PORT": "7704"})
    assert "realtime_updater.py /tmp/repo" in command, command
    assert "--port" not in command, command
    assert "--host" not in command, command


@pytest.mark.parametrize(
    ("make_vars", "expected"),
    [
        (("PORT=7999",), "--port 7999"),
        (("HOST=graph.local",), "--host graph.local"),
        (("BATCH_SIZE=500",), "--batch-size 500"),
    ],
)
def test_an_explicit_make_variable_still_wins(
    make_vars: tuple[str, ...], expected: str
) -> None:
    # Negative: the documented overrides keep working.
    assert expected in _watch_command(*make_vars)
