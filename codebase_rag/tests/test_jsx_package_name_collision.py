from __future__ import annotations

from pathlib import Path
from typing import NamedTuple
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

# Follow-up to issue #2535: a JSX member tag whose head is bound to a bare
# package specifier (`import * as React from "react"`) was judged first-party
# whenever the package name matched the project's own name or a local module's
# stem, so `<React.Fragment />` fell back to an unrelated local `Fragment`.

REFERENCES = cs.RelationshipType.REFERENCES.value

LOCAL_FRAGMENT = "export function Fragment() {\n  return null;\n}\n"


class _Edge(NamedTuple):
    source: str
    rel: str
    target: str
    resolution: str | None


def _refs_from(
    tmp_path: Path, project: str, files: dict[str, str], lang_key: str, source: str
) -> list[_Edge]:
    parsers, queries = load_parsers()
    if lang_key not in parsers:
        pytest.skip(f"{lang_key} parser not available")
    repo = tmp_path / project
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(ingestor=mock, repo_path=repo, parsers=parsers, queries=queries).run()
    refs: list[_Edge] = []
    for call in mock.ensure_relationship_batch.call_args_list:
        if str(call.args[1]) != REFERENCES or call.args[0][2] != source:
            continue
        props = call.kwargs.get("properties") or {}
        resolution = props.get(cs.KEY_RESOLUTION)
        refs.append(
            _Edge(
                call.args[0][2],
                str(call.args[1]),
                call.args[2][2],
                str(resolution) if resolution is not None else None,
            )
        )
    return refs


def _view(import_line: str, tag: str) -> str:
    return f"{import_line}\nexport function View() {{\n  return <{tag} />;\n}}\n"


@pytest.mark.parametrize(
    ("view_file", "import_line", "tag", "lang_key"),
    [
        ("src/View.tsx", 'import * as React from "react";', "React.Fragment", "tsx"),
        (
            "src/View.tsx",
            'import * as Runtime from "react/jsx-runtime";',
            "Runtime.Fragment",
            "tsx",
        ),
        ("src/View.tsx", 'import React from "react";', "React.Fragment", "tsx"),
        (
            "src/View.jsx",
            'const React = require("react");',
            "React.Fragment",
            "javascript",
        ),
    ],
    ids=["namespace-import", "subpath-import", "default-import", "require"],
)
def test_package_named_like_the_project_binds_no_local_symbol(
    tmp_path: Path, view_file: str, import_line: str, tag: str, lang_key: str
) -> None:
    # The project directory is `react`, so every first-party qn starts with
    # `react.`; the package `react` is still node_modules code, not the project.
    files = {
        view_file: _view(import_line, tag),
        "src/frag.tsx": LOCAL_FRAGMENT,
    }
    refs = _refs_from(tmp_path, "react", files, lang_key, "react.src.View.View")
    assert refs == [], f"<{tag}> bound a local symbol in a project named react: {refs}"


@pytest.mark.parametrize(
    ("import_line", "tag"),
    [
        ('import * as React from "react";', "React.Fragment"),
        ('import { Fragment as F } from "react";', "F.Fragment"),
    ],
    ids=["namespace-import", "named-import"],
)
def test_package_named_like_a_local_module_binds_no_local_symbol(
    tmp_path: Path, import_line: str, tag: str
) -> None:
    # A repo-root `react.tsx` registers as `proj.react`, the same path a bare
    # `react` specifier spells once project-prefixed; the bare specifier still
    # names the npm package, never that file.
    files = {
        "src/View.tsx": _view(import_line, tag),
        "react.tsx": LOCAL_FRAGMENT,
    }
    refs = _refs_from(tmp_path, "proj", files, "tsx", "proj.src.View.View")
    assert refs == [], f"<{tag}> bound the local react.tsx module: {refs}"


# Negative tests: specifiers the import processor maps to project files keep
# linking, whatever their name collides with.


