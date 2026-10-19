"""A Dart import of the project's own package reaches its module (#3278).

`import 'package:shapes/src/circle.dart'` is how a Dart package imports its
own `lib/` (and the only way `test/` can), where `shapes` is the `name:` of
the package's `pubspec.yaml`. Every `package:` URI was kept verbatim as an
external target, so the IMPORTS edge went to an ExternalModule, importers
of `lib/src/circle.dart` missed it, and an extension imported this way got
no CALLS edge because the import map proved no library visible.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.checkout_state import state_file
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import create_and_run_updater
from codebase_rag.tests.test_incremental_deleted_dependents import (
    _materialise,
    _snapshot,
)
from evals.cgr_graph import _StatefulIngestor

_SHAPES = {
    "pubspec.yaml": 'name: shapes\nenvironment:\n  sdk: ">=3.0.0 <4.0.0"\n',
    "lib/src/circle.dart": "class Circle {\n  double area() => 3.14;\n}\n",
    "lib/ext/fancy.dart": (
        "import '../src/circle.dart';\n\n"
        "extension Fancy on Circle {\n  String label() => 'fancy';\n}\n"
    ),
    "lib/ext/plain.dart": (
        "import '../src/circle.dart';\n\n"
        "extension Plain on Circle {\n  String label() => 'plain';\n}\n"
    ),
    "lib/report.dart": (
        "import 'package:shapes/src/circle.dart';\n"
        "import 'package:shapes/ext/fancy.dart';\n\n"
        "String show(Circle c) => c.label();\n"
    ),
    "lib/report_rel.dart": (
        "import 'src/circle.dart';\nimport 'ext/fancy.dart';\n\n"
        "String show(Circle c) => c.label();\n"
    ),
    "test/circle_test.dart": (
        "import 'package:shapes/src/circle.dart';\n"
        "import 'package:flutter/material.dart';\nimport 'dart:math';\n\n"
        "void main() {\n  final c = Circle();\n  c.area();\n}\n"
    ),
}

# Two packages in one repository, one importing the other by name. The
# imported one quotes its name and comments it, as YAML allows.
_MONOREPO = {
    "pkgs/a/pubspec.yaml": "name: 'a' # the library\n",
    "pkgs/a/lib/a.dart": "int one() => 1;\n",
    "pkgs/b/pubspec.yaml": "name: b\ndependencies:\n  a:\n    path: ../a\n",
    "pkgs/b/lib/b.dart": "import 'package:a/a.dart';\n\nint two() => one() + 1;\n",
    # A build output's copy of a pubspec names no package of the project,
    # though it sits shallower than the real one.
    "build/pubspec.yaml": "name: a\n",
}


def _write(repo: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")


def _index(
    repo: Path,
    mock: MagicMock,
    files: dict[str, str],
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
) -> GraphUpdater:
    _write(repo, files)
    return create_and_run_updater(
        repo, mock, exclude_paths=exclude_paths, unignore_paths=unignore_paths
    )


def _edges(repo: Path, mock: MagicMock, rel: str) -> set[tuple[str, str, str]]:
    # (from, to label, to) of every `rel` edge, project prefix dropped.
    prefix = f"{repo.name}."
    return {
        (
            str(c.args[0][2]).removeprefix(prefix),
            str(c.args[2][0]),
            str(c.args[2][2]).removeprefix(prefix),
        )
        for c in mock.ensure_relationship_batch.call_args_list
        if c.args[1] == rel
    }


@pytest.fixture
def shapes(temp_repo: Path, mock_ingestor: MagicMock) -> tuple[Path, MagicMock]:
    _index(temp_repo, mock_ingestor, _SHAPES)
    return temp_repo, mock_ingestor


@pytest.mark.parametrize(
    ("importer", "target"),
    [
        ("lib.report", "lib.src.circle"),
        ("lib.report", "lib.ext.fancy"),
        ("test.circle_test", "lib.src.circle"),
    ],
    ids=["lib-to-lib", "lib-to-extension", "test-to-lib"],
)
def test_an_own_package_import_targets_the_module(
    shapes: tuple[Path, MagicMock], importer: str, target: str
) -> None:
    imports = _edges(*shapes, cs.RelationshipType.IMPORTS.value)
    assert (importer, cs.NodeLabel.MODULE.value, target) in imports, sorted(imports)


def test_an_extension_imported_by_package_uri_binds_its_call(
    shapes: tuple[Path, MagicMock],
) -> None:
    calls = _edges(*shapes, cs.RelationshipType.CALLS.value)
    label = {to for frm, _l, to in calls if frm == "lib.report.show"}
    # The same result the relative imports in `report_rel.dart` get.
    rel = {to for frm, _l, to in calls if frm == "lib.report_rel.show"}
    assert label == rel == {"lib.ext.fancy.Fancy.label"}, (label, rel)


def test_other_schemes_stay_external(shapes: tuple[Path, MagicMock]) -> None:
    # Negative: a package the repository does not hold, and the SDK.
    imports = _edges(*shapes, cs.RelationshipType.IMPORTS.value)
    targets = {(label, to) for frm, label, to in imports if frm == "test.circle_test"}
    assert (cs.NodeLabel.EXTERNAL_MODULE.value, "package:flutter/material.dart") in (
        targets
    ), targets
    assert not any(to.startswith("lib.") and "flutter" in to for _l, to in targets)


def test_a_sibling_package_in_the_repository_is_resolved(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, _MONOREPO)

    imports = _edges(temp_repo, mock_ingestor, cs.RelationshipType.IMPORTS.value)
    calls = _edges(temp_repo, mock_ingestor, cs.RelationshipType.CALLS.value)
    assert ("pkgs.b.lib.b", cs.NodeLabel.MODULE.value, "pkgs.a.lib.a") in imports, (
        sorted(imports)
    )
    assert ("pkgs.b.lib.b.two", cs.NodeLabel.FUNCTION.value, "pkgs.a.lib.a.one") in (
        calls
    ), sorted(calls)


def test_a_package_name_the_repository_does_not_declare_stays_external(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: without a pubspec naming it, `package:shapes/...` is a
    # dependency, even when `lib/src/circle.dart` happens to exist.
    files = {k: v for k, v in _SHAPES.items() if k != "pubspec.yaml"}
    _index(temp_repo, mock_ingestor, files)

    imports = _edges(temp_repo, mock_ingestor, cs.RelationshipType.IMPORTS.value)
    assert (
        "lib.report",
        cs.NodeLabel.EXTERNAL_MODULE.value,
        "package:shapes/src/circle.dart",
    ) in imports, sorted(imports)


def test_a_name_two_pubspecs_claim_goes_to_the_shallower_package(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A copy of the package nested inside it (a template, a fixture) must
    # not take the import from the package itself.
    files = {
        "pubspec.yaml": "name: shapes\n",
        "lib/src/circle.dart": "class Circle {}\n",
        "tool/template/pubspec.yaml": "name: shapes\n",
        "tool/template/lib/src/circle.dart": "class Circle {}\n",
        "test/circle_test.dart": "import 'package:shapes/src/circle.dart';\n",
    }
    _index(temp_repo, mock_ingestor, files)

    imports = _edges(temp_repo, mock_ingestor, cs.RelationshipType.IMPORTS.value)
    targets = {to for frm, _l, to in imports if frm == "test.circle_test"}
    assert targets == {"lib.src.circle"}, targets


# An app importing a package by name, with that package placed where the
# indexer's filters decide whether it is part of the project.
def _app_importing(package_dir: str) -> dict[str, str]:
    return {
        "pubspec.yaml": "name: app\n",
        "lib/main.dart": "import 'package:a/a.dart';\n\nint two() => one() + 1;\n",
        f"{package_dir}/pubspec.yaml": "name: a\n",
        f"{package_dir}/lib/a.dart": "int one() => 1;\n",
    }


def test_an_excluded_package_stays_external(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The excluded package's modules are not in the graph, so its name must
    # not claim the import: the edge stays on the external target instead of
    # pointing at (and being dropped for) a module that was never indexed.
    _index(
        temp_repo,
        mock_ingestor,
        _app_importing("third_party/a"),
        exclude_paths=frozenset({"third_party"}),
    )

    imports = _edges(temp_repo, mock_ingestor, cs.RelationshipType.IMPORTS.value)
    assert (
        "lib.main",
        cs.NodeLabel.EXTERNAL_MODULE.value,
        "package:a/a.dart",
    ) in imports, sorted(imports)


def test_an_unignored_package_under_an_ignored_dir_is_resolved(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `vendor/` is skipped by default; unignoring it indexes the package, so
    # its name must reach the indexed module like any other package's.
    _index(
        temp_repo,
        mock_ingestor,
        _app_importing("vendor/a"),
        unignore_paths=frozenset({"vendor"}),
    )

    imports = _edges(temp_repo, mock_ingestor, cs.RelationshipType.IMPORTS.value)
    edge = ("lib.main", cs.NodeLabel.MODULE.value, "vendor.a.lib.a")
    assert edge in imports, sorted(imports)


def test_a_renamed_package_is_seen_by_the_next_run(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The package map is per run: renaming a pubspec's `name:` and
    # re-indexing with the same updater must resolve against the new name.
    files = {k: v for k, v in _SHAPES.items() if k != "pubspec.yaml"}
    updater = _index(
        temp_repo, mock_ingestor, {**files, "pubspec.yaml": "name: old_shapes\n"}
    )
    edge = ("test.circle_test", cs.NodeLabel.MODULE.value, "lib.src.circle")
    imports = _edges(temp_repo, mock_ingestor, cs.RelationshipType.IMPORTS.value)
    assert edge not in imports, sorted(imports)

    _write(temp_repo, {"pubspec.yaml": "name: shapes\n"})
    mock_ingestor.reset_mock()
    updater.run(force=True)

    imports = _edges(temp_repo, mock_ingestor, cs.RelationshipType.IMPORTS.value)
    assert edge in imports, sorted(imports)


def test_a_reingested_pubspec_rename_reaches_the_reparsed_importer(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The watcher path: no full run in between, only the edited files.
    files = {k: v for k, v in _SHAPES.items() if k != "pubspec.yaml"}
    updater = _index(
        temp_repo, mock_ingestor, {**files, "pubspec.yaml": "name: old_shapes\n"}
    )
    _write(temp_repo, {"pubspec.yaml": "name: shapes\n"})
    mock_ingestor.reset_mock()
    updater.reingest(["pubspec.yaml", "test/circle_test.dart"], deleted=[])

    imports = _edges(temp_repo, mock_ingestor, cs.RelationshipType.IMPORTS.value)
    edge = ("test.circle_test", cs.NodeLabel.MODULE.value, "lib.src.circle")
    assert edge in imports, sorted(imports)


# A sync that sees only the pubspec change: the importers are unchanged on
# disk, and nothing in the graph points from them at the pubspec, so only
# the package map moving can send them to the re-parse.
_PUBSPEC_CHANGES = {
    "renamed": ("name: old_shapes\n", "name: shapes\n"),
    "renamed-away": ("name: shapes\n", "name: old_shapes\n"),
    "added": (None, "name: shapes\n"),
    "deleted": ("name: shapes\n", None),
}


def _shapes_with(pubspec: str | None) -> dict[str, str]:
    files = {k: v for k, v in _SHAPES.items() if k != cs.DEP_FILE_PUBSPEC}
    return files if pubspec is None else {**files, cs.DEP_FILE_PUBSPEC: pubspec}


def _stateful_updater(store: _StatefulIngestor, root: Path) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )


def _change_pubspec(root: Path, text: str | None) -> None:
    # Stamped past the hash cache, whose mtime the incremental pass trusts.
    stamp = state_file(root, cs.HASH_CACHE_FILENAME).stat().st_mtime + 1
    pubspec = root / cs.DEP_FILE_PUBSPEC
    if text is None:
        pubspec.unlink()
    else:
        pubspec.write_text(text, encoding="utf-8")
        os.utime(pubspec, (stamp, stamp))
    os.utime(root, (stamp, stamp))


def _sync_pubspec(updater: GraphUpdater, sync: str, deleted: bool) -> None:
    if sync == "run":
        updater.run(force=False)
    elif deleted:
        updater.reingest([], deleted=[cs.DEP_FILE_PUBSPEC])
    else:
        updater.reingest([cs.DEP_FILE_PUBSPEC])


@pytest.mark.parametrize("sync", ["run", "reingest"])
@pytest.mark.parametrize(
    ("before", "after"), list(_PUBSPEC_CHANGES.values()), ids=list(_PUBSPEC_CHANGES)
)
def test_a_pubspec_change_alone_reparses_the_package_importers(
    temp_repo: Path, sync: str, before: str | None, after: str | None
) -> None:
    root = temp_repo / "proj"
    _materialise(root, _shapes_with(before))
    store = _StatefulIngestor()
    _stateful_updater(store, root).run(force=True)
    _change_pubspec(root, after)
    _sync_pubspec(_stateful_updater(store, root), sync, deleted=after is None)

    clean_root = temp_repo / "clean" / "proj"
    clean_root.parent.mkdir()
    _materialise(clean_root, _shapes_with(after))
    clean = _StatefulIngestor()
    _stateful_updater(clean, clean_root).run(force=True)

    # `lib/report.dart` imports its own package from inside `lib/`, and
    # `test/circle_test.dart` from outside it; both move with the map.
    after, expected = _snapshot(store), _snapshot(clean)
    assert after == expected, sorted(after[1] ^ expected[1])


def test_a_pubspec_edit_keeping_the_name_reparses_no_importer(
    temp_repo: Path,
) -> None:
    # Negative: a dependency bump, the usual pubspec edit, leaves every
    # `package:` import where it was, so no Dart file joins the re-parse.
    root = temp_repo / "proj"
    pubspec = "name: shapes\n"
    _materialise(root, _shapes_with(pubspec))
    store = _StatefulIngestor()
    _stateful_updater(store, root).run(force=True)
    _change_pubspec(root, f"{pubspec}dependencies:\n  meta: ^1.9.0\n")

    report = _stateful_updater(store, root).reingest([cs.DEP_FILE_PUBSPEC])

    assert report.reparsed == (cs.DEP_FILE_PUBSPEC,)
    assert report.affected == ()
