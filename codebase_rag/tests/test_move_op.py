# move (issue #1534): a definition is cut from its module with the imports
# it needs and pasted into the target, every importer is rewritten, the old
# module keeps an import (or a re-export with keep_alias), and a move that
# would create an import cycle is refused before any file is touched.

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing import MoveRefused, move
from codebase_rag.editing.transaction import load_history
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

PROJECT = "move_fixture"
FIXTURE: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/util.py": (
        "import os\n"
        "from pathlib import Path\n\n\n"
        "def other():\n    return Path('.')\n\n\n"
        "# joins with the platform separator\n"
        'def helper(a):\n    """Join."""\n    return os.sep.join(a)\n\n\n'
        "def unrelated():\n    return 1\n"
    ),
    "pkg/a.py": "from pkg.util import helper\n\n\ndef run():\n    return helper(['x', 'y'])\n",
    "pkg/b.py": "from pkg.util import helper, other\n\n\ndef go():\n    return helper([]) + str(other())\n",
    "pkg/c.py": "import pkg.util\n\n\ndef via_module():\n    return pkg.util.helper(['q'])\n",
    "tests/__init__.py": "",
    "tests/test_app.py": (
        "import os\n\nfrom pkg.a import run\n\n\ndef test_run():\n    assert run() == 'x' + os.sep + 'y'\n"
    ),
}


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _index(root: Path) -> tuple[_StatefulIngestor, GraphUpdater]:
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)
    return store, updater


def _materialise(temp_repo: Path, fixture: dict[str, str]) -> Path:
    root = temp_repo / PROJECT
    root.mkdir()
    for rel, text in fixture.items():
        _write(root, rel, text)
    return root


@pytest.fixture
def repo(temp_repo: Path) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    root = _materialise(temp_repo, FIXTURE)
    store, updater = _index(root)
    return root, store, updater


def _qn(rel: str) -> str:
    return f"{PROJECT}.{rel}"


def _smoke(root: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=root,
        check=True,
        capture_output=True,
    )


