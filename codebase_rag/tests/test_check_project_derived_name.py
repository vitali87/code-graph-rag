"""`cgr check --project <derived name>` reads the stamp `cgr start` wrote.

`cgr start` without `--project-name` indexes under the derived name
(`<dir>__<digest of the path>`) and stamps its exclusion scope as an
unnamed run. `cgr check --project` with that same name was refused, with a
message naming one project on both sides: "belongs to project X, not X"
(issue #2854). The derived name hashes this checkout's path, so it can
only mean the graph that unnamed run wrote.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_check import CheckError, indexed_scope
from codebase_rag.utils.path_utils import derive_project_name
from evals.cgr_graph import _StatefulIngestor

_EXCLUDED = frozenset({"generated"})


def _index(root: Path, project_name: str | None, named: bool) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "lib.py").write_text("def greet(name):\n    return name\n")
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=_StatefulIngestor(),
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=project_name,
        project_named=named,
        exclude_paths=_EXCLUDED,
    ).run(force=True)


def test_the_derived_name_reads_the_default_runs_scope(tmp_path: Path) -> None:
    root = tmp_path / "chkproj"
    # What `cgr start --repo-path .` does: the derived name, unnamed.
    _index(root, derive_project_name(root.resolve()), named=False)

    derived = derive_project_name(root.resolve())
    assert indexed_scope(root, derived, explicit=True) == (_EXCLUDED, None)
    assert indexed_scope(root, derived) == (_EXCLUDED, None)


def test_a_refusal_never_names_one_project_twice(tmp_path: Path) -> None:
    # Negative: an unnamed stamp under the bare directory name still does
    # not serve an explicit `--project <dir>`, which could be a named
    # project of that name whose scope the unnamed run overwrote. The
    # message says so instead of "belongs to project X, not X".
    root = tmp_path / "chkproj"
    _index(root, None, named=False)

    with pytest.raises(CheckError) as refused:
        indexed_scope(root, root.name, explicit=True)
    message = str(refused.value)
    assert f"{root.name}, not {root.name}" not in message, message
    assert "named no project" in message, message
    assert f"--project-name {root.name}" in message, message


def test_another_project_is_still_refused(tmp_path: Path) -> None:
    # Negative: a different explicit project never borrows this scope.
    root = tmp_path / "chkproj"
    _index(root, derive_project_name(root.resolve()), named=False)

    with pytest.raises(CheckError) as refused:
        indexed_scope(root, "other", explicit=True)
    owner = derive_project_name(root.resolve())
    assert f"belongs to project {owner}, not other" in str(refused.value)


def test_a_named_run_still_answers_to_its_name(tmp_path: Path) -> None:
    # Negative: the named path is unchanged.
    root = tmp_path / "chkproj"
    _index(root, "chosen", named=True)

    assert indexed_scope(root, "chosen", explicit=True) == (_EXCLUDED, None)
    derived = derive_project_name(root.resolve())
    with pytest.raises(CheckError):
        indexed_scope(root, derived, explicit=True)
