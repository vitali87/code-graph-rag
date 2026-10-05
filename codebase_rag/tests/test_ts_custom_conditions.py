# TypeScript 5's `compilerOptions.customConditions` is how a library points
# its own imports at SOURCE rather than build output (the "live types" setup):
# the manifest lists a private condition first (`"@zod/source":
# "./src/v4/index.ts"`) and the package's tsconfig names it. Only `import` /
# `module` / `default` were selectable, so a self-import (`import * as z from
# "zod/v4"`) took the `import` build artefact, which the repo does not hold,
# and became an ExternalModule with no CALLS (issue #2935).
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

_INDEX = "export function object(shape: object) {\n\treturn shape;\n}\n"
_LEGACY = "export function object(shape: object) {\n\treturn [shape];\n}\n"
_CALLER = (
    'import * as z from "mylib/v4";\n\n'
    "export function testObject() {\n\treturn z.object({});\n}\n"
)
_CONDITION = "@mylib/source"


def _exports(*keys: tuple[str, str]) -> str:
    return json.dumps(
        {"name": "mylib", "type": "module", "exports": {"./v4": dict(keys)}}
    )


_SOURCE_FIRST = _exports(
    (_CONDITION, "./src/v4/index.ts"),
    ("types", "./v4/index.d.ts"),
    ("import", "./v4/index.js"),
    ("require", "./v4/index.cjs"),
)


def _tsconfig(**options: object) -> str:
    return json.dumps(
        {
            "compilerOptions": {
                "module": "nodenext",
                "moduleResolution": "nodenext",
                **options,
            }
        }
    )


def _run(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str, str, str]]:
    parsers, queries = load_parsers()
    if "typescript" not in parsers:
        pytest.skip("typescript parser not available")
    root = tmp_path / "repo"
    root.mkdir()
    files = {
        "package.json": json.dumps(
            {"name": "root", "private": True, "workspaces": ["packages/*"]}
        ),
        **files,
    }
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(ingestor=mock, repo_path=root, parsers=parsers, queries=queries).run()
    return {
        (c.args[0][2], str(c.args[1]), str(c.args[2][0]), c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
    }


def _calls(rels: set[tuple[str, str, str, str]], caller_suffix: str) -> set[str]:
    return {
        dst
        for src, rel, _label, dst in rels
        if rel == "CALLS" and src.endswith(caller_suffix)
    }


def _imports(rels: set[tuple[str, str, str, str]], src: str) -> set[tuple[str, str]]:
    return {
        (label, dst) for s, rel, label, dst in rels if rel == "IMPORTS" and s == src
    }


_TEST_MODULE = "repo.packages.mylib.src.v4.tests.object.test"


def test_custom_condition_resolves_a_self_import_to_source(tmp_path: Path) -> None:
    rels = _run(
        tmp_path,
        {
            "packages/mylib/package.json": _SOURCE_FIRST,
            "packages/mylib/tsconfig.json": _tsconfig(customConditions=[_CONDITION]),
            "packages/mylib/src/v4/index.ts": _INDEX,
            "packages/mylib/src/v4/tests/object.test.ts": _CALLER,
        },
    )
    calls = _calls(rels, "object.test.testObject")
    assert "repo.packages.mylib.src.v4.index.object" in calls, calls
    imports = _imports(rels, _TEST_MODULE)
    assert ("Module", "repo.packages.mylib.src.v4.index") in imports, imports
    assert not any(label == "ExternalModule" for label, _ in imports), imports


def test_custom_condition_inherited_through_extends(tmp_path: Path) -> None:
    # The condition is declared once in a shared base config the package
    # config extends, as zod's packages do.
    rels = _run(
        tmp_path,
        {
            "tsconfig.base.json": _tsconfig(customConditions=[_CONDITION]),
            "packages/mylib/package.json": _SOURCE_FIRST,
            "packages/mylib/tsconfig.json": json.dumps(
                {"extends": "../../tsconfig.base.json"}
            ),
            "packages/mylib/src/v4/index.ts": _INDEX,
            "packages/mylib/src/v4/tests/object.test.ts": _CALLER,
        },
    )
    calls = _calls(rels, "object.test.testObject")
    assert "repo.packages.mylib.src.v4.index.object" in calls, calls


def test_condition_applies_only_where_its_tsconfig_governs(tmp_path: Path) -> None:
    # `app` has its own tsconfig without the condition: tsc resolves its
    # import to the build output, which this repo does not hold.
    rels = _run(
        tmp_path,
        {
            "packages/mylib/package.json": _SOURCE_FIRST,
            "packages/mylib/tsconfig.json": _tsconfig(customConditions=[_CONDITION]),
            "packages/mylib/src/v4/index.ts": _INDEX,
            "packages/app/package.json": json.dumps({"name": "app"}),
            "packages/app/tsconfig.json": _tsconfig(),
            "packages/app/src/main.ts": _CALLER,
        },
    )
    calls = _calls(rels, "main.testObject")
    assert "repo.packages.mylib.src.v4.index.object" not in calls, calls


def test_manifest_order_still_decides_between_selectable_keys(
    tmp_path: Path,
) -> None:
    # `import` precedes the custom condition here, so it wins, as in tsc.
    rels = _run(
        tmp_path,
        {
            "packages/mylib/package.json": _exports(
                ("import", "./src/v4/legacy.ts"),
                (_CONDITION, "./src/v4/index.ts"),
            ),
            "packages/mylib/tsconfig.json": _tsconfig(customConditions=[_CONDITION]),
            "packages/mylib/src/v4/index.ts": _INDEX,
            "packages/mylib/src/v4/legacy.ts": _LEGACY,
            "packages/mylib/src/v4/tests/object.test.ts": _CALLER,
        },
    )
    calls = _calls(rels, "object.test.testObject")
    assert "repo.packages.mylib.src.v4.legacy.object" in calls, calls
    assert "repo.packages.mylib.src.v4.index.object" not in calls, calls