@pytest.mark.parametrize(
    ("project", "files", "source", "expected"),
    [
        (
            "react",
            {
                "src/View.tsx": _view('import * as R from "./react";', "R.Fragment"),
                "src/react.tsx": LOCAL_FRAGMENT,
            },
            "react.src.View.View",
            "react.src.react.Fragment",
        ),
        (
            "proj",
            {
                "View.tsx": _view('import * as R from "./react";', "R.Fragment"),
                "react.tsx": LOCAL_FRAGMENT,
            },
            "proj.View.View",
            "proj.react.Fragment",
        ),
    ],
    ids=["project-named-react", "root-react-module"],
)
def test_relative_import_of_same_named_module_still_links(
    tmp_path: Path,
    project: str,
    files: dict[str, str],
    source: str,
    expected: str,
) -> None:
    refs = _refs_from(tmp_path, project, files, "tsx", source)
    assert [(e.target, e.resolution) for e in refs] == [
        (expected, cs.EdgeResolution.EXACT.value)
    ], refs


def test_workspace_package_import_still_links(tmp_path: Path) -> None:
    # A first-party workspace package may even be NAMED `react`: the import
    # processor maps the specifier to its source, so it is project code.
    files = {
        "packages/react/package.json": '{"name": "react"}',
        "packages/react/src/index.tsx": LOCAL_FRAGMENT,
        "src/View.tsx": _view('import * as React from "react";', "React.Fragment"),
    }
    refs = _refs_from(tmp_path, "proj", files, "tsx", "proj.src.View.View")
    assert [e.target for e in refs] == ["proj.packages.react.src.index.Fragment"], refs


def test_tsconfig_alias_named_like_the_project_still_links(tmp_path: Path) -> None:
    # A `react/*` tsconfig alias in a project named `react` resolves to a real
    # project file, so its members stay first-party.
    files = {
        "tsconfig.json": '{"compilerOptions":{"paths":{"react/*":["src/lib/*"]}}}',
        "src/lib/ui.tsx": LOCAL_FRAGMENT,
        "src/View.tsx": _view('import * as UI from "react/ui";', "UI.Fragment"),
    }
    refs = _refs_from(tmp_path, "react", files, "tsx", "react.src.View.View")
    assert [e.target for e in refs] == ["react.src.lib.ui.Fragment"], refs


# Review follow-ups: a bare specifier TypeScript resolves through `baseUrl`
# alone (no `paths` entry) names a project file, and a name rebound to a
# project module is that module's, whatever an earlier binding said.


@pytest.mark.parametrize(
    ("tsconfig", "files", "import_line", "tag", "expected"),
    [
        (
            '{"compilerOptions":{"baseUrl":"."}}',
            {"widgets.tsx": "export function Card() {\n  return <b />;\n}\n"},
            'import * as Widgets from "widgets";',
            "Widgets.Card",
            "proj.widgets.Card",
        ),
        (
            '{"compilerOptions":{"baseUrl":"."}}',
            {"react.tsx": LOCAL_FRAGMENT},
            'import * as React from "react";',
            "React.Fragment",
            "proj.react.Fragment",
        ),
    ],
    ids=["local-module", "shadows-package-name"],
)
def test_base_url_import_of_a_project_file_still_links(
    tmp_path: Path,
    tsconfig: str,
    files: dict[str, str],
    import_line: str,
    tag: str,
    expected: str,
) -> None:
    # With `baseUrl` set, TypeScript resolves a bare specifier against it
    # before node_modules, so a file there IS what the import names, even
    # when it shares a package's name.
    repo_files = {
        "tsconfig.json": tsconfig,
        "src/View.tsx": _view(import_line, tag),
        **files,
    }
    refs = _refs_from(tmp_path, "proj", repo_files, "tsx", "proj.src.View.View")
    assert [e.target for e in refs] == [expected], refs


def test_name_rebound_to_a_project_module_links(tmp_path: Path) -> None:
    # The second `require` replaces the package binding, as it replaces the
    # import target: the last binding wins.
    files = {
        "src/View.jsx": _view(
            'var UI = require("external-ui");\nvar UI = require("./widgets");',
            "UI.Card",
        ),
        "src/widgets.jsx": "export function Card() {\n  return <b />;\n}\n",
    }
    refs = _refs_from(tmp_path, "proj", files, "javascript", "proj.src.View.View")
    assert [e.target for e in refs] == ["proj.src.widgets.Card"], refs


