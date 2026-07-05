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


def test_tsx_class_component_usage_is_referenced(tmp_path: Path) -> None:
    # (H) A React class component resolves to a CLASS node, not a function; the
    # (H) JSX usage must still reference the class. JS/TS classes have no
    # (H) __init__, so routing through the Python callback helper (which
    # (H) redirects CLASS -> Class.__init__) would silently drop the edge.
    files = {
        "card.tsx": (
            "import * as React from 'react'\n"
            "export class Card extends React.Component {\n"
            "  render() { return <div>x</div> }\n"
            "}\n"
        ),
        "app.tsx": (
            "import { Card } from './card'\n\n"
            "export function App() {\n"
            "  return <Card />\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "tsx")
    assert _has(rels, "app.App", REFERENCES, "card.Card")


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


def test_jsx_attribute_bare_handler_is_referenced(tmp_path: Path) -> None:
    # (H) `<button onClick={handleLogout}>` hands a local function to the element
    # (H) as a prop; the framework invokes it on the event, never by a call the
    # (H) graph can see, so the rendering scope must reference it or every event
    # (H) handler passed by name reports as dead (Sidebar/User handlers on the
    # (H) FastAPI template).
    files = {
        "panel.tsx": (
            "export function Panel() {\n"
            "  const handleLogout = () => { logout() }\n"
            "  return <button onClick={handleLogout}>x</button>\n"
            "}\n\n\n"
            "function logout() {}\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "tsx")
    assert _has(rels, "panel.Panel", REFERENCES, "panel.Panel.handleLogout")


def test_jsx_attribute_inline_arrow_is_referenced(tmp_path: Path) -> None:
    # (H) An inline arrow in a JSX attribute (`onClick={() => toggle()}`) registers
    # (H) as an anonymous node in the rendering scope with no incoming edge; the
    # (H) element consumes it, so the scope must reference it by position or every
    # (H) inline JSX handler reports as dead (the routes/* form callbacks on the
    # (H) template).
    files = {
        "input.tsx": (
            "export function PasswordInput() {\n"
            "  return <button onClick={() => toggle()}>x</button>\n"
            "}\n\n\n"
            "function toggle() {}\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "tsx")
    refs = {
        b for a, r, b in rels if r == REFERENCES and a.endswith("input.PasswordInput")
    }
    assert any(".input.PasswordInput.anonymous_" in b for b in refs), (
        f"inline JSX-attribute arrow not referenced; refs={refs}"
    )


def test_jsx_handler_in_nested_callback_is_referenced(tmp_path: Path) -> None:
    # (H) A JSX handler inside a nested anonymous callback (`items.map(item => <a
    # (H) onClick={handleMenuClick}/>)`) must still be referenced by the enclosing
    # (H) component. The map arrow is anonymous, so it never gets its own caller pass;
    # (H) the component's JSX walk must therefore continue THROUGH it instead of
    # (H) stopping at the arrow boundary, or the handler reports as dead.
    files = {
        "main.tsx": (
            "export function Main() {\n"
            "  const handleMenuClick = () => { go() }\n"
            "  return (\n"
            "    <ul>{items.map((item) => <a onClick={handleMenuClick}>{item}</a>)}</ul>\n"
            "  )\n"
            "}\n\n\n"
            "function go() {}\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "tsx")
    assert _has(rels, "main.Main", REFERENCES, "main.Main.handleMenuClick")


def test_jsx_component_in_config_callback_is_referenced(tmp_path: Path) -> None:
    # (H) TanStack Table columns render via `cell: ({ row }) => <CopyId />`; the cell
    # (H) arrow is an anonymous value in a module-level array, so the module JSX walk
    # (H) must descend through it to reference the rendered component (else CopyId,
    # (H) used only in config callbacks, reports as dead).
    files = {
        "cols.tsx": (
            "import { CopyId } from './copy'\n\n"
            "export const columns = [\n"
            "  { cell: ({ row }) => <CopyId id={row.id} /> },\n"
            "]\n"
        ),
        "copy.tsx": (
            "export function CopyId({ id }: { id: string }) {\n"
            "  return <span>{id}</span>\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files, "tsx")
    assert _has(rels, "cols", REFERENCES, "copy.CopyId")


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
