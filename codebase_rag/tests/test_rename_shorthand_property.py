"""Renaming a JS/TS function keeps the key of a shorthand property (#3252).

`{ pad }` means `{ pad: pad }`: a key and a value, and only the value names
the function. The rename rewrote the token, so `export default { pad }`
became `{ padLeft }` -- the object's key changed under every consumer reading
`tools.pad`, and the program failed while the rename reported `ok`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.editing.rename import rename
from codebase_rag.tests.test_rename_op import _index, _write

_NODE = shutil.which("node")
needs_node = pytest.mark.skipif(_NODE is None, reason="node is not installed")


def _node(root: Path, entry: str) -> str:
    assert _NODE is not None
    result = subprocess.run(
        [_NODE, entry], cwd=root, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _rename_pad(root: Path, mock: MagicMock, module: str) -> str:
    graph = _index(root, mock)
    report = rename(
        root, graph.fetch_all, graph.project, f"{graph.project}.{module}.pad", "padLeft"
    )
    assert report.applied, report.message
    return report.message


_ESM = {
    "package.json": '{"type": "module"}\n',
    "u.js": 'export function pad(s) { return " " + s; }\n',
    "shorthand.js": (
        'import { pad } from "./u.js";\n'
        "export default { pad, other: 1 };\nexport const api = { pad };\n"
        'export const direct = pad("d");\n'
    ),
    "main.js": (
        'import tools, { api, direct } from "./shorthand.js";\n'
        'console.log(tools.pad("q") + api.pad("r") + direct);\n'
    ),
}


@needs_node
def test_a_shorthand_property_keeps_its_key(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    for rel, text in _ESM.items():
        _write(temp_repo, rel, text)
    before = _node(temp_repo, "main.js")

    _rename_pad(temp_repo, mock_ingestor, "u")

    shorthand = (temp_repo / "shorthand.js").read_text()
    assert "export default { pad: padLeft, other: 1 };" in shorthand, shorthand
    assert "export const api = { pad: padLeft };" in shorthand, shorthand
    # A plain reference is still renamed outright.
    assert 'export const direct = padLeft("d");' in shorthand, shorthand
    assert _node(temp_repo, "main.js") == before


def test_a_typescript_shorthand_property_keeps_its_key(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write(
        temp_repo,
        "src/u.ts",
        'export function pad(s: string): string { return " " + s; }\n',
    )
    _write(
        temp_repo,
        "src/tools.ts",
        'import { pad } from "./u";\nexport default { pad };\n',
    )

    _rename_pad(temp_repo, mock_ingestor, "src.u")

    assert (temp_repo / "src/tools.ts").read_text() == (
        'import { padLeft } from "./u";\nexport default { pad: padLeft };\n'
    )


@needs_node
def test_a_commonjs_exports_object_renames_its_key_with_its_readers(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: `module.exports = { pad }` is the module's export list, and
    # the readers `require("./u").pad` are linked to the function, so the
    # rename moves the key and every reader together, as it did before.
    files = {
        "u.js": 'function pad(s) { return " " + s; }\nmodule.exports = { pad };\n',
        "main.js": 'const u = require("./u");\nconsole.log(u.pad("q"));\n',
    }
    for rel, text in files.items():
        _write(temp_repo, rel, text)
    before = _node(temp_repo, "main.js")

    _rename_pad(temp_repo, mock_ingestor, "u")

    assert "module.exports = { padLeft };" in (temp_repo / "u.js").read_text()
    assert 'u.padLeft("q")' in (temp_repo / "main.js").read_text()
    assert _node(temp_repo, "main.js") == before
