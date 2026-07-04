from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def _run_rels(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str, str]]:
    # (H) Build the graph for `files` and return (caller_qn, rel_type, callee_qn).
    # (H) Skip on the parser each fixture actually needs: an environment can have
    # (H) typescript without tsx (pip package predating language_tsx).
    parsers, queries = load_parsers()
    required = {"tsx" if rel.endswith(".tsx") else "typescript" for rel in files}
    if missing := sorted(required - parsers.keys()):
        pytest.skip(f"parsers not available: {', '.join(missing)}")
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


def _has_call(
    rels: set[tuple[str, str, str]], caller_suffix: str, callee_suffix: str
) -> bool:
    return any(
        a.endswith(caller_suffix) and r == "CALLS" and b.endswith(callee_suffix)
        for a, r, b in rels
    )


def test_tsx_call_inside_jsx_attribute_is_traced(tmp_path: Path) -> None:
    # (H) .tsx must parse with the tsx grammar; the plain typescript grammar turns
    # (H) JSX into an ERROR forest and every call inside any React component is
    # (H) silently dropped (cn() in className={cn(...)} -- the shadcn/ui shape
    # (H) that made whole component libraries report as dead code).
    files = {
        "utils.ts": "export function cn(...args: string[]) { return args.join(' ') }\n",
        "card.tsx": (
            "import { cn } from './utils'\n\n"
            "export function Card({ className }: { className?: string }) {\n"
            "  return <div className={cn('card', className)}>x</div>\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has_call(rels, "card.Card", "utils.cn")


def test_tsx_call_in_jsx_event_handler_is_traced(tmp_path: Path) -> None:
    # (H) An arrow handler inside a JSX attribute (onClick={() => save(id)}) lives
    # (H) inside the JSX expression tree; its call must reach the graph.
    files = {
        "api.ts": "export function save(id: string) { return id }\n",
        "button.tsx": (
            "import { save } from './api'\n\n"
            "export function SaveButton({ id }: { id: string }) {\n"
            "  return <button onClick={() => save(id)}>save</button>\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert any(r == "CALLS" and b.endswith("api.save") for _, r, b in rels)


def test_tsx_plain_typescript_constructs_still_work(tmp_path: Path) -> None:
    # (H) The tsx grammar must not regress ordinary TS constructs inside .tsx.
    files = {
        "svc.tsx": (
            "class Service {\n"
            "  fetch(): number { return 1 }\n"
            "}\n\n"
            "export function useService() {\n"
            "  const svc = new Service()\n"
            "  return svc.fetch()\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert _has_call(rels, "svc.useService", "Service.fetch")


def test_ts_generic_arrow_function_still_parses(tmp_path: Path) -> None:
    # (H) Plain .ts keeps the typescript grammar: a bare generic arrow
    # (H) (`<T>(x: T) => x`) is legal .ts but parses as JSX under the tsx
    # (H) grammar, so .ts and .tsx need SEPARATE grammars, not a shared one.
    files = {
        "gen.ts": (
            "export function target() { return 1 }\n\n"
            "export const wrap = <T>(x: T): T => {\n"
            "  target()\n"
            "  return x\n"
            "}\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert any(r == "CALLS" and b.endswith("gen.target") for _, r, b in rels)
