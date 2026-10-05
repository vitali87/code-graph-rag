"""Renaming a React component rewrites its closing JSX tags too.

The graph records one use per element, at the opening tag, so renaming
`Frame` to `Box` left `<Box>{children}</Frame>`: a file `tsc` rejects
(TS17002), reported `applied` with `verdict.ok` (issue #2811). Self-closing
uses (`<Header />`) were already fine.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.rename import rename
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.test_rename_op import _index, _write

_CARD = """\
import React from "react";

function Frame({ children }: { children: React.ReactNode }) {
  return <section className="frame">{children}</section>;
}

export function Card({ children }: { children: React.ReactNode }) {
  return <Frame>{children}</Frame>;
}

export function Header() {
  return <h1>title</h1>;
}
"""

_APP = """\
import React from "react";
import { Card, Header } from "./Card";

export default function App() {
  return (
    <div>
      <Header />
      <Card>
        <ul />
      </Card>
      <Card><Card>nested</Card></Card>
    </div>
  );
}
"""


def _parses(path: Path) -> bool:
    # A mismatched closing tag is a TSX syntax error.
    parsers, _queries = load_parsers()
    return (
        not parsers[cs.SupportedLanguage.TSX]
        .parse(path.read_bytes())
        .root_node.has_error
    )


def _renamed(temp_repo: Path, mock: MagicMock, qn_tail: str, new_name: str) -> Path:
    _write(temp_repo, "src/Card.tsx", _CARD)
    _write(temp_repo, "src/App.tsx", _APP)
    graph = _index(temp_repo, mock)
    report = rename(
        temp_repo,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.{qn_tail}",
        new_name,
        allow_heuristic=True,
    )
    assert report.applied, report.message
    return temp_repo / "src"


def test_a_same_file_wrapper_renames_both_tags(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    src = _renamed(temp_repo, mock_ingestor, "src.Card.Frame", "Box")
    card = (src / "Card.tsx").read_text()
    assert "return <Box>{children}</Box>;" in card, card
    assert "Frame" not in card
    assert _parses(src / "Card.tsx")


def test_an_imported_component_renames_every_closing_tag(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    src = _renamed(temp_repo, mock_ingestor, "src.Card.Card", "Panel")
    app = (src / "App.tsx").read_text()
    assert "<Panel>\n        <ul />\n      </Panel>" in app, app
    assert "<Panel><Panel>nested</Panel></Panel>" in app, app
    assert "Card" not in app.replace('"./Card"', "")
    assert _parses(src / "App.tsx")


def test_a_member_expression_tag_renames_its_member(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write(
        temp_repo,
        "src/ui.tsx",
        "export function Card(p: { children: string }) { return <b>{p.children}</b>; }\n",
    )
    _write(
        temp_repo,
        "src/App.tsx",
        'import * as Ui from "./ui";\n\n'
        "export function App() { return <Ui.Card>hi</Ui.Card>; }\n",
    )
    graph = _index(temp_repo, mock_ingestor)
    report = rename(
        temp_repo,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.src.ui.Card",
        "Panel",
        allow_heuristic=True,
    )
    assert report.applied, report.message
    app = (temp_repo / "src" / "App.tsx").read_text()
    assert "<Ui.Panel>hi</Ui.Panel>" in app, app


@pytest.mark.parametrize("qn_tail", ["src.Card.Header"])
def test_a_self_closing_use_is_renamed_once(
    temp_repo: Path, mock_ingestor: MagicMock, qn_tail: str
) -> None:
    # Negatives: a self-closing tag has no closing partner, and an element
    # of another name (`<section>`, `<h1>`) is never touched.
    src = _renamed(temp_repo, mock_ingestor, qn_tail, "Title")
    app = (src / "App.tsx").read_text()
    card = (src / "Card.tsx").read_text()
    assert "<Title />" in app
    assert "<h1>title</h1>" in card
    assert '<section className="frame">{children}</section>' in card
