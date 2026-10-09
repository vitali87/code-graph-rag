"""A Dart import of the project's own package reaches its module (#3278).

`import 'package:shapes/src/circle.dart'` is how a Dart package imports its
own `lib/` (and the only way `test/` can), where `shapes` is the `name:` of
the package's `pubspec.yaml`. Every `package:` URI was kept verbatim as an
external target, so the IMPORTS edge went to an ExternalModule, importers
of `lib/src/circle.dart` missed it, and an extension imported this way got
no CALLS edge because the import map proved no library visible.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater

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


def _index(repo: Path, mock: MagicMock, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    create_and_run_updater(repo, mock)


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
