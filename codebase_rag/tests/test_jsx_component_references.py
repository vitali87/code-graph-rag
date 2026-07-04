from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

REFERENCES = cs.RelationshipType.REFERENCES.value


def _run_rels(
    tmp_path: Path, files: dict[str, str], lang_key: str
) -> set[tuple[str, str, str]]:
    # (H) Build the graph for `files` and return (caller_qn, rel_type, callee_qn).
    parsers, queries = load_parsers()
    if lang_key not in parsers:
        pytest.skip(f"{lang_key} parser not available")
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock, repo_path=tmp_path, parsers=parsers, queries=queries
    ).run()
    return {
        (c.args[0][2], str(c.args[1]), c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
    }


def _has(
    rels: set[tuple[str, str, str]], caller_suffix: str, rel: str, callee_suffix: str
) -> bool:
    return any(
        a.endswith(caller_suffix) and r == rel and b.endswith(callee_suffix)
        for a, r, b in rels
    )


def test_tsx_component_usage_is_referenced(tmp_path: Path) -> None:
    # (H) `<Card />` renders the Card component: React invokes it through the
    # (H) element, so the JSX usage must reference it or every component used
    # (H) only in markup reports as dead (the shadcn/ui cluster on the FastAPI
    # (H) template).
    files = {
        "card.tsx": (
            "export function Card({ title }: { title: string }) {\n"
            "  return <div>{title}</div>\n"
            "}\n"
        ),
        "app.tsx": (
            "import { Card } from './card'\n\n"
            "export function App() {\n"
            "  return (\n"
            "    <div>\n"
            "      <Card title='a' />\n"
            "    </div>\n"
            "  )\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "tsx")
    assert _has(rels, "app.App", REFERENCES, "card.Card")


def test_tsx_paired_element_component_is_referenced(tmp_path: Path) -> None:
    # (H) A paired element (<Layout>...</Layout>) carries the component name on
    # (H) its opening element; only the opening side must emit (no duplicate
    # (H) from the closing tag).
    files = {
        "layout.tsx": (
            "export function Layout({ children }: { children: unknown }) {\n"
            "  return <main>{children}</main>\n"
            "}\n"
        ),
        "app.tsx": (
            "import { Layout } from './layout'\n\n"
            "export function App() {\n"
            "  return <Layout><span>x</span></Layout>\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "tsx")
    assert _has(rels, "app.App", REFERENCES, "layout.Layout")


def test_jsx_component_usage_in_js_is_referenced(tmp_path: Path) -> None:
    # (H) The javascript grammar parses JSX natively; .jsx files get the same
    # (H) component-reference edges.
    files = {
        "widget.jsx": "export function Widget() {\n  return <b>w</b>\n}\n",
        "page.jsx": (
            "import { Widget } from './widget'\n\n"
            "export function Page() {\n"
            "  return <Widget />\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "javascript")
    assert _has(rels, "page.Page", REFERENCES, "widget.Widget")


def test_html_tags_are_not_referenced(tmp_path: Path) -> None:
    # (H) Lowercase tags are HTML elements, not components; a same-named local
    # (H) function (div) must not be misbound.
    files = {
        "app.tsx": (
            "function div() { return 1 }\n\n"
            "export function App() {\n"
            "  return <div>x</div>\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "tsx")
    assert not any(r == REFERENCES for _, r, _ in rels)


def test_member_expression_component_is_referenced(tmp_path: Path) -> None:
    # (H) A namespaced component (<Menu.Item />) names its member through a
    # (H) member expression; resolve it like any dotted callable reference.
    files = {
        "menu.tsx": (
            "export const Menu = {\n  Item: function Item() { return <li>i</li> },\n}\n"
        ),
        "app.tsx": (
            "import { Menu } from './menu'\n\n"
            "export function App() {\n"
            "  return <Menu.Item />\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "tsx")
    assert _has(rels, "app.App", REFERENCES, "Item")
