from __future__ import annotations

from pathlib import Path
from typing import NamedTuple
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

# Issue #2535: a JSX member tag (`<Prim.Item>`) whose namespace is imported from
# an npm package named its member only through the last segment, so the
# bare-name fallback bound it (labelled `exact`) to whatever first-party symbol
# shared that name, in files the component never imports, interfaces included.

REFERENCES = cs.RelationshipType.REFERENCES.value
CALLS = cs.RelationshipType.CALLS.value

# First-party symbols named like common UI-primitive members, in a file none
# of the components import; any edge into it from a JSX tag is a false bind.
UNRELATED = (
    "interface Item { id: number }\n"
    "export interface Thing { a: number }\n"
    "export function Root(): Item { return { id: 1 }; }\n"
    "export function Panel() {}\n"
    "export function Fragment() {}\n"
)


class _Edge(NamedTuple):
    source: str
    rel: str
    target_label: str
    target: str
    resolution: str | None


def _edges(tmp_path: Path, files: dict[str, str], lang_key: str) -> list[_Edge]:
    parsers, queries = load_parsers()
    if lang_key not in parsers:
        pytest.skip(f"{lang_key} parser not available")
    repo = tmp_path / "proj"
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(ingestor=mock, repo_path=repo, parsers=parsers, queries=queries).run()
    edges: list[_Edge] = []
    for call in mock.ensure_relationship_batch.call_args_list:
        props = call.kwargs.get("properties") or {}
        resolution = props.get(cs.KEY_RESOLUTION)
        edges.append(
            _Edge(
                call.args[0][2],
                str(call.args[1]),
                str(call.args[2][0]),
                call.args[2][2],
                str(resolution) if resolution is not None else None,
            )
        )
    return edges


def _refs_from(edges: list[_Edge], source: str) -> list[_Edge]:
    return [e for e in edges if e.rel == REFERENCES and e.source == source]


def test_issue_repro_external_namespace_tags_bind_nothing_first_party(
    tmp_path: Path,
) -> None:
    # The issue's exact repro: Radix `<Prim.Root>`/`<Prim.Item>` must not reach
    # scripts/report.ts's `Root` function or its `Item` interface.
    files = {
        "src/Menu.tsx": (
            'import * as Prim from "@radix-ui/react-accordion";\n'
            "export function Menu() {\n"
            "  return (\n"
            '    <Prim.Root type="single">\n'
            '      <Prim.Item value="a">A</Prim.Item>\n'
            "    </Prim.Root>\n"
            "  );\n"
            "}\n"
        ),
        "scripts/report.ts": UNRELATED,
    }
    edges = _edges(tmp_path, files, "tsx")
    refs = _refs_from(edges, "proj.src.Menu.Menu")
    assert refs == [], f"external namespace tags bound first-party symbols: {refs}"


@pytest.mark.parametrize(
    ("import_line", "tag"),
    [
        ('import Def from "some-lib";', "Def.Root"),
        ('import { Dialog } from "@headlessui/react";', "Dialog.Panel"),
        ('import * as React from "react";', "React.Fragment"),
    ],
    ids=["default-import", "named-import", "single-segment-package"],
)
def test_external_import_member_tag_binds_nothing(
    tmp_path: Path, import_line: str, tag: str
) -> None:
    # Every way an npm package can bind the tag's head (default import, a named
    # namespace object like Headless UI's `Dialog`, a one-segment package name
    # like `react`) names a member of that package, never a project symbol.
    files = {
        "src/View.tsx": (
            f"{import_line}\nexport function View() {{\n  return <{tag} />;\n}}\n"
        ),
        "scripts/report.ts": UNRELATED,
    }
    edges = _edges(tmp_path, files, "tsx")
    refs = _refs_from(edges, "proj.src.View.View")
    assert refs == [], f"<{tag}> bound first-party symbols: {refs}"


def test_external_require_member_tag_binds_nothing(tmp_path: Path) -> None:
    # A CommonJS `require()` of a package binds the head the same way in .jsx.
    files = {
        "src/View.jsx": (
            'const UI = require("ui-lib");\n'
            "export function View() {\n  return <UI.Panel />;\n}\n"
        ),
        "scripts/report.js": "export function Panel() {}\n",
    }
    edges = _edges(tmp_path, files, "javascript")
    refs = _refs_from(edges, "proj.src.View.View")
    assert refs == [], refs


