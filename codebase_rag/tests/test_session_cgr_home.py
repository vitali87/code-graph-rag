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

The vector stores were isolated the same way, in `settings` alone, so a
child read the developer's own: a `QDRANT_URL` from the shell or `.env` sent
a `cgr index` a test started to the live server (PR #3809 review).

The outer tests run the inner ones in a pytest session of their own, with
the user's home pointed at a temporary folder, and look there afterwards:
only from outside can a run tell that nothing reached the real home. The
inner tests run in the suite as well, where they check the real session.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.checkout_state import state_dir
from codebase_rag.config import AppConfig, settings
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
# What a child reads of the settings the suite isolates.
SETTINGS_CHILD = (
    "import json, sys\n"
    "from codebase_rag.config import settings\n"
    "print(json.dumps({name: str(getattr(settings, name)) for name in sys.argv[1:]}))\n"
)
ISOLATED = (
    cs.ENV_CGR_HOME,
    "QDRANT_URL",
    "QDRANT_API_KEY",
    "QDRANT_ALLOW_INSECURE_API_KEY",
    "QDRANT_DB_PATH",
    "MILVUS_URI",
    "SQLITE_VECTOR_PATH",
)
# pymilvus reads MILVUS_URI when imported and refuses a file path there, so
# the suite unsets it rather than export its own: a child reads the default.
UNEXPORTED = {"MILVUS_URI": str(AppConfig.model_fields["MILVUS_URI"].default)}
INNER = (
    "test_a_module_fixture_indexes_under_the_sessions_own_home",
    "test_a_test_still_gets_a_home_of_its_own",
    "test_a_module_fixtures_child_reads_the_sessions_settings",
    "test_a_tests_child_reads_its_own_settings",
)
PASSED = re.compile(rf"\b{len(INNER)} passed\b")


class ModuleRun(NamedTuple):
    home: Path
    exported: str | None
    state: Path
    child_state: Path
    isolated: dict[str, str]
    child_isolated: dict[str, str]


def _isolated_for_children() -> dict[str, str]:
    """This process's isolated settings, as a child it starts should read
    them."""
    return {name: str(getattr(settings, name)) for name in ISOLATED} | UNEXPORTED


def _isolated_in_child(cwd: Path) -> dict[str, str]:
    child = subprocess.run(
        [sys.executable, "-c", SETTINGS_CHILD, *ISOLATED],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
        timeout=120,
    )
    assert child.returncode == 0, child.stderr[-4000:]
    return json.loads(child.stdout.splitlines()[-1])


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
        isolated=_isolated_for_children(),
        child_isolated=_isolated_in_child(base),
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


def test_a_module_fixtures_child_reads_the_sessions_settings(
    module_run: ModuleRun,
) -> None:
    assert module_run.child_isolated == module_run.isolated


def test_a_tests_child_reads_its_own_settings(tmp_path: Path) -> None:
    assert _isolated_in_child(tmp_path) == _isolated_for_children()


def _home_env(home: Path) -> dict[str, str]:
    """The parent's environment with the user's home at `home`, and the user's
    own CGR_HOME unset, as for most users, so the default is its `.cgr`."""
    home.mkdir()
    return {
        key: value
        for key, value in child_env().items()
        if key.upper() != cs.ENV_CGR_HOME
    } | {"HOME": str(home), "USERPROFILE": str(home)}


def _run_inner(
    tmp_path: Path, workers: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
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


@pytest.mark.parametrize("workers", ["0", "2"])
def test_a_session_leaves_nothing_under_the_real_home(
    tmp_path: Path, workers: str
) -> None:
    home = tmp_path / "home"

    result = _run_inner(tmp_path, workers, _home_env(home))

    output = result.stdout + result.stderr
    written = sorted(
        path.relative_to(home).as_posix()
        for path in (home / cs.CGR_HOME_DIRNAME).rglob("*")
    )
    assert not written, "\n".join(["written under the home:", *written])
    assert result.returncode == pytest.ExitCode.OK, output[-4000:]
    assert PASSED.search(output), output[-4000:]


@pytest.mark.parametrize("spell", [str.upper, str.lower], ids=["upper", "lower"])
def test_a_developers_own_settings_reach_no_child(
    tmp_path: Path, spell: Callable[[str], str]
) -> None:
    # A developer's shell, or the `.env` merged into it at import, points the
    # home and the vector stores at real ones: a live Qdrant server among
    # them, which URL mode prefers to any embedded path. The settings read
    # a name in any case, so either spelling must be replaced.
    developer = tmp_path / "developer"
    env = _home_env(tmp_path / "home") | {
        spell(name): value
        for name, value in {
            cs.ENV_CGR_HOME: str(developer / cs.CGR_HOME_DIRNAME),
            "QDRANT_URL": "http://qdrant.developer.invalid:6333",
            "QDRANT_API_KEY": "developer-key",
            "QDRANT_ALLOW_INSECURE_API_KEY": "true",
            "QDRANT_DB_PATH": str(developer / "qdrant"),
            "MILVUS_URI": "http://milvus.developer.invalid:19530",
            "SQLITE_VECTOR_PATH": str(developer / "vectors.sqlite"),
        }.items()
    }

    result = _run_inner(tmp_path, "0", env)

    output = result.stdout + result.stderr
    assert result.returncode == pytest.ExitCode.OK, output[-4000:]
    assert PASSED.search(output), output[-4000:]
