"""`cgr check` reports the importers of a deleted or moved module (#3266).

`dangling_importers` only looked for imports naming a removed SYMBOL, so a
statement importing the module itself -- `import pkg.signals`, `from pkg
import util`, an ES `export * from "./util"` or `import * as u` -- was never
listed when the module went away: the file showed up in `removed_files`,
nothing else did, and `--fail-on-found` passed an edit Python fails with
ModuleNotFoundError and tsc with TS2307.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner, Result

from codebase_rag import constants as cs
from codebase_rag.cli import app
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_delta import (
    DanglingImporter,
    StructuralDelta,
    has_findings,
    observe,
)
from codebase_rag.types_defs import PropertyParams, ResultRow
from evals.cgr_graph import _StatefulIngestor

PROJECT = "modimp"

SIGNALS = "def on_save(obj):\n    return obj\n"
APPS = "import pkg.signals  # registers the handlers\n\nREADY = True\n"
UTIL = "def title(s):\n    return s.title()\n"
CONF = (
    "from pkg import util\n"
    "import pkg.util\n"
    "from pkg.util import title as T\n"
    "import pkg.util as pu\n"
    "\nNAME = util.__name__\n"
)
TS_UTIL = "export function up(s: string): string { return s.toUpperCase(); }\n"

PY_FILES = {
    "pkg/__init__.py": "",
    "pkg/signals.py": SIGNALS,
    "pkg/apps.py": APPS,
    "pkg/util.py": UTIL,
    "pkg/conf.py": CONF,
}
# The barrel and the namespace importer sit outside `src/`, which holds no
# `index.ts`: `./util` names `src/util.ts` and nothing else.
TS_FILES = {
    "src/util.ts": TS_UTIL,
    "barrel/index.ts": 'export * from "../src/util";\n',
    "app/ns.ts": 'import * as u from "../src/util";\nexport const v = u.up("a");\n',
}


def _qn(dotted: str) -> str:
    return f"{PROJECT}.{dotted}"


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _move(root: Path, old: str, new: str) -> None:
    (root / new).parent.mkdir(parents=True, exist_ok=True)
    (root / old).rename(root / new)


def _index(root: Path) -> tuple[_StatefulIngestor, GraphUpdater]:
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)
    return store, updater


def _observe(
    root: Path,
    store: _StatefulIngestor,
    updater: GraphUpdater,
    changed: list[str],
    deleted: list[str],
) -> StructuralDelta:
    def fetch_all(query: str, params: PropertyParams | None) -> list[ResultRow]:
        return store.fetch_all(query, None if params is None else dict(params))

    return observe(
        fetch_all,
        PROJECT,
        [*changed, *deleted],
        lambda: updater.reingest(changed, deleted=deleted),
        repo_root=root,
    )


def _entries(delta: StructuralDelta) -> list[tuple[str, int | None, str, str, str]]:
    # (path, line, kind, name, target) of every dangling importer.
    return [
        (
            e["path"],
            e["line"],
            e["kind"],
            e["name"],
            e["target"].removeprefix(f"{PROJECT}."),
        )
        for e in delta["dangling_importers"]
    ]


def _module_entry(
    importer: str,
    path: str,
    line: int,
    name: str,
    target: str,
    renamed_to: str | None = None,
) -> DanglingImporter:
    return DanglingImporter(
        importer=_qn(importer),
        path=path,
        line=line,
        col=0,
        kind=cs.DanglingImportKind.MODULE,
        name=name,
        target=_qn(target),
        renamed_to=_qn(renamed_to) if renamed_to else None,
    )


@pytest.fixture
def py_root(temp_repo: Path) -> Path:
    root = temp_repo / PROJECT
    _write(root, PY_FILES)
    return root


@pytest.fixture
def ts_root(temp_repo: Path) -> Path:
    root = temp_repo / PROJECT
    _write(root, TS_FILES)
    return root


# --- the bug ------------------------------------------------------------------


def test_an_import_of_a_deleted_python_module_is_a_finding(py_root: Path) -> None:
    store, updater = _index(py_root)
    (py_root / "pkg/signals.py").unlink()

    delta = _observe(py_root, store, updater, [], deleted=["pkg/signals.py"])

    assert delta["dangling_importers"] == [
        _module_entry("pkg.apps", "pkg/apps.py", 1, "pkg", "pkg.signals")
    ]
    assert has_findings(delta)


def test_every_import_of_a_moved_python_module_is_a_finding(py_root: Path) -> None:
    # `from pkg import util`, `import pkg.util` and `import pkg.util as pu`
    # name the module; `from pkg.util import title` names a symbol in it and
    # keeps its own `import` entry, once.
    store, updater = _index(py_root)
    _move(py_root, "pkg/util.py", "pkg/text/util.py")

    delta = _observe(
        py_root, store, updater, ["pkg/text/util.py"], deleted=["pkg/util.py"]
    )

    assert _entries(delta) == [
        ("pkg/conf.py", 1, "module", "util", "pkg.util"),
        ("pkg/conf.py", 2, "module", "pkg", "pkg.util"),
        ("pkg/conf.py", 3, "import", "title", "pkg.util.title"),
        ("pkg/conf.py", 4, "module", "pu", "pkg.util"),
    ]
    renamed = {e["line"]: e["renamed_to"] for e in delta["dangling_importers"]}
    assert renamed == {
        1: _qn("pkg.text.util"),
        2: _qn("pkg.text.util"),
        3: _qn("pkg.text.util.title"),
        4: _qn("pkg.text.util"),
    }
    assert has_findings(delta)


def test_a_moved_module_that_defines_nothing_is_paired_by_its_file_name(
    py_root: Path,
) -> None:
    # No definition moves with a module of top-level statements only, so
    # the rename pass has nothing to pair; the one new module with the same
    # file name is where it went.
    _write(py_root, {"pkg/signals.py": "print('registered')\n"})
    store, updater = _index(py_root)
    _move(py_root, "pkg/signals.py", "pkg/hooks/signals.py")

    delta = _observe(
        py_root, store, updater, ["pkg/hooks/signals.py"], deleted=["pkg/signals.py"]
    )

    assert delta["dangling_importers"] == [
        _module_entry(
            "pkg.apps", "pkg/apps.py", 1, "pkg", "pkg.signals", "pkg.hooks.signals"
        )
    ]


def test_a_module_moved_under_a_new_file_name_is_paired_by_its_definitions(
    py_root: Path,
) -> None:
    # `pkg/util.py` -> `pkg/strings.py`: no file name to go by, but `title`
    # moved with it, so the rename pass names the new module.
    store, updater = _index(py_root)
    _move(py_root, "pkg/util.py", "pkg/strings.py")

    delta = _observe(
        py_root, store, updater, ["pkg/strings.py"], deleted=["pkg/util.py"]
    )

    renamed = {
        e["line"]: e["renamed_to"]
        for e in delta["dangling_importers"]
        if e["kind"] == "module"
    }
    assert renamed == {
        1: _qn("pkg.strings"),
        2: _qn("pkg.strings"),
        4: _qn("pkg.strings"),
    }


def test_two_new_modules_with_its_file_name_leave_the_destination_open(
    py_root: Path,
) -> None:
    # Negative: the file name pairs a module only when one new module has it.
    _write(py_root, {"pkg/signals.py": "print('registered')\n"})
    store, updater = _index(py_root)
    _move(py_root, "pkg/signals.py", "pkg/hooks/signals.py")
    _write(py_root, {"pkg/other/signals.py": "print('other')\n"})

    delta = _observe(
        py_root,
        store,
        updater,
        ["pkg/hooks/signals.py", "pkg/other/signals.py"],
        deleted=["pkg/signals.py"],
    )

    assert delta["dangling_importers"] == [
        _module_entry("pkg.apps", "pkg/apps.py", 1, "pkg", "pkg.signals")
    ]


@pytest.mark.parametrize(
    ("importer", "path", "name"),
    [("barrel.index", "barrel/index.ts", "*"), ("app.ns", "app/ns.ts", "u")],
    ids=["export-star", "namespace-import"],
)
def test_an_import_of_a_deleted_typescript_module_is_a_finding(
    ts_root: Path, importer: str, path: str, name: str
) -> None:
    store, updater = _index(ts_root)
    (ts_root / "src/util.ts").unlink()

    delta = _observe(ts_root, store, updater, [], deleted=["src/util.ts"])

    assert (
        _module_entry(importer, path, 1, name, "src.util")
        in delta["dangling_importers"]
    ), delta["dangling_importers"]
    assert has_findings(delta)


def test_check_fail_on_found_exits_nonzero_for_a_deleted_imported_module(
    py_root: Path,
) -> None:
    store, _updater = _index(py_root)
    _commit_base(py_root)
    subprocess.run(["git", "rm", "-q", "pkg/signals.py"], cwd=py_root, check=True)

    result = _run_cli_check(py_root, store)

    assert result.exit_code == 1, result.output
    assert '"kind": "module"' in result.output
    assert _qn("pkg.signals") in result.output


# --- negatives ----------------------------------------------------------------


def test_a_move_that_updates_every_importer_passes(py_root: Path) -> None:
    store, updater = _index(py_root)
    _move(py_root, "pkg/signals.py", "pkg/hooks/signals.py")
    _write(py_root, {"pkg/apps.py": APPS.replace("pkg.signals", "pkg.hooks.signals")})

    delta = _observe(
        py_root,
        store,
        updater,
        ["pkg/hooks/signals.py", "pkg/apps.py"],
        deleted=["pkg/signals.py"],
    )

    assert delta["dangling_importers"] == []
    assert not has_findings(delta)


def test_an_importer_dropping_the_import_in_the_same_edit_passes(
    py_root: Path,
) -> None:
    store, updater = _index(py_root)
    (py_root / "pkg/signals.py").unlink()
    _write(py_root, {"pkg/apps.py": "READY = True\n"})

    delta = _observe(
        py_root, store, updater, ["pkg/apps.py"], deleted=["pkg/signals.py"]
    )

    assert delta["dangling_importers"] == []


def test_deleting_a_module_nothing_imports_passes(py_root: Path) -> None:
    _write(py_root, {"pkg/unused.py": "def spare():\n    return 0\n"})
    store, updater = _index(py_root)
    (py_root / "pkg/unused.py").unlink()

    delta = _observe(py_root, store, updater, [], deleted=["pkg/unused.py"])

    assert delta["dangling_importers"] == []
    assert not has_findings(delta)


def test_a_python_module_turned_into_a_package_passes(py_root: Path) -> None:
    # `pkg/util.py` -> `pkg/util/__init__.py`: `import pkg.util` still loads,
    # and the package's module keeps the qualified name.
    store, updater = _index(py_root)
    _move(py_root, "pkg/util.py", "pkg/util/__init__.py")

    delta = _observe(
        py_root, store, updater, ["pkg/util/__init__.py"], deleted=["pkg/util.py"]
    )

    assert [e for e in delta["dangling_importers"] if e["kind"] == "module"] == []


def test_a_typescript_module_turned_into_a_directory_index_passes(
    ts_root: Path,
) -> None:
    # `src/util.ts` -> `src/util/index.ts`: `"../src/util"` resolves to the
    # directory's index, as Node and TypeScript resolve it.
    store, updater = _index(ts_root)
    _move(ts_root, "src/util.ts", "src/util/index.ts")

    delta = _observe(
        ts_root, store, updater, ["src/util/index.ts"], deleted=["src/util.ts"]
    )

    assert [e for e in delta["dangling_importers"] if e["kind"] == "module"] == []


def test_check_fail_on_found_passes_a_move_that_updates_its_importer(
    py_root: Path,
) -> None:
    store, _updater = _index(py_root)
    _commit_base(py_root)
    subprocess.run(
        ["git", "mv", "pkg/signals.py", "pkg/hooks_signals.py"],
        cwd=py_root,
        check=True,
    )
    _write(py_root, {"pkg/apps.py": APPS.replace("pkg.signals", "pkg.hooks_signals")})

    result = _run_cli_check(py_root, store)

    assert result.exit_code == 0, result.output


# --- helpers ------------------------------------------------------------------


def _commit_base(root: Path) -> None:
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _run_cli_check(root: Path, store: _StatefulIngestor) -> Result:
    # The store itself, so the re-ingest really rewrites the graph.
    context = MagicMock()
    context.__enter__.return_value = store
    context.__exit__.return_value = False
    with (
        patch("codebase_rag.cli.connect_memgraph", return_value=context),
        patch.object(store, "list_projects", create=True, return_value=[PROJECT]),
    ):
        return CliRunner().invoke(
            app,
            [
                "check",
                "--repo-path",
                str(root),
                "--project",
                PROJECT,
                "--fail-on-found",
            ],
        )
