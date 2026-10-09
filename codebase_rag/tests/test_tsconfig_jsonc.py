"""tsconfig.json is read as JSONC, the way TypeScript reads it.

Comments were stripped with regexes that knew nothing of string literals, so
the `/*` in `"@/*"` or `"**/*.ts"` opened a "comment" running to the next
`*/`. The result was not JSON and the whole config was dropped without a
word: no `paths`, no `baseUrl`, and every `@/…` import became an external
package (issue #3178; shadcn-ui apps/v4: 0 importers of ui/button, 185 with
the config read).
"""

from __future__ import annotations

import codecs
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers import import_processor
from evals.cgr_graph import _capture

_REPRO = """\
{
  "compilerOptions": {
    "jsx": "preserve",
    // path aliases
    "paths": { "@/*": ["./*"] }
  },
  "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx"]
}
"""
_REPRO_DATA = {
    "compilerOptions": {"jsx": "preserve", "paths": {"@/*": ["./*"]}},
    "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx"],
}
_T3_APP = """\
{
  "compilerOptions": {
    /* Base Options: */
    "esModuleInterop": true,
    "skipLibCheck": true,

    /* Path Aliases */
    "baseUrl": ".",
    "paths": {
      "~/*": ["./src/*"]
    }
  },
  "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx", "**/*.cjs", "**/*.js"],
  "exclude": ["node_modules"]
}
"""
_TANSTACK_START = """\
{
  "include": ["**/*.ts", "**/*.tsx"],
  "compilerOptions": {
    "target": "ES2022",
    /* Bundler mode */
    "moduleResolution": "bundler",
    "baseUrl": ".",
  }
}
"""
_SCHEMA = """\
{
  "$schema": "https://json.schemastore.org/tsconfig", // the editor schema
  "compilerOptions": { "paths": { "@app/*": ["src/app/*"], }, },
}
"""
_SLASH_STAR_IN_LINE_COMMENT = """\
{
  "compilerOptions": {
    // aliases: "@/*" maps to the root (see /* below)
    "paths": { "@/*": ["./*"] }
  },
  "include": ["**/*.ts"]
}
"""
_STRINGS = """\
{
  "a": "ends with a backslash \\\\",
  "b": "an escaped \\" then // and /* inside",
  "c": "a comma before a bracket ,]",
  "d": "*/"
}
"""

_BUTTON = """\
export function Button({ children }: { children?: unknown }) {
  return <button>{children}</button>;
}
"""
_PAGE = """\
import { Button } from "@/components/ui/button";
export default function Page() {
  return <Button>ok</Button>;
}
"""


def _ts_available() -> bool:
    return cs.SupportedLanguage.TSX in load_parsers()[0]


@pytest.fixture
def warnings() -> Iterator[list[str]]:
    seen: list[str] = []
    sink = logger.add(lambda m: seen.append(m.record["message"]), level="WARNING")
    yield seen
    logger.remove(sink)


def _load(tmp_path: Path, text: str, encoding: str = "utf-8") -> dict | None:
    cfg = tmp_path / "tsconfig.json"
    cfg.write_text(text, encoding=encoding)
    return import_processor._load_jsonc(cfg)


@pytest.mark.parametrize(
    ("text", "key", "expected"),
    [
        (_REPRO, "include", ["next-env.d.ts", "**/*.ts", "**/*.tsx"]),
        (_REPRO, "compilerOptions", {"jsx": "preserve", "paths": {"@/*": ["./*"]}}),
        (_T3_APP, "compilerOptions", None),
        (_TANSTACK_START, "include", ["**/*.ts", "**/*.tsx"]),
        (_SCHEMA, "$schema", "https://json.schemastore.org/tsconfig"),
        (_SLASH_STAR_IN_LINE_COMMENT, "compilerOptions", {"paths": {"@/*": ["./*"]}}),
    ],
    ids=[
        "repro-include",
        "repro-paths",
        "t3-app",
        "tanstack-start",
        "schema-url",
        "slash-star-in-line-comment",
    ],
)
def test_a_commented_tsconfig_loads_with_its_strings_intact(
    tmp_path: Path, warnings: list[str], text: str, key: str, expected: object
) -> None:
    data = _load(tmp_path, text)
    assert data is not None, text
    if expected is None:
        assert data[key]["paths"] == {"~/*": ["./src/*"]}
        assert data[key]["baseUrl"] == "."
    else:
        assert data[key] == expected
    assert warnings == []


def test_strings_keep_every_comment_and_comma_lookalike(
    tmp_path: Path, warnings: list[str]
) -> None:
    # The comment that makes the file JSONC is what used to send it through
    # the strip; every lookalike inside a string survives it.
    assert _load(tmp_path, "// x\n" + _STRINGS) == json.loads(_STRINGS)
    assert _load(tmp_path, '{"x": [1, 2, /* c */ ], // t\n}') == {"x": [1, 2]}
    assert warnings == []


@pytest.mark.skipif(not _ts_available(), reason="tsx grammar not available")
def test_the_alias_import_reaches_the_first_party_module(tmp_path: Path) -> None:
    root = tmp_path / "nextalias"
    (root / "components" / "ui").mkdir(parents=True)
    (root / "app").mkdir()
    (root / "components" / "ui" / "button.tsx").write_text(_BUTTON, encoding="utf-8")
    (root / "app" / "page.tsx").write_text(_PAGE, encoding="utf-8")
    (root / "tsconfig.json").write_text(_REPRO, encoding="utf-8")
    ingestor = _capture(root, "nextalias")
    imports = {
        (str(src), str(dst))
        for _fl, src, rel, _tl, dst in ingestor.rels
        if rel == cs.RelationshipType.IMPORTS
    }
    assert ("nextalias.app.page", "nextalias.components.ui.button") in imports, imports
    externals = {
        str(dst)
        for _fl, _src, rel, label, dst in ingestor.rels
        if rel == cs.RelationshipType.IMPORTS and label == cs.NodeLabel.EXTERNAL_MODULE
    }
    assert not {qn for qn in externals if qn.startswith("@")}, externals


def test_an_unparseable_tsconfig_is_named_once(
    tmp_path: Path, warnings: list[str]
) -> None:
    cfg = tmp_path / "tsconfig.json"
    cfg.write_text('{"compilerOptions": {"paths": {"@/*": ["./*"]}', encoding="utf-8")
    assert import_processor._load_jsonc(cfg) is None
    assert import_processor._load_jsonc(cfg) is None
    assert len([w for w in warnings if str(cfg) in w]) == 1, warnings


def test_a_bom_does_not_lose_the_config(tmp_path: Path, warnings: list[str]) -> None:
    # TypeScript accepts a UTF-8 BOM; the strict parse rejected it.
    bom = codecs.BOM_UTF8.decode("utf-8") + _REPRO
    assert _load(tmp_path, bom) == _REPRO_DATA
    assert warnings == []


def test_plain_json_still_loads_quietly(tmp_path: Path, warnings: list[str]) -> None:
    # Negatives: strict JSON is read as before, and a config that is not an
    # object is no config, without a warning either way.
    plain = '{"compilerOptions": {"baseUrl": "src"}}'
    assert _load(tmp_path, plain) == {"compilerOptions": {"baseUrl": "src"}}
    assert _load(tmp_path, "[1, 2]") is None
    assert warnings == []