def test_move_updates_three_importers_without_a_cycle(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = repo
    report = move(
        root,
        store.fetch_all,
        PROJECT,
        _qn("pkg.util.helper"),
        "pkg.core",
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.new_qualified_name == _qn("pkg.core.helper")
    core = (root / "pkg/core.py").read_text()
    # The definition travelled with its comment, docstring and the one
    # import it needs; the unrelated import stayed behind.
    assert core == (
        "import os\n\n\n"
        "# joins with the platform separator\n"
        'def helper(a):\n    """Join."""\n    return os.sep.join(a)\n'
    )
    util = (root / "pkg/util.py").read_text()
    assert "def helper" not in util and "def other" in util and "def unrelated" in util
    assert "from pathlib import Path" in util
    assert (root / "pkg/a.py").read_text().startswith("from pkg.core import helper\n")
    assert (
        (root / "pkg/b.py")
        .read_text()
        .startswith("from pkg.util import other\nfrom pkg.core import helper\n")
    )
    assert set(report.importers) == {"pkg/a.py", "pkg/b.py"}
    # `pkg.util.helper(...)` through a module import is not an import of
    # the symbol: it is left alone and named.
    assert report.unchanged_importers == ()
    assert report.copied_imports == ("import os",)
    assert report.verdict is not None and report.verdict.ok
    assert [t["qualified_name"] for t in report.verdict.affected_tests] == [
        _qn("tests.test_app.test_run")
    ]
    _smoke(root)


def test_keep_alias_leaves_a_working_re_export(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = repo
    report = move(
        root,
        store.fetch_all,
        PROJECT,
        _qn("pkg.util.helper"),
        "pkg/core.py",
        keep_alias=True,
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    util = (root / "pkg/util.py").read_text()
    assert "from pkg.core import helper  # noqa: F401" in util
    # The old import path still works, and so does the module-attribute use.
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pkg.util import helper; import pkg.c; print(helper(['a', 'b']), pkg.c.via_module())",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        encoding=cs.ENCODING_UTF8,
    )
    assert "q" in probe.stdout
    _smoke(root)


def test_move_that_would_create_a_cycle_is_refused_before_writing(
    temp_repo: Path,
) -> None:
    fixture = dict(FIXTURE)
    # helper needs `other` from util, and util's own code still calls helper:
    # after the move util imports core and core imports util.
    fixture["pkg/util.py"] = (
        "import os\n\n\n"
        "def other():\n    return 'o'\n\n\n"
        "def helper(a):\n    return other() + os.sep.join(a)\n\n\n"
        "def run():\n    return helper(['x'])\n"
    )
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    before = {rel: (root / rel).read_text() for rel in fixture}
    with pytest.raises(MoveRefused) as excinfo:
        move(
            root,
            store.fetch_all,
            PROJECT,
            _qn("pkg.util.helper"),
            "pkg.core",
            reingest=updater.reingest,
        )
    assert excinfo.value.cycle == (_qn("pkg.core"), _qn("pkg.util"))
    assert "import cycle" in str(excinfo.value)
    for rel, text in before.items():
        assert (root / rel).read_text() == text
    assert not (root / "pkg/core.py").exists()
    assert load_history(root) == []


def test_move_into_an_existing_module_appends(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = repo
    _write(root, "pkg/core.py", "VERSION = 1\n")
    updater.reingest(["pkg/core.py"])
    report = move(
        root,
        store.fetch_all,
        PROJECT,
        _qn("pkg.util.helper"),
        "pkg.core",
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    core = (root / "pkg/core.py").read_text()
    assert core.startswith("VERSION = 1\n\n\nimport os\n\n\n# joins")
    _smoke(root)


def test_old_module_that_still_uses_the_name_imports_it(temp_repo: Path) -> None:
    fixture = dict(FIXTURE)
    fixture["pkg/util.py"] = (
        FIXTURE["pkg/util.py"] + "\n\ndef run():\n    return helper(['x'])\n"
    )
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    report = move(
        root,
        store.fetch_all,
        PROJECT,
        _qn("pkg.util.helper"),
        "pkg.core",
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    util = (root / "pkg/util.py").read_text()
    assert util.startswith(
        "import os\nfrom pathlib import Path\nfrom pkg.core import helper\n"
    )
    assert "def run():\n    return helper(['x'])" in util
    _smoke(root)


def test_refusals(repo: tuple[Path, _StatefulIngestor, GraphUpdater]) -> None:
    root, store, _updater = repo
    with pytest.raises(MoveRefused, match="No definition"):
        move(
            root,
            store.fetch_all,
            PROJECT,
            _qn("pkg.util.nothing"),
            "pkg.core",
            dry_run=True,
        )
    with pytest.raises(MoveRefused, match="already holds"):
        move(
            root,
            store.fetch_all,
            PROJECT,
            _qn("pkg.util.helper"),
            "pkg.util",
            dry_run=True,
        )
    report = move(
        root, store.fetch_all, PROJECT, _qn("pkg.util.helper"), "pkg.core", dry_run=True
    )
    assert not report.applied and report.new_path == "pkg/core.py"
    assert (root / "pkg/util.py").read_text() == FIXTURE["pkg/util.py"]


async def test_mcp_move_tool_reports_and_refuses(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    from unittest.mock import MagicMock

    from codebase_rag.mcp.tools import MCPToolsRegistry

    root, store, updater = repo
    ingestor = MagicMock()
    ingestor.fetch_all = store.fetch_all
    ingestor.list_projects.return_value = [PROJECT]
    registry = MCPToolsRegistry(
        project_root=str(root), ingestor=ingestor, cypher_gen=MagicMock()
    )
    registry._live_updater = updater
    schema = next(
        s for s in registry.get_tool_schemas() if s.name == cs.MCPToolName.MOVE
    )
    assert set(schema.inputSchema["required"]) == {
        cs.MCPParamName.QUALIFIED_NAME,
        cs.MCPParamName.TARGET_MODULE,
    }
    refused = await registry.move(
        qualified_name=_qn("pkg.util.helper"), target_module="pkg.util", project=PROJECT
    )
    assert isinstance(refused, dict) and cs.DICT_KEY_ERROR in refused
    payload = await registry.move(
        qualified_name=_qn("pkg.util.helper"), target_module="pkg.core", project=PROJECT
    )
    assert isinstance(payload, dict)
    assert payload["applied"] is True
    assert payload["new_qualified_name"] == _qn("pkg.core.helper")
    assert payload[cs.KEY_VERDICT]["ok"] is True
