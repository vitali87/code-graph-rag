import json
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag.cli import app
from codebase_rag.editing.rename import rename
from codebase_rag.editing.transaction import (
    EditTransaction,
    history_path,
    load_history,
    undo_transaction,
)
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.tests.test_edit_contract import PROJECT, _real_project
from evals.cgr_graph import _StatefulIngestor


@pytest.fixture
def collision(tmp_path: Path) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    root = tmp_path / PROJECT
    root.mkdir()
    store, updater = _real_project(
        root,
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "def assist(a):\n    return a\n\n\ndef helper(a):\n    return a\n",
            "pkg/app.py": "from pkg.util import helper\n\n\ndef run():\n    return helper(1)\n",
        },
    )
    return root, store, updater


def _reingest_with_interference(
    root: Path, updater: GraphUpdater, interference: str, calls: list[list[str]]
) -> Callable[[list[str]], None]:
    reingest = updater.reingest

    def interfere(paths: list[str]) -> None:
        calls.append(paths)
        reingest(paths)
        if len(calls) != 1:
            return
        match interference:
            case "newer_edit":
                tx = EditTransaction(root)
                tx.stage("pkg/note.py", "# later edit\n")
                assert tx.commit().applied
            case "hand_edit":
                path = root / "pkg/util.py"
                path.write_text(path.read_text() + "\n# hand edit\n")
            case "empty_entry":
                entries = load_history(root)
                entries[-1][cs.EDIT_KEY_FILES] = []
                history_path(root).write_text(json.dumps(entries))
            case "missing_history":
                history_path(root).unlink()
            case "already_undone":
                entries = load_history(root)
                outcome = undo_transaction(root, str(entries[-1][cs.EDIT_KEY_ID]))
                assert outcome.applied

    return interfere


@pytest.mark.parametrize(
    ("interference", "undone", "reingest_count"),
    [
        ("newer_edit", False, 1),
        ("hand_edit", False, 1),
        ("empty_entry", False, 1),
        ("missing_history", False, 1),
        ("already_undone", True, 1),
        ("none", True, 2),
    ],
)
def test_failed_contract_reports_rollback_without_running_success_hook(
    collision: tuple[Path, _StatefulIngestor, GraphUpdater],
    interference: str,
    undone: bool,
    reingest_count: int,
) -> None:
    root, store, updater = collision
    before = {path: path.read_bytes() for path in root.rglob("*.py")}
    calls: list[list[str]] = []
    after_apply = MagicMock()

    report = rename(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.util.helper",
        "assist",
        reingest=_reingest_with_interference(root, updater, interference, calls),
        after_apply=after_apply,
    )

    assert report.verdict is not None and not report.verdict.ok
    after_apply.assert_not_called()
    assert not report.applied
    if undone:
        assert all(path.read_bytes() == content for path, content in before.items())
        assert load_history(root) == []
    else:
        assert "def helper(" not in (root / "pkg/util.py").read_text()
        assert "from pkg.util import assist" in (root / "pkg/app.py").read_text()
        assert "check the working tree" in report.message
    assert report.undone is undone
    assert len(calls) == reingest_count
    match interference:
        case "newer_edit":
            assert (root / "pkg/note.py").read_text() == "# later edit\n"
            assert len(load_history(root)) == 2
        case "hand_edit":
            assert "# hand edit" in (root / "pkg/util.py").read_text()
            assert "pkg/util.py" in report.message
            assert len(load_history(root)) == 1
        case "empty_entry":
            assert cs.EDIT_NOTHING_STAGED in report.message
            assert len(load_history(root)) == 1


@pytest.mark.parametrize(
    "interference", ["newer_edit", "hand_edit", "empty_entry", "none"]
)
def test_cli_failed_contract_exits_nonzero_and_serializes_rollback_status(
    collision: tuple[Path, _StatefulIngestor, GraphUpdater], interference: str
) -> None:
    root, store, updater = collision
    calls: list[list[str]] = []
    reingest = _reingest_with_interference(root, updater, interference, calls)
    with (
        patch(
            "codebase_rag.graph_cli._project_and_fetch",
            return_value=(PROJECT, store.fetch_all, MagicMock()),
        ),
        patch("codebase_rag.cli.GraphUpdater", return_value=updater),
        patch.object(updater, "reingest", side_effect=reingest),
    ):
        result = CliRunner().invoke(
            app,
            [
                "rename",
                f"{PROJECT}.pkg.util.helper",
                "assist",
                "--repo-path",
                str(root),
                "--project",
                PROJECT,
            ],
        )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["applied"] is False
    assert payload["verdict"]["ok"] is False
    assert payload["undone"] is (interference == "none")


@pytest.mark.parametrize("interference", ["newer_edit", "empty_entry", "none"])
def test_mcp_failed_contract_serializes_rollback_status(
    collision: tuple[Path, _StatefulIngestor, GraphUpdater], interference: str
) -> None:
    root, store, updater = collision
    calls: list[list[str]] = []
    registry = MCPToolsRegistry.__new__(MCPToolsRegistry)
    registry.ingestor = store
    registry.project_root = str(root)
    with patch.object(
        registry,
        "_guarded_rename_reingest",
        return_value=_reingest_with_interference(root, updater, interference, calls),
    ):
        payload = registry._run_rename(
            PROJECT, f"{PROJECT}.pkg.util.helper", "assist", False, False
        )

    assert payload["applied"] is False
    assert payload["verdict"]["ok"] is False
    assert payload["undone"] is (interference == "none")


@pytest.mark.parametrize("mode", ["measured", "unchecked", "dry_run"])
def test_rename_without_contract_failure_has_no_rollback_status(
    collision: tuple[Path, _StatefulIngestor, GraphUpdater], mode: str
) -> None:
    root, store, updater = collision
    after_apply = MagicMock()

    report = rename(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.util.helper",
        "renamed",
        reingest=None if mode == "unchecked" else updater.reingest,
        dry_run=mode == "dry_run",
        after_apply=after_apply,
    )

    assert report.undone is None
    assert report.applied is (mode != "dry_run")
    if mode == "measured":
        assert report.verdict is not None and report.verdict.ok
    if mode == "dry_run":
        after_apply.assert_not_called()
    else:
        after_apply.assert_called_once_with(list(report.files))
