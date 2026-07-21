from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _capture


def _ts_grammar_available() -> bool:
    try:
        parsers, _ = load_parsers()
    except Exception:
        return False
    return cs.SupportedLanguage.TS in parsers


needs_ts_grammar = pytest.mark.skipif(
    not _ts_grammar_available(),
    reason="cgr typescript tree-sitter grammar not installed",
)


def _make(root: Path) -> None:
    (root / "util.ts").write_text(
        "export function notImplemented(m: string): never { throw new Error(m); }\n",
        encoding="utf-8",
    )
    # A non-relative specifier (here a deno-style `ext:` alias; a tsconfig
    # `paths` alias like `@/util` behaves identically) does not resolve to a
    # file-path module qn, so the import target is unregistered.
    (root / "a.ts").write_text(
        'import { notImplemented } from "ext:alias/util.ts";\n'
        "export function useAlias() { return notImplemented('x'); }\n",
        encoding="utf-8",
    )
    (root / "b.ts").write_text(
        'import { notImplemented } from "./util.ts";\n'
        "export function useRel() { return notImplemented('y'); }\n",
        encoding="utf-8",
    )
    # A genuine external package import whose name collides with a first-party
    # symbol must NOT be rebound by the trie fallback (regression guard).
    (root / "collide.ts").write_text(
        "export function externalCollide(): number { return 1; }\n",
        encoding="utf-8",
    )
    (root / "c.ts").write_text(
        'import { externalCollide } from "some-npm-pkg";\n'
        "export function useExternal() { return externalCollide(); }\n",
        encoding="utf-8",
    )


@needs_ts_grammar
def test_call_via_non_relative_aliased_import_resolves(tmp_path: Path) -> None:
    # A call to a first-party function imported via a non-relative specifier was
    # dropped: the unresolvable target looked external and suppressed the
    # simple-name trie fallback. It must resolve to the indexed first-party
    # function, exactly as the relative-import call already does.
    _make(tmp_path)
    ingestor = _capture(tmp_path, "proj")
    calls = {
        (str(from_val), str(to_val))
        for _fl, from_val, rel, _tl, to_val in ingestor.rels
        if rel == "CALLS"
    }
    assert ("proj.a.useAlias", "proj.util.notImplemented") in calls
    assert ("proj.b.useRel", "proj.util.notImplemented") in calls
    # `some-npm-pkg` is a real external package (no custom scheme), so its
    # import stays suppressed and must not rebind to the first-party collision.
    assert ("proj.c.useExternal", "proj.collide.externalCollide") not in calls
