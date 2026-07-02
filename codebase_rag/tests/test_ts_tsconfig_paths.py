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
    # (H) tsconfig `paths` maps the alias `@/*` to `src/*`; a call to a function
    # (H) imported via the alias must resolve to the real first-party file, not be
    # (H) dropped as external. JSONC (comments) is tolerated.
    (root / "tsconfig.json").write_text(
        "{\n"
        "  // project config\n"
        '  "compilerOptions": {\n'
        '    "baseUrl": ".",\n'
        '    "paths": { "@/*": ["src/*"], "~lib": ["src/lib/index.ts"],\n'
        '      "*": ["src/*"] }\n'
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    src = root / "src"
    (src / "lib").mkdir(parents=True, exist_ok=True)
    (src / "util.ts").write_text(
        "export function aliasedHelper(): number { return 1; }\n", encoding="utf-8"
    )
    (src / "lib" / "index.ts").write_text(
        "export function libEntry(): number { return 2; }\n", encoding="utf-8"
    )
    # (H) A first-party function whose name collides with an external package import.
    (src / "collide.ts").write_text(
        "export function pkgCollide(): number { return 9; }\n", encoding="utf-8"
    )
    (root / "a.ts").write_text(
        'import { aliasedHelper } from "@/util";\n'
        'import { libEntry } from "~lib";\n'
        'import { pkgCollide } from "lodash";\n'
        "export function useAliases() { return aliasedHelper() + libEntry(); }\n"
        "export function useExternal() { return pkgCollide(); }\n",
        encoding="utf-8",
    )


@needs_ts_grammar
def test_tsconfig_paths_alias_calls_resolve_to_first_party_files(
    tmp_path: Path,
) -> None:
    _make(tmp_path)
    ingestor = _capture(tmp_path, "proj")
    calls = {
        (str(from_val), str(to_val))
        for _fl, from_val, rel, _tl, to_val in ingestor.rels
        if rel == "CALLS"
    }
    # (H) wildcard alias `@/*` -> `src/*`
    assert ("proj.a.useAliases", "proj.src.util.aliasedHelper") in calls
    # (H) exact (non-wildcard) alias `~lib` -> `src/lib/index.ts`
    assert ("proj.a.useAliases", "proj.src.lib.index.libEntry") in calls
    # (H) the catch-all `"*": ["src/*"]` maps `lodash` -> `src/lodash`, which has NO
    # (H) first-party file, so the alias must NOT apply and the external `pkgCollide`
    # (H) call must not rebind to the first-party `src/collide.pkgCollide`.
    assert ("proj.a.useExternal", "proj.src.collide.pkgCollide") not in calls
