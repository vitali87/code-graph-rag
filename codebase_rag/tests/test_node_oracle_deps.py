# (H) Covers the shared node-oracle dependency bootstrap: `node_modules` existing
# (H) is NOT proof of a completed npm install (npm creates the directory before
# (H) populating it), so under pytest-xdist a sibling worker could run an oracle
# (H) against a half-installed tree. The bootstrap must key on a completion marker
# (H) written only after npm succeeds, and must surface node's stderr on failure.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from evals import constants as ec
from evals.oracles._common import ensure_node_deps, run_node_oracle_payload


def _npm_ok() -> MagicMock:
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = "{}"
    proc.stderr = ""
    return proc


def test_half_installed_node_modules_triggers_install(tmp_path: Path) -> None:
    # (H) node_modules exists (npm died mid-install) but the marker is absent: the
    # (H) bootstrap must run npm install anyway, then write the marker.
    (tmp_path / ec.NODE_MODULES_DIRNAME).mkdir()
    with (
        patch("evals.oracles._common.shutil.which", return_value="npm"),
        patch(
            "evals.oracles._common.subprocess.run", return_value=_npm_ok()
        ) as run_mock,
    ):
        ensure_node_deps(tmp_path)
    run_mock.assert_called_once()
    assert (tmp_path / ec.NODE_DEPS_MARKER).exists()


def test_stale_marker_without_node_modules_reinstalls(tmp_path: Path) -> None:
    # (H) Cache cleanup can remove node_modules but leave the gitignored marker;
    # (H) the bootstrap must drop the stale marker and reinstall, not run the
    # (H) oracle with missing packages.
    (tmp_path / ec.NODE_DEPS_MARKER).touch()
    with (
        patch("evals.oracles._common.shutil.which", return_value="npm"),
        patch(
            "evals.oracles._common.subprocess.run", return_value=_npm_ok()
        ) as run_mock,
    ):
        ensure_node_deps(tmp_path)
    run_mock.assert_called_once()


def test_completed_marker_skips_install(tmp_path: Path) -> None:
    (tmp_path / ec.NODE_MODULES_DIRNAME).mkdir()
    (tmp_path / ec.NODE_DEPS_MARKER).touch()
    with (
        patch("evals.oracles._common.shutil.which", return_value="npm"),
        patch("evals.oracles._common.subprocess.run") as run_mock,
    ):
        ensure_node_deps(tmp_path)
    run_mock.assert_not_called()


def test_concurrent_installer_lock_is_waited_out(tmp_path: Path) -> None:
    # (H) Another worker holds the lock and finishes (writes the marker) while we
    # (H) poll: no second npm install must run.
    (tmp_path / ec.NODE_DEPS_LOCK).mkdir()

    def _finish_install(_seconds: float) -> None:
        (tmp_path / ec.NODE_MODULES_DIRNAME).mkdir(exist_ok=True)
        (tmp_path / ec.NODE_DEPS_MARKER).touch()

    with (
        patch("evals.oracles._common.shutil.which", return_value="npm"),
        patch("evals.oracles._common.subprocess.run") as run_mock,
        patch("evals.oracles._common.time.sleep", side_effect=_finish_install),
    ):
        ensure_node_deps(tmp_path)
    run_mock.assert_not_called()


def test_failed_oracle_surfaces_stderr(tmp_path: Path) -> None:
    # (H) exit 1 from the node script must raise with the script's stderr in the
    # (H) message, not a blind CalledProcessError (CI debugging depends on it).
    (tmp_path / ec.NODE_DEPS_MARKER).touch()
    proc = MagicMock()
    proc.returncode = 1
    proc.stdout = ""
    proc.stderr = "Cannot find module 'typescript'"
    script = tmp_path / "ts_ast.js"
    with (
        patch("evals.oracles._common.shutil.which", return_value="node"),
        patch("evals.oracles._common.subprocess.run", return_value=proc),
        pytest.raises(RuntimeError, match="Cannot find module"),
    ):
        run_node_oracle_payload(tmp_path, script, ())