@pytest.mark.parametrize(
    ("tsconfig", "project"),
    [
        ('{"compilerOptions":{"baseUrl":"."}}', "react"),
        ('{"compilerOptions":{"baseUrl":"src"}}', "proj"),
    ],
    ids=["project-named-react", "base-url-without-the-file"],
)
def test_base_url_that_names_no_file_keeps_the_package_external(
    tmp_path: Path, tsconfig: str, project: str
) -> None:
    # `baseUrl` only claims a specifier it resolves to a file: no
    # `<baseUrl>/react.*` exists, so `react` is still the npm package.
    files = {
        "tsconfig.json": tsconfig,
        "src/View.tsx": _view('import * as React from "react";', "React.Fragment"),
        "src/frag.tsx": LOCAL_FRAGMENT,
    }
    refs = _refs_from(tmp_path, project, files, "tsx", f"{project}.src.View.View")
    assert refs == [], refs


def test_name_rebound_to_a_package_stays_external(tmp_path: Path) -> None:
    files = {
        "src/View.jsx": _view(
            'var UI = require("./widgets");\nvar UI = require("external-ui");',
            "UI.Card",
        ),
        "src/widgets.jsx": "export function Card() {\n  return <b />;\n}\n",
    }
    refs = _refs_from(tmp_path, "proj", files, "javascript", "proj.src.View.View")
    assert refs == [], refs


# A `baseUrl` applies only to the files its tsconfig governs: those under the
# config's directory, the nearest enclosing config winning, with a `baseUrl`
# it inherits through `extends` counting as its own.

BASE_URL_DOT = '{"compilerOptions":{"baseUrl":"."}}'


def test_sibling_app_base_url_keeps_the_package_external(tmp_path: Path) -> None:
    # App B's `baseUrl` and its `react.tsx` say nothing about app A's imports:
    # in A, `react` is still the npm package.
    files = {
        "apps/a/src/View.tsx": _view(
            'import * as React from "react";', "React.Fragment"
        ),
        "apps/a/src/frag.tsx": LOCAL_FRAGMENT,
        "apps/b/tsconfig.json": BASE_URL_DOT,
        "apps/b/react.tsx": LOCAL_FRAGMENT,
    }
    refs = _refs_from(tmp_path, "proj", files, "tsx", "proj.apps.a.src.View.View")
    assert refs == [], refs


@pytest.mark.parametrize(
    ("files", "source", "expected"),
    [
        (
            {
                "apps/b/tsconfig.json": BASE_URL_DOT,
                "apps/b/widgets.tsx": "export function Card() {\n  return <b />;\n}\n",
                "apps/b/src/View.tsx": _view(
                    'import * as Widgets from "widgets";', "Widgets.Card"
                ),
            },
            "proj.apps.b.src.View.View",
            ["proj.apps.b.widgets.Card"],
        ),
        (
            {
                "tsconfig.json": BASE_URL_DOT,
                "react.tsx": LOCAL_FRAGMENT,
                "apps/b/tsconfig.json": '{"compilerOptions":{"baseUrl":"src"}}',
                "apps/b/src/View.tsx": _view(
                    'import * as React from "react";', "React.Fragment"
                ),
            },
            "proj.apps.b.src.View.View",
            [],
        ),
        (
            {
                "tsconfig.base.json": BASE_URL_DOT,
                "widgets.tsx": "export function Card() {\n  return <b />;\n}\n",
                "apps/b/tsconfig.json": '{"extends":"../../tsconfig.base.json"}',
                "apps/b/src/View.tsx": _view(
                    'import * as Widgets from "widgets";', "Widgets.Card"
                ),
            },
            "proj.apps.b.src.View.View",
            ["proj.widgets.Card"],
        ),
    ],
    ids=["file-under-the-config", "nearest-config-wins", "inherited-through-extends"],
)
def test_base_url_applies_to_the_files_its_config_governs(
    tmp_path: Path, files: dict[str, str], source: str, expected: list[str]
) -> None:
    refs = _refs_from(tmp_path, "proj", files, "tsx", source)
    assert [e.target for e in refs] == expected, refs
