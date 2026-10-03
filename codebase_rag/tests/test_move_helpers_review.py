"""The module-level helpers `move` builds its imports from.

Each case here is a way the moved code, or the file it came from, ended
up with the wrong imports while still parsing -- so the transaction's
parse gate let it through and the damage surfaced at runtime:

- an import nested in a function counted as binding the module, so the
  top-level import the rewritten call sites need was never added;
- a relative `from .x import y` was copied verbatim into a file in
  another package, where the same dots name a different module;
- a default or namespace JS binding was dropped, or dragged its
  siblings along, and a TS inline `type` entry was never matched;
- `import a, b` was copied whole, binding `b` in the destination;
- `.tsx` took the Python path;
- an import added to a file without imports landed above the module
  docstring, shebang, `from __future__` line or "use strict" directive.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.move import (
    _JS_LANGUAGES,
    _import_block_end,
    _module_bound,
    _narrow_statement,
)
from codebase_rag.parser_loader import load_parsers

PY = cs.SupportedLanguage.PYTHON
JS = cs.SupportedLanguage.JS
TS = cs.SupportedLanguage.TS
TSX = cs.SupportedLanguage.TSX


# --- _module_bound: only module-level imports bind the module --------------------------


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("def f():\n    import pkg.new\n", id="in-function"),
        pytest.param("class C:\n    import pkg.new\n", id="in-class"),
        pytest.param(
            "def f():\n    if True:\n        import pkg.new\n", id="in-nested-block"
        ),
    ],
)
def test_a_nested_import_does_not_bind_the_module(source: str) -> None:
    assert _module_bound(source, "pkg.new") is False


def test_a_top_level_import_next_to_a_nested_one_still_binds() -> None:
    source = "import pkg.new\n\ndef f():\n    import other\n"
    assert _module_bound(source, "pkg.new") is True


# --- _narrow_statement, Python ---------------------------------------------------------


@pytest.mark.parametrize(
    ("statement", "alias", "old_path", "new_path", "expected"),
    [
        pytest.param(
            "from .alpha import value",
            "value",
            "app/src.py",
            "app/dst/mod.py",
            "from ..alpha import value",
            id="into-subpackage",
        ),
        pytest.param(
            "from .deps import X",
            "X",
            "pkg/sub/util.py",
            "pkg/core.py",
            "from .sub.deps import X",
            id="into-parent-package",
        ),
        pytest.param(
            "from . import helpers",
            "helpers",
            "p/x.py",
            "p/y.py",
            "from . import helpers",
            id="same-package-unchanged",
        ),
        pytest.param(
            "from ..shared import tool",
            "tool",
            "pkg/a/mod.py",
            "pkg/b/mod.py",
            "from ..shared import tool",
            id="sibling-package-same-depth",
        ),
        pytest.param(
            "from pkg.deps import X",
            "X",
            "pkg/sub/util.py",
            "pkg/core.py",
            "from pkg.deps import X",
            id="absolute-unchanged",
        ),
    ],
)
def test_a_relative_from_import_is_respelled_for_the_destination(
    statement: str, alias: str, old_path: str, new_path: str, expected: str
) -> None:
    assert _narrow_statement(statement, alias, PY, old_path, new_path) == expected


def test_the_respelled_relative_import_resolves_to_the_original_module(
    tmp_path: Path,
) -> None:
    """The interpreter, not a string comparison, is the judge of `..`."""
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "sub" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "sub" / "deps.py").write_text("X = 'sub.deps'\n")
    # A decoy the verbatim copy would have resolved to.
    (tmp_path / "pkg" / "deps.py").write_text("X = 'decoy'\n")
    line = _narrow_statement(
        "from .deps import X", "X", PY, "pkg/sub/util.py", "pkg/core.py"
    )
    assert line is not None
    (tmp_path / "pkg" / "core.py").write_text(line + "\n")
    sys.path.insert(0, str(tmp_path))
    try:
        assert importlib.import_module("pkg.core").X == "sub.deps"
    finally:
        sys.path.remove(str(tmp_path))
        for name in [m for m in sys.modules if m == "pkg" or m.startswith("pkg.")]:
            del sys.modules[name]


@pytest.mark.parametrize(
    ("statement", "alias", "expected"),
    [
        pytest.param("import a, b", "a", "import a", id="first-of-two"),
        pytest.param("import a, b", "b", "import b", id="second-of-two"),
        pytest.param(
            "import pkg.dep, other", "pkg.dep", "import pkg.dep", id="dotted-by-path"
        ),
        pytest.param(
            "import pkg.dep, other", "pkg", "import pkg.dep", id="dotted-by-root"
        ),
        pytest.param("import x as y, z", "y", "import x as y", id="aliased-entry"),
        pytest.param("import solo", "solo", "import solo", id="single-unchanged"),
    ],
)
def test_a_plain_import_is_narrowed_to_the_needed_entry(
    statement: str, alias: str, expected: str
) -> None:
    assert _narrow_statement(statement, alias, PY, "a/x.py", "a/y.py") == expected


# --- _narrow_statement, JS/TS ----------------------------------------------------------


@pytest.mark.parametrize(
    ("statement", "alias", "expected"),
    [
        pytest.param(
            'import client, { helper } from "./api";',
            "client",
            'import client from "./api";',
            id="default-of-mixed",
        ),
        pytest.param(
            'import client, { helper } from "./api";',
            "helper",
            'import { helper } from "./api";',
            id="named-of-mixed-drops-default",
        ),
        pytest.param(
            'import D, * as ns from "./api";',
            "D",
            'import D from "./api";',
            id="default-of-default-and-namespace",
        ),
        pytest.param(
            'import D, * as ns from "./api";',
            "ns",
            'import * as ns from "./api";',
            id="namespace-of-default-and-namespace",
        ),
        pytest.param(
            "import { type Foo, Bar } from './api';",
            "Foo",
            "import { type Foo } from './api';",
            id="inline-type-entry",
        ),
        pytest.param(
            "import { type Foo as F, Bar } from './api';",
            "F",
            "import { type Foo as F } from './api';",
            id="inline-type-entry-aliased",
        ),
        pytest.param(
            "import type { Foo, Bar } from './api';",
            "Foo",
            "import type { Foo } from './api';",
            id="type-only-statement-keeps-type",
        ),
        pytest.param(
            "import { a, b as c } from './api'",
            "c",
            "import { b as c } from './api'",
            id="plain-named-no-semicolon",
        ),
    ],
)
def test_a_js_import_keeps_exactly_the_needed_binding(
    statement: str, alias: str, expected: str
) -> None:
    assert _narrow_statement(statement, alias, TS, "src/x.ts", "src/y.ts") == expected


def test_tsx_is_handled_as_javascript() -> None:
    assert TSX in _JS_LANGUAGES
    assert (
        _narrow_statement(
            "import { a, b } from './m';", "a", TSX, "src/x.tsx", "src/ui/y.tsx"
        )
        == "import { a } from '../m';"
    )


# --- _import_block_end: never above the prologue --------------------------------------


def _end(language: cs.SupportedLanguage, source: bytes) -> int:
    root = load_parsers()[0][language].parse(source).root_node
    return _import_block_end(source, root)


@pytest.mark.parametrize(
    ("language", "prologue", "body"),
    [
        pytest.param(PY, b'"""Doc."""\n', b"\nX = 1\n", id="py-docstring"),
        pytest.param(
            PY,
            b'#!/usr/bin/env python\n# -*- coding: utf-8 -*-\n"""Doc."""\n',
            b"X = 1\n",
            id="py-shebang-encoding-docstring",
        ),
        pytest.param(PY, b"#!/usr/bin/env python\n", b"X = 1\n", id="py-shebang"),
        pytest.param(
            PY,
            b'"""Doc."""\nfrom __future__ import annotations\n',
            b"X = 1\n",
            id="py-future",
        ),
        pytest.param(
            JS, b'#!/usr/bin/env node\n"use strict";\n', b"const x = 1;\n", id="js"
        ),
    ],
)
def test_an_import_is_never_placed_above_the_prologue(
    language: cs.SupportedLanguage, prologue: bytes, body: bytes
) -> None:
    assert _end(language, prologue + body) == len(prologue)


@pytest.mark.parametrize(
    ("language", "source"),
    [
        pytest.param(PY, b"X = 1\n", id="py-no-prologue"),
        pytest.param(PY, b"", id="py-empty"),
        pytest.param(JS, b"const x = 1;\n", id="js-no-prologue"),
        # A string that is not the first statement is not a docstring.
        pytest.param(PY, b'X = 1\n"""not a docstring"""\n', id="py-late-string"),
    ],
)
def test_without_a_prologue_the_top_of_the_file_is_still_right(
    language: cs.SupportedLanguage, source: bytes
) -> None:
    assert _end(language, source) == 0


def test_existing_imports_still_decide_the_insertion_point() -> None:
    source = b'"""Doc."""\nimport os\nimport sys\n\nX = 1\n'
    assert _end(PY, source) == source.index(b"import sys\n") + len(b"import sys\n")