def test_jsx_tag_never_binds_a_type_only_node(tmp_path: Path) -> None:
    # A JSX tag names a component; an interface can never be rendered, so a
    # same-named interface elsewhere is not a target even by bare name.
    files = {
        "src/View.tsx": "export function View() {\n  return <Thing />;\n}\n",
        "scripts/report.ts": UNRELATED,
    }
    edges = _edges(tmp_path, files, "tsx")
    refs = _refs_from(edges, "proj.src.View.View")
    assert all(
        e.target_label not in (cs.NodeLabel.INTERFACE, cs.NodeLabel.TYPE) for e in refs
    ), refs


# Negative tests: resolution that must stay as it was.


def test_first_party_namespace_member_tag_still_resolves_exact(
    tmp_path: Path,
) -> None:
    # `Ns` bound to a project module resolves `Item` inside that module, not
    # the unrelated same-named symbol elsewhere.
    files = {
        "src/View.tsx": (
            'import * as Ns from "./widgets";\n'
            "export function View() {\n  return <Ns.Item />;\n}\n"
        ),
        "src/widgets.tsx": "export function Item() {\n  return <li />;\n}\n",
        "scripts/report.ts": "export function Item() {}\n",
    }
    edges = _edges(tmp_path, files, "tsx")
    refs = _refs_from(edges, "proj.src.View.View")
    assert [(e.target, e.resolution) for e in refs] == [
        ("proj.src.widgets.Item", cs.EdgeResolution.EXACT.value)
    ], refs


def test_tsconfig_alias_namespace_member_tag_still_resolves(tmp_path: Path) -> None:
    # `@/components/ui` looks like a scoped package but is a tsconfig alias
    # for a project folder, so its members stay first-party.
    files = {
        "tsconfig.json": '{"compilerOptions":{"baseUrl":".","paths":{"@/*":["src/*"]}}}',
        "src/components/ui.tsx": "export function Button() {\n  return <b />;\n}\n",
        "src/View.tsx": (
            'import * as UI from "@/components/ui";\n'
            "export function View() {\n  return <UI.Button />;\n}\n"
        ),
    }
    edges = _edges(tmp_path, files, "tsx")
    refs = _refs_from(edges, "proj.src.View.View")
    assert [e.target for e in refs] == ["proj.src.components.ui.Button"], refs


def test_plain_tag_imported_by_name_still_resolves(tmp_path: Path) -> None:
    files = {
        "src/View.tsx": (
            'import { Item } from "./widgets";\n'
            "export function View() {\n  return <Item />;\n}\n"
        ),
        "src/widgets.tsx": "export function Item() {\n  return <li />;\n}\n",
        "scripts/report.ts": UNRELATED,
    }
    edges = _edges(tmp_path, files, "tsx")
    refs = _refs_from(edges, "proj.src.View.View")
    assert [(e.target, e.resolution) for e in refs] == [
        ("proj.src.widgets.Item", cs.EdgeResolution.EXACT.value)
    ], refs


def test_class_component_tag_still_resolves(tmp_path: Path) -> None:
    # The type-only guard keeps class components: a class is renderable.
    files = {
        "src/card.tsx": (
            "export class Card {\n  render() { return <div>x</div> }\n}\n"
        ),
        "src/View.tsx": (
            'import { Card } from "./card";\n'
            "export function View() {\n  return <Card />;\n}\n"
        ),
    }
    edges = _edges(tmp_path, files, "tsx")
    refs = _refs_from(edges, "proj.src.View.View")
    assert [(e.target_label, e.target) for e in refs] == [
        (cs.NodeLabel.CLASS.value, "proj.src.card.Card")
    ], refs


def test_namespace_member_calls_keep_todays_behaviour(tmp_path: Path) -> None:
    # Outside JSX, `Ns.Item()` already resolves through the JS/TS member-call
    # rules: an external namespace binds nothing, a first-party one binds the
    # module's own function. The JSX fix must leave both exactly as they are.
    files = {
        "src/calls.ts": (
            'import * as Prim from "@radix-ui/react-accordion";\n'
            'import * as Ns from "./widgets";\n'
            "export function external() { return Prim.Item(); }\n"
            "export function firstParty() { return Ns.Item(); }\n"
        ),
        "src/widgets.ts": "export function Item() { return 1; }\n",
        "scripts/report.ts": UNRELATED,
    }
    edges = _edges(tmp_path, files, "typescript")
    external = [e for e in edges if e.source == "proj.src.calls.external"]
    first_party = [
        (e.rel, e.target)
        for e in edges
        if e.source == "proj.src.calls.firstParty" and e.rel == CALLS
    ]
    assert [e for e in external if e.rel in (CALLS, REFERENCES)] == []
    assert first_party == [(CALLS, "proj.src.widgets.Item")]
