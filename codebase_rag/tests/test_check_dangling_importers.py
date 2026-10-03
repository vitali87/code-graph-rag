# `cgr check --fail-on-found` passed an edit that deleted a symbol a package
# `__init__.py` still re-exported (issue #2516): no call site went with the
# import, so `dangling_callers` was empty, and `stale_importers` only covers
# moves that empty a module. The IMPORTS edge already carried
# `imported_name`; these tests pin that the delta now reports every import
# statement and `__all__` entry naming a removed or renamed symbol, and that
# the gate counts them -- while an unrelated binding of the same name, a
# removal nothing imports and an importer fixed in the same edit stay quiet.

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import Result
from typer.testing import CliRunner

from codebase_rag.cli import app
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_delta import StructuralDelta, has_findings, observe
from evals.cgr_graph import _StatefulIngestor

PROJECT = "reexport_fixture"

INIT = 'from app.core import helper, keep\n\n__all__ = ["helper", "keep"]\n'
CORE = (
    "def helper():\n"
    "    return 1\n"
    "\n\n"
    "def keep(items):\n"
    "    return [item for item in items if item]\n"
    "\n\n"
    "def unused(a, b):\n"
    "    return {a: b}\n"
)
CORE_WITHOUT_HELPER = CORE.replace("def helper():\n    return 1\n\n\n", "")
# A second `helper` in another module, imported and re-exported by name: the
# same NAME as the removed symbol but a different symbol, so it must never
# be read as an importer of `app.core.helper`.
OTHER = (
    "def helper(value):\n    while value > 1:\n        value //= 2\n    return value\n"
)
CONSUMER = (
    'from app.other import helper\n\n__all__ = ["helper"]\n\n\n'
    "def use():\n    return helper(8)\n"
)

FIXTURE: dict[str, str] = {
    "app/__init__.py": INIT,
    "app/core.py": CORE,
    "app/other.py": OTHER,
    "app/consumer.py": CONSUMER,
}


def _qn(dotted: str) -> str:
    return f"{PROJECT}.{dotted}"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def indexed(temp_repo: Path) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    root = temp_repo / PROJECT
    root.mkdir()
    for rel, text in FIXTURE.items():
        _write(root, rel, text)
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
    return root, store, updater


def _observe(
    root: Path,
    store: _StatefulIngestor,
    updater: GraphUpdater,
    changed: list[str],
    deleted: list[str] | None = None,
) -> StructuralDelta:
    return observe(
        store.fetch_all,
        PROJECT,
        [*changed, *(deleted or [])],
        lambda: updater.reingest(changed, deleted=deleted or []),
        repo_root=root,
    )


def _import_entry(
    target: str, renamed_to: str | None = None, name: str = "helper"
) -> dict[str, object]:
    return {
        "importer": _qn("app"),
        "path": "app/__init__.py",
        "line": 1,
        "col": 0,
        "kind": "import",
        "name": name,
        "target": _qn(target),
        "renamed_to": _qn(renamed_to) if renamed_to else None,
    }


def _all_entry(
    target: str, renamed_to: str | None = None, name: str = "helper"
) -> dict[str, object]:
    # `__all__ = ["helper", "keep"]`: the name starts after `__all__ = ["`.
    return {
        "importer": _qn("app"),
        "path": "app/__init__.py",
        "line": 3,
        "col": 12,
        "kind": "__all__",
        "name": name,
        "target": _qn(target),
        "renamed_to": _qn(renamed_to) if renamed_to else None,
    }


# --- the bug ------------------------------------------------------------------


