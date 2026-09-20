from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from move_helpers import (
    FIXTURE,
    PROJECT,
    _move_repo,  # noqa: F401
    index_repo,
    materialise,
    qn,
    smoke,
    write_file,
)

from codebase_rag import constants as cs
from codebase_rag.editing import MoveRefused, move
from codebase_rag.editing.transaction import load_history
from codebase_rag.graph_updater import GraphUpdater
from evals.cgr_graph import _StatefulIngestor


def test_move_updates_three_importers_without_a_cycle(
    move_repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = move_repo
    report = move(
        root,
        store.fetch_all,
        PROJECT,
        qn("pkg.util.helper"),
        "pkg.core",
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.new_qualified_name == qn("pkg.core.helper")
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
        qn("tests.test_app.test_run")
    ]
    smoke(root)


def test_keep_alias_leaves_a_working_re_export(
    move_repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = move_repo
    report = move(
        root,
        store.fetch_all,
        PROJECT,
        qn("pkg.util.helper"),
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
    smoke(root)


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
    root = materialise(temp_repo, fixture)
    store, updater = index_repo(root)
    before = {rel: (root / rel).read_text() for rel in fixture}
    with pytest.raises(MoveRefused) as excinfo:
        move(
            root,
            store.fetch_all,
            PROJECT,
            qn("pkg.util.helper"),
            "pkg.core",
            reingest=updater.reingest,
        )
    assert excinfo.value.cycle == (qn("pkg.core"), qn("pkg.util"))
    assert "import cycle" in str(excinfo.value)
    for rel, text in before.items():
        assert (root / rel).read_text() == text
    assert not (root / "pkg/core.py").exists()
    assert load_history(root) == []


def test_move_into_an_existing_module_appends(
    move_repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = move_repo
    write_file(root, "pkg/core.py", "VERSION = 1\n")
    updater.reingest(["pkg/core.py"])
    report = move(
        root,
        store.fetch_all,
        PROJECT,
        qn("pkg.util.helper"),
        "pkg.core",
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    core = (root / "pkg/core.py").read_text()
    assert core.startswith("VERSION = 1\n\n\nimport os\n\n\n# joins")
    smoke(root)


def test_old_module_that_still_uses_the_name_imports_it(temp_repo: Path) -> None:
    fixture = dict(FIXTURE)
    fixture["pkg/util.py"] = (
        FIXTURE["pkg/util.py"] + "\n\ndef run():\n    return helper(['x'])\n"
    )
    root = materialise(temp_repo, fixture)
    store, updater = index_repo(root)
    report = move(
        root,
        store.fetch_all,
        PROJECT,
        qn("pkg.util.helper"),
        "pkg.core",
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    util = (root / "pkg/util.py").read_text()
    assert util.startswith(
        "import os\nfrom pathlib import Path\nfrom pkg.core import helper\n"
    )
    assert "def run():\n    return helper(['x'])" in util
    smoke(root)


def test_refusals(move_repo: tuple[Path, _StatefulIngestor, GraphUpdater]) -> None:
    root, store, _updater = move_repo
    with pytest.raises(MoveRefused, match="No definition"):
        move(
            root,
            store.fetch_all,
            PROJECT,
            qn("pkg.util.nothing"),
            "pkg.core",
            dry_run=True,
        )
    with pytest.raises(MoveRefused, match="already holds"):
        move(
            root,
            store.fetch_all,
            PROJECT,
            qn("pkg.util.helper"),
            "pkg.util",
            dry_run=True,
        )
    report = move(
        root, store.fetch_all, PROJECT, qn("pkg.util.helper"), "pkg.core", dry_run=True
    )
    assert not report.applied and report.new_path == "pkg/core.py"
    assert (root / "pkg/util.py").read_text() == FIXTURE["pkg/util.py"]
