"""Property tests for the prune candidate predicate (#2479 / PR #3221).

`root_proven_missing` is a pure function over (path, filesystem state), and
`cgr prune` destroys whole graphs on its answer — so its contract is pinned as
invariants over adversarial inputs, not point cases:

  I1. a path that stat() proves absent raises into the MISSING branch → True;
  I2. ANY other OSError (EACCES, EIO, ESTALE…) → False (fail-closed: an
      undeterminable check never selects a project for destruction);
  I3. a stat that succeeds on a non-directory → True (the checkout is gone);
  I4. a stat that succeeds on a directory → False (live).

Hypothesis is deliberately not used: it is not a repo dependency (the fuzz
harnesses run on atheris/ClusterFuzzLite per CONTRIBUTING.md), and adding it
for one predicate would violate the minimal-scope rule. The properties are
exhaustively enumerated instead — the input space here is the OSError errno
set plus two stat outcomes, which is small and fully covered below.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from codebase_rag.utils.path_utils import root_proven_missing

# (errno, exception the OS layer raises for it, expected proven-missing)
_ERRNO_CASES: tuple[tuple[int, type[OSError], bool], ...] = (
    (errno.ENOENT, FileNotFoundError, True),
    (errno.ENOTDIR, NotADirectoryError, True),
    (errno.EACCES, PermissionError, False),
    (errno.EIO, OSError, False),
    (errno.ESTALE, OSError, False),
    (errno.ELOOP, OSError, False),
    (errno.EPERM, PermissionError, False),
    (errno.ETIMEDOUT, TimeoutError, False),  # TimeoutError is an OSError subclass
)


class TestAbsenceInvariants:
    @pytest.mark.parametrize("err,exc,expected", _ERRNO_CASES)
    def test_stat_failure_errnos(
        self, err: int, exc: type[OSError], expected: bool
    ) -> None:
        # I1 + I2: the OSError class carries the errno; missing/ENOTDIR prove
        # absence, every other errno leaves the question open (fail-closed).
        def _raise(path: str) -> None:
            raise exc(err, f"forced errno {err}", path)

        with patch("codebase_rag.utils.path_utils.os.stat", side_effect=_raise):
            assert root_proven_missing("/any/path") is expected

    def test_directory_stat_means_live(self, tmp_path: Path) -> None:
        # I4: a stat that succeeds on a directory is a live checkout.
        live = tmp_path / "checkout"
        live.mkdir()
        assert root_proven_missing(str(live)) is False

    def test_non_directory_stat_means_missing(self, tmp_path: Path) -> None:
        # I3: a stat that succeeds on a non-directory proves the checkout is
        # gone — a same-named file at the recorded root is not a checkout.
        replaced = tmp_path / "checkout"
        replaced.write_text("not a checkout")
        assert root_proven_missing(str(replaced)) is True

    @pytest.mark.skipif(
        os.name == "nt", reason="symlink privilege not guaranteed on runners"
    )
    def test_dangling_symlink_is_proven_missing(self, tmp_path: Path) -> None:
        # ENOENT through a broken leaf link: absence proven, safe to prune.
        dangling = tmp_path / "dangling"
        dangling.symlink_to(tmp_path / "nowhere")
        assert root_proven_missing(str(dangling)) is True

    @pytest.mark.skipif(
        os.name == "nt", reason="symlink privilege not guaranteed on runners"
    )
    def test_symlink_to_a_directory_is_live(self, tmp_path: Path) -> None:
        # stat() follows links: a link to a live checkout is a live checkout.
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"
        link.symlink_to(target)
        assert root_proven_missing(str(link)) is False