def test_removed_symbol_still_re_exported_by_the_package_is_a_finding(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(root, "app/core.py", CORE_WITHOUT_HELPER)

    delta = _observe(root, store, updater, ["app/core.py"])

    assert delta["symbols"]["removed"] == [_qn("app.core.helper")]
    # No call site goes with the import, which is why the gate stayed green.
    assert delta["dangling_callers"] == []
    assert has_findings(delta)
    assert delta["dangling_importers"] == [
        _import_entry("app.core.helper"),
        _all_entry("app.core.helper"),
    ]


def test_renamed_symbol_still_imported_under_its_old_name_is_a_finding(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(root, "app/core.py", CORE.replace("def helper():", "def assist():"))

    delta = _observe(root, store, updater, ["app/core.py"])

    assert delta["symbols"]["renamed"] == [
        {
            "old": _qn("app.core.helper"),
            "new": _qn("app.core.assist"),
            "path": "app/core.py",
        }
    ]
    assert has_findings(delta)
    assert delta["dangling_importers"] == [
        _import_entry("app.core.helper", renamed_to="app.core.assist"),
        _all_entry("app.core.helper", renamed_to="app.core.assist"),
    ]


def test_all_entry_left_in_the_defining_module_is_a_finding(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    # `unused` is imported by no one, but the module's own `__all__` still
    # lists it, so `from app.core import *` raises AttributeError.
    _write(root, "app/core.py", CORE + '\n\n__all__ = ["keep", "unused"]\n')
    _write(root, "app/__init__.py", "from app.core import keep\n")
    _observe(root, store, updater, ["app/core.py", "app/__init__.py"])
    _write(
        root,
        "app/core.py",
        CORE.replace("def unused(a, b):\n    return {a: b}\n", "")
        + '\n\n__all__ = ["keep", "unused"]\n',
    )

    delta = _observe(root, store, updater, ["app/core.py"])

    assert delta["symbols"]["removed"] == [_qn("app.core.unused")]
    assert has_findings(delta)
    (entry,) = delta["dangling_importers"]
    assert entry["kind"] == "__all__"
    assert entry["importer"] == _qn("app.core")
    assert entry["path"] == "app/core.py"
    assert entry["name"] == "unused"
    assert entry["target"] == _qn("app.core.unused")
    line = entry["line"]
    assert line is not None
    text = (root / "app/core.py").read_text().splitlines()[line - 1]
    assert text.startswith("__all__")
    assert text[entry["col"] or 0 :].startswith('unused"')


def test_import_from_a_deleted_module_is_a_finding(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    (root / "app/core.py").unlink()

    delta = _observe(root, store, updater, [], deleted=["app/core.py"])

    assert has_findings(delta)
    kinds = {(e["kind"], e["name"], e["target"]) for e in delta["dangling_importers"]}
    assert ("import", "helper", _qn("app.core.helper")) in kinds
    assert ("import", "keep", _qn("app.core.keep")) in kinds
    assert all(e["path"] == "app/__init__.py" for e in delta["dangling_importers"])


def test_check_fail_on_found_exits_nonzero_for_a_removed_re_export(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = indexed
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b")
    _write(root, "app/core.py", CORE_WITHOUT_HELPER)

    result = _run_cli_check(root, store)

    assert result.exit_code == 1, result.output
    assert '"dangling_importers"' in result.output
    assert _qn("app.core.helper") in result.output


def test_typescript_barrel_re_export_of_a_removed_function_is_a_finding(
    temp_repo: Path,
) -> None:
    # The IMPORTS edge carries `imported_name` for every language, so an ES
    # barrel breaks the same way a package `__init__` does.
    root = temp_repo / PROJECT
    core = (
        "export function helper(): number {\n  return 1;\n}\n\n"
        "export function keep(): number {\n  return 2;\n}\n"
    )
    _write(root, "src/core.ts", core)
    _write(root, "src/barrel.ts", "export { helper, keep } from './core';\n")
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
    _write(root, "src/core.ts", core.split("\n\n", 1)[1])

    delta = _observe(root, store, updater, ["src/core.ts"])

    assert delta["symbols"]["removed"] == [_qn("src.core.helper")]
    assert has_findings(delta)
    assert delta["dangling_importers"] == [
        {
            "importer": _qn("src.barrel"),
            "path": "src/barrel.ts",
            "line": 1,
            "col": 0,
            "kind": "import",
            "name": "helper",
            "target": _qn("src.core.helper"),
            "renamed_to": None,
        }
    ]


# A replacement import is only as good as its target (Greptile, PR #2574):
# `binds` must look through an import into a module the edit did not touch,
# both to reject one naming nothing and to accept a wildcard that works.


def _add_unchanged_module(
    root: Path, store: _StatefulIngestor, updater: GraphUpdater, rel: str, text: str
) -> None:
    # Indexed in its own step, so the edit under test leaves it alone and
    # its definitions are absent from that edit's snapshots.
    _write(root, rel, text)
    _observe(root, store, updater, [rel])


def _commit_base(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b")


def _imports_cleanly(root: Path) -> bool:
    # The ground truth the gate must agree with: does the package load?
    result = subprocess.run(
        [sys.executable, "-c", "from app import helper"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def test_re_export_from_an_unchanged_module_lacking_the_name_is_a_finding(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _add_unchanged_module(root, store, updater, "app/empty.py", "VALUE = 1\n")
    _write(
        root, "app/core.py", "from app.empty import helper\n\n\n" + CORE_WITHOUT_HELPER
    )

    delta = _observe(root, store, updater, ["app/core.py"])

    assert not _imports_cleanly(root)
    assert delta["symbols"]["removed"] == [_qn("app.core.helper")]
    assert has_findings(delta)
    assert delta["dangling_importers"] == [
        _import_entry("app.core.helper"),
        _all_entry("app.core.helper"),
    ]


def test_check_fail_on_found_exits_nonzero_for_a_re_export_naming_nothing(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _add_unchanged_module(root, store, updater, "app/empty.py", "VALUE = 1\n")
    _commit_base(root)
    _write(
        root, "app/core.py", "from app.empty import helper\n\n\n" + CORE_WITHOUT_HELPER
    )

    result = _run_cli_check(root, store)

    assert result.exit_code == 1, result.output
    assert _qn("app.core.helper") in result.output


def test_wildcard_re_export_from_an_unchanged_module_defining_it_passes(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _add_unchanged_module(
        root, store, updater, "app/util.py", "def helper():\n    return 1\n"
    )
    _write(root, "app/core.py", "from app.util import *\n\n\n" + CORE_WITHOUT_HELPER)

    delta = _observe(root, store, updater, ["app/core.py"])

    assert _imports_cleanly(root)
    assert delta["symbols"]["removed"] == [_qn("app.core.helper")]
    assert delta["dangling_importers"] == []
    assert not has_findings(delta)


def test_check_fail_on_found_passes_a_working_wildcard_re_export(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _add_unchanged_module(
        root, store, updater, "app/util.py", "def helper():\n    return 1\n"
    )
    _commit_base(root)
    _write(root, "app/core.py", "from app.util import *\n\n\n" + CORE_WITHOUT_HELPER)

    result = _run_cli_check(root, store)

    assert result.exit_code == 0, result.output


def test_name_in_a_comment_inside_all_is_not_an_export(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(root, "app/core.py", CORE_WITHOUT_HELPER)
    _write(
        root,
        "app/__init__.py",
        "from app.core import keep\n\n"
        "__all__ = [\n    \"keep\",\n    # 'helper' was removed\n]\n",
    )

    delta = _observe(root, store, updater, ["app/core.py", "app/__init__.py"])

    assert delta["symbols"]["removed"] == [_qn("app.core.helper")]
    assert delta["dangling_importers"] == []
    assert not has_findings(delta)


def test_comprehension_variable_named_like_the_removed_symbol_binds_nothing(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    # A comprehension's variable is local to it in Python 3: the module still
    # has no `helper` (Greptile, PR #2574).
    root, store, updater = indexed
    _write(
        root,
        "app/core.py",
        CORE_WITHOUT_HELPER + "\n\nvalues = [helper for helper in range(3)]\n",
    )

    delta = _observe(root, store, updater, ["app/core.py"])

    assert not _imports_cleanly(root)
    assert delta["dangling_importers"] == [
        _import_entry("app.core.helper"),
        _all_entry("app.core.helper"),
    ]


def test_wildcard_of_a_module_whose_all_leaves_the_name_out_is_a_finding(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    # `import *` takes only the names in `__all__` (Greptile, PR #2574).
    root, store, updater = indexed
    _add_unchanged_module(
        root,
        store,
        updater,
        "app/util.py",
        "def helper():\n    return 1\n\n\ndef other():\n    return 2\n\n\n"
        '__all__ = ["other"]\n',
    )
    _write(root, "app/core.py", "from app.util import *\n\n\n" + CORE_WITHOUT_HELPER)

    delta = _observe(root, store, updater, ["app/core.py"])

    assert not _imports_cleanly(root)
    assert delta["dangling_importers"] == [
        _import_entry("app.core.helper"),
        _all_entry("app.core.helper"),
    ]


def test_wildcard_skips_an_underscore_name_of_a_module_without_all(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    # With no `__all__`, `import *` takes the names that do not start with
    # an underscore (Greptile, PR #2574).
    root, store, updater = indexed
    _write(root, "app/core.py", CORE.replace("def helper():", "def _helper():"))
    _write(root, "app/__init__.py", "from app.core import _helper, keep\n")
    _observe(root, store, updater, ["app/core.py", "app/__init__.py"])
    _add_unchanged_module(
        root, store, updater, "app/util.py", "def _helper():\n    return 1\n"
    )
    _write(root, "app/core.py", "from app.util import *\n\n\n" + CORE_WITHOUT_HELPER)

    delta = _observe(root, store, updater, ["app/core.py"])

    assert delta["symbols"]["removed"] == [_qn("app.core._helper")]
    assert [(e["kind"], e["name"]) for e in delta["dangling_importers"]] == [
        ("import", "_helper")
    ]


def test_wildcard_of_a_module_that_reassigns_all_without_the_name_is_a_finding(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    # A later `__all__ = [...]` replaces the list before it, so `import *`
    # takes only `other` (Greptile, PR #2574).
    root, store, updater = indexed
    _add_unchanged_module(
        root,
        store,
        updater,
        "app/util.py",
        "def helper():\n    return 1\n\n\ndef other():\n    return 2\n\n\n"
        '__all__ = ["helper"]\n__all__ = ["other"]\n',
    )
    _write(root, "app/core.py", "from app.util import *\n\n\n" + CORE_WITHOUT_HELPER)

    delta = _observe(root, store, updater, ["app/core.py"])

    assert not _imports_cleanly(root)
    assert delta["dangling_importers"] == [
        _import_entry("app.core.helper"),
        _all_entry("app.core.helper"),
    ]


def test_walrus_in_a_lambda_default_binds_the_module_name(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    # A lambda's defaults run in the enclosing scope when it is created;
    # only its body is a scope of its own (Greptile, PR #2574).
    root, store, updater = indexed
    _write(
        root,
        "app/core.py",
        CORE_WITHOUT_HELPER + "\n\nfactory = lambda x=(helper := keep): x\n",
    )

    delta = _observe(root, store, updater, ["app/core.py"])

    assert _imports_cleanly(root)
    assert delta["dangling_importers"] == []


# --- what must not change -----------------------------------------------------


def test_a_re_export_cycle_binds_nothing_and_terminates() -> None:
    # `a` and `b` each import `helper` from the other and neither defines
    # it: following the chain must stop, and the name is not bound.
    from codebase_rag.structural_delta import ImportBinding, Snapshot, _AfterBindings

    def binding(importer: str, module: str) -> ImportBinding:
        return ImportBinding(
            importer=importer,
            importer_path=f"{importer}.py",
            module=module,
            imported_name="helper",
            bound="helper",
            line=1,
            col=0,
            module_path=f"{module}.py",
        )

    after = Snapshot(
        paths=frozenset({"a.py", "b.py"}),
        definitions={},
        callees={},
        sites=(),
        imports={},
        module_paths={},
        bindings=(binding("a", "b"), binding("b", "a")),
    )

    still = _AfterBindings(
        after,
        gone={"a.helper", "b.helper"},
        load=lambda _path: (frozenset(), ()),
        repo_root=None,
    )

    assert not still.binds("a", "a.py", "helper")
    assert not still.binds("b", "b.py", "helper")


def test_removing_a_symbol_nothing_imports_or_calls_passes(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(
        root, "app/core.py", CORE.replace("def unused(a, b):\n    return {a: b}\n", "")
    )

    delta = _observe(root, store, updater, ["app/core.py"])

    assert delta["symbols"]["removed"] == [_qn("app.core.unused")]
    assert delta["dangling_importers"] == []
    assert not has_findings(delta)


def test_check_fail_on_found_still_passes_a_removal_nothing_imports(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = indexed
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b")
    _write(
        root, "app/core.py", CORE.replace("def unused(a, b):\n    return {a: b}\n", "")
    )

    result = _run_cli_check(root, store)

    assert result.exit_code == 0, result.output


def test_same_named_import_from_another_module_is_not_reported(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(root, "app/core.py", CORE_WITHOUT_HELPER)

    delta = _observe(root, store, updater, ["app/core.py"])

    # `app.consumer` imports and re-exports a `helper` too, from
    # `app.other`: only the package that named `app.core.helper` is listed.
    assert {e["importer"] for e in delta["dangling_importers"]} == {_qn("app")}
    assert all(e["path"] != "app/consumer.py" for e in delta["dangling_importers"])


def test_importer_fixed_in_the_same_edit_is_not_reported(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(root, "app/core.py", CORE_WITHOUT_HELPER)
    _write(root, "app/__init__.py", 'from app.core import keep\n\n__all__ = ["keep"]\n')

    delta = _observe(root, store, updater, ["app/core.py", "app/__init__.py"])

    assert delta["symbols"]["removed"] == [_qn("app.core.helper")]
    assert delta["dangling_importers"] == []
    # The same-named `helper` that `app.consumer` still imports from
    # `app.other` must not trip the gate either.
    assert not has_findings(delta)


def test_moved_symbol_re_exported_from_its_old_module_is_not_reported(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    # The compatibility shim: `helper` now lives in `app.util`, and
    # `app.core` imports it back, so `from app.core import helper` works.
    _write(root, "app/util.py", "def helper():\n    return 1\n")
    _write(
        root, "app/core.py", "from app.util import helper\n\n\n" + CORE_WITHOUT_HELPER
    )

    delta = _observe(root, store, updater, ["app/core.py", "app/util.py"])

    assert delta["symbols"]["renamed"] == [
        {
            "old": _qn("app.core.helper"),
            "new": _qn("app.util.helper"),
            "path": "app/core.py",
        }
    ]
    assert delta["dangling_importers"] == []


def test_named_re_export_from_an_unchanged_module_defining_it_passes(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _add_unchanged_module(
        root, store, updater, "app/util.py", "def helper():\n    return 1\n"
    )
    _write(
        root, "app/core.py", "from app.util import helper\n\n\n" + CORE_WITHOUT_HELPER
    )

    delta = _observe(root, store, updater, ["app/core.py"])

    assert _imports_cleanly(root)
    assert delta["dangling_importers"] == []
    assert not has_findings(delta)


def test_re_export_of_a_name_an_unchanged_module_assigns_passes(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    # The graph records no node for a module-level assignment; the source
    # still binds the name, so the import works and must not be reported.
    _add_unchanged_module(root, store, updater, "app/util.py", "helper = int\n")
    _write(
        root, "app/core.py", "from app.util import helper\n\n\n" + CORE_WITHOUT_HELPER
    )

    delta = _observe(root, store, updater, ["app/core.py"])

    assert _imports_cleanly(root)
    assert delta["dangling_importers"] == []


def test_removed_definition_replaced_by_an_assignment_passes(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(root, "app/core.py", CORE_WITHOUT_HELPER + "\n\nhelper = keep\n")

    delta = _observe(root, store, updater, ["app/core.py"])

    assert _imports_cleanly(root)
    assert delta["symbols"]["removed"] == [_qn("app.core.helper")]
    assert delta["dangling_importers"] == []


def test_wildcard_re_export_from_an_unchanged_module_lacking_it_is_a_finding(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _add_unchanged_module(root, store, updater, "app/empty.py", "VALUE = 1\n")
    _write(root, "app/core.py", "from app.empty import *\n\n\n" + CORE_WITHOUT_HELPER)

    delta = _observe(root, store, updater, ["app/core.py"])

    assert not _imports_cleanly(root)
    assert delta["dangling_importers"] == [
        _import_entry("app.core.helper"),
        _all_entry("app.core.helper"),
    ]


def test_re_export_from_outside_the_project_is_taken_at_its_word(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(
        root,
        "app/core.py",
        "from os.path import basename as helper\n\n\n" + CORE_WITHOUT_HELPER,
    )

    delta = _observe(root, store, updater, ["app/core.py"])

    assert _imports_cleanly(root)
    assert delta["dangling_importers"] == []


def test_re_export_of_a_submodule_passes(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    # `from app import helpers` binds the submodule: its IMPORTS edge names
    # `app.helpers` itself, which defines no `helpers`.
    _add_unchanged_module(root, store, updater, "app/helpers.py", "VALUE = 1\n")
    _write(
        root,
        "app/core.py",
        "from app import helpers as helper\n\n\n" + CORE_WITHOUT_HELPER,
    )

    delta = _observe(root, store, updater, ["app/core.py"])

    assert _imports_cleanly(root)
    assert delta["dangling_importers"] == []


def test_all_entry_with_a_trailing_comment_is_still_an_export(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(
        root,
        "app/__init__.py",
        "from app.core import helper, keep\n\n"
        '__all__ = [\n    "helper",  # kept for callers\n    "keep",\n]\n',
    )
    _observe(root, store, updater, ["app/__init__.py"])
    _write(root, "app/core.py", CORE_WITHOUT_HELPER)

    delta = _observe(root, store, updater, ["app/core.py"])

    entries = [e for e in delta["dangling_importers"] if e["kind"] == "__all__"]
    assert [(e["line"], e["col"], e["name"]) for e in entries] == [(4, 5, "helper")]


def test_walrus_in_a_comprehension_binds_the_module_name(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    # The one comprehension target that binds in the enclosing scope.
    root, store, updater = indexed
    _write(
        root,
        "app/core.py",
        CORE_WITHOUT_HELPER + "\n\nvalues = [(helper := n) for n in range(3)]\n",
    )

    delta = _observe(root, store, updater, ["app/core.py"])

    assert _imports_cleanly(root)
    assert delta["dangling_importers"] == []


def test_wildcard_of_a_module_whose_all_lists_the_name_passes(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _add_unchanged_module(
        root,
        store,
        updater,
        "app/util.py",
        'def helper():\n    return 1\n\n\n__all__ = ["helper"]\n',
    )
    _write(root, "app/core.py", "from app.util import *\n\n\n" + CORE_WITHOUT_HELPER)

    delta = _observe(root, store, updater, ["app/core.py"])

    assert _imports_cleanly(root)
    assert delta["dangling_importers"] == []


def test_wildcard_of_a_module_that_extends_a_reassigned_all_with_the_name_passes(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _add_unchanged_module(
        root,
        store,
        updater,
        "app/util.py",
        "def helper():\n    return 1\n\n\ndef other():\n    return 2\n\n\n"
        '__all__ = ["unused"]\n__all__ = ["other"]\n__all__ += ["helper"]\n',
    )
    _write(root, "app/core.py", "from app.util import *\n\n\n" + CORE_WITHOUT_HELPER)

    delta = _observe(root, store, updater, ["app/core.py"])

    assert _imports_cleanly(root)
    assert delta["dangling_importers"] == []


def test_wildcard_of_a_module_that_reassigns_all_in_a_branch_is_taken_at_its_word(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    # A branch may not run, so the list before it may still be the one
    # `import *` reads.
    root, store, updater = indexed
    _add_unchanged_module(
        root,
        store,
        updater,
        "app/util.py",
        "def helper():\n    return 1\n\n\nFLAG = False\n"
        '__all__ = ["helper"]\nif FLAG:\n    __all__ = ["FLAG"]\n',
    )
    _write(root, "app/core.py", "from app.util import *\n\n\n" + CORE_WITHOUT_HELPER)

    delta = _observe(root, store, updater, ["app/core.py"])

    assert _imports_cleanly(root)
    assert delta["dangling_importers"] == []


def test_walrus_in_a_lambda_body_binds_no_module_name(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(
        root,
        "app/core.py",
        CORE_WITHOUT_HELPER + "\n\nfactory = lambda x=keep: (helper := x)\n",
    )

    delta = _observe(root, store, updater, ["app/core.py"])

    assert not _imports_cleanly(root)
    assert delta["dangling_importers"] == [
        _import_entry("app.core.helper"),
        _all_entry("app.core.helper"),
    ]


def test_wildcard_of_a_module_whose_all_is_computed_is_taken_at_its_word(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    # An `__all__` built at run time cannot be read without running it.
    root, store, updater = indexed
    _add_unchanged_module(
        root,
        store,
        updater,
        "app/util.py",
        'def helper():\n    return 1\n\n\n__all__ = [n for n in ("helper",)]\n',
    )
    _write(root, "app/core.py", "from app.util import *\n\n\n" + CORE_WITHOUT_HELPER)

    delta = _observe(root, store, updater, ["app/core.py"])

    assert _imports_cleanly(root)
    assert delta["dangling_importers"] == []


def _run_cli_check(root: Path, store: _StatefulIngestor) -> Result:
    # The store itself, not a `MagicMock(wraps=...)`: the re-ingest must
    # really rewrite the graph, or the after-snapshot still holds the removed
    # symbol and the check measures no edit at all.
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
