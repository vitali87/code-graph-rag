"""A test session writes nothing under the real CGR_HOME (issue #3808).

The suite gave each test a fresh CGR_HOME through a function-scoped autouse
fixture, and module- and session-scoped fixtures are set up before any
function-scoped one. A fixture that indexed a repository at module scope
therefore wrote its checkout state (hash cache, parser fingerprint, sync
lock) into the developer's `~/.cgr/state`, and so did every `cgr`, script or
nested pytest a test started, since the home lived only in `settings`. One
container gathered 22,687 such folders. The state folder is named from the
checkout's path, and pytest reuses `pytest-0`, `pytest-1`, ... once its temp
root is cleared, so a later session could find an earlier one's hash cache
and sync where its test expected a fresh index.

The outer test runs the inner ones in a pytest session of their own, with
the user's home pointed at a temporary folder, and looks there afterwards:
only from outside can a run tell that nothing reached the real home. The
inner tests run in the suite as well, where they check the real session.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.checkout_state import state_dir
from codebase_rag.config import settings
from codebase_rag.tests.conftest import create_and_run_updater
from codebase_rag.tests.shadows import child_env

REPO_ROOT = Path(__file__).resolve().parents[2]
# What a `cgr` a fixture starts does first with a checkout: make its state
# folder under the CGR_HOME it reads.
CHILD = (
    "import sys\n"
    "from pathlib import Path\n"
    "from codebase_rag.checkout_state import prepare_state_dir\n"
    "print(prepare_state_dir(Path(sys.argv[1])))\n"
)
INNER = (
    "test_a_module_fixture_indexes_under_the_sessions_own_home",
    "test_a_test_still_gets_a_home_of_its_own",
)
PASSED = re.compile(rf"\b{len(INNER)} passed\b")


class ModuleRun(NamedTuple):
    home: Path
    exported: str | None
    state: Path
    child_state: Path


@pytest.fixture(scope="module")
def module_run(tmp_path_factory: pytest.TempPathFactory) -> ModuleRun:
    base = tmp_path_factory.mktemp("module-home")
    repo = base / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("def f():\n    return 1\n", encoding=cs.ENCODING_UTF8)
    create_and_run_updater(repo, MagicMock())
    child_repo = base / "child-repo"
    child_repo.mkdir()
    child = subprocess.run(
        [sys.executable, "-c", CHILD, str(child_repo)],
        cwd=base,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
        timeout=120,
    )
    assert child.returncode == 0, child.stderr[-4000:]
    return ModuleRun(
        home=settings.CGR_HOME,
        exported=os.environ.get(cs.ENV_CGR_HOME),
        state=state_dir(repo),
        child_state=Path(child.stdout.strip()),
    )


def test_a_module_fixture_indexes_under_the_sessions_own_home(
    module_run: ModuleRun,
) -> None:
    assert module_run.exported == str(module_run.home)
    assert not module_run.home.is_relative_to(Path.home() / cs.CGR_HOME_DIRNAME)
    # The index did write its state, so finding none under the real home is
    # no accident of a run that wrote nothing.
    assert (module_run.state / cs.HASH_CACHE_FILENAME).is_file()
    assert module_run.state.is_relative_to(module_run.home)
    assert module_run.child_state.is_relative_to(module_run.home)


def test_a_test_still_gets_a_home_of_its_own(module_run: ModuleRun) -> None:
    assert settings.CGR_HOME != module_run.home
    assert os.environ.get(cs.ENV_CGR_HOME) == str(settings.CGR_HOME)


@pytest.mark.parametrize("workers", ["0", "2"])
def test_a_session_leaves_nothing_under_the_real_home(
    tmp_path: Path, workers: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    # The user's own CGR_HOME is unset, as for most users, so the session's
    # default is the home's `.cgr`.
    env = {
        key: value
        for key, value in child_env().items()
        if key.upper() != cs.ENV_CGR_HOME
    } | {"HOME": str(home), "USERPROFILE": str(home)}

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-n",
            workers,
            # Its own temporary root, out of the outer run's sweep (#3773).
            "--basetemp",
            str(tmp_path / "basetemp"),
            *(f"{__file__}::{name}" for name in INNER),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
        timeout=600,
    )

    output = result.stdout + result.stderr
    written = sorted(
        path.relative_to(home).as_posix()
        for path in (home / cs.CGR_HOME_DIRNAME).rglob("*")
    )
    assert not written, "\n".join(["written under the home:", *written])
    assert result.returncode == pytest.ExitCode.OK, output[-4000:]
    assert PASSED.search(output), output[-4000:]
