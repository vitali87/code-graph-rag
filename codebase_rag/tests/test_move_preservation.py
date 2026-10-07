# move (issue #1534) review findings from PR #2907: each test is a move that
# used to commit while changing what the program does -- a name rebound, a
# constant lost, an optional import made required, an export dropped, or a
# concurrent edit overwritten.

from __future__ import annotations

import importlib
import shutil
import subprocess
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.move import MoveRefused
from codebase_rag.editing.transaction import load_history
from codebase_rag.tests.test_move_op import (
    FIXTURE,
    PROJECT,
    _index,
    _materialise,
    _write,
)
from codebase_rag.tests.test_move_safety import OTHER, _move, _python

move_mod = importlib.import_module("codebase_rag.editing.move")


# --- the transaction's baseline is what the move was planned from ----------------


def _edit_after_planning(
    monkeypatch: pytest.MonkeyPatch, path: Path, text: str
) -> None:
    """Write `text` to `path` once `plan` has read the tree, before staging."""
    plan = move_mod.Mover.plan

    def plan_then_edit(self, *args, **kwargs):
        planned = plan(self, *args, **kwargs)
        path.write_text(text)
        return planned

    monkeypatch.setattr(move_mod.Mover, "plan", plan_then_edit)


def test_an_edit_made_after_planning_refuses_the_move(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`stage` records the CURRENT bytes as the baseline, so a file edited
    between planning and staging was overwritten with content built from
    the bytes the plan read, and the edit was lost without a conflict."""
    root = _materialise(temp_repo, FIXTURE)
    store, updater = _index(root)
    edited = FIXTURE["pkg/a.py"] + "\n\ndef later():\n    return 'kept'\n"
    _edit_after_planning(monkeypatch, root / "pkg/a.py", edited)
    with pytest.raises(MoveRefused, match="pkg/a.py changed on disk"):
        _move(root, store, updater)
    assert (root / "pkg/a.py").read_text() == edited
    assert (root / "pkg/util.py").read_text() == FIXTURE["pkg/util.py"]
    assert not (root / "pkg/core.py").exists()
    assert load_history(root) == []


def test_a_destination_created_after_planning_refuses_the_move(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan found no destination, so it wrote the whole file; one that
    appeared in the meantime was replaced by it."""
    root = _materialise(temp_repo, FIXTURE)
    store, updater = _index(root)
    created = "VERSION = 2\n"
    _edit_after_planning(monkeypatch, root / "pkg/core.py", created)
    with pytest.raises(MoveRefused, match="pkg/core.py changed on disk"):
        _move(root, store, updater)
    assert (root / "pkg/core.py").read_text() == created
    assert (root / "pkg/util.py").read_text() == FIXTURE["pkg/util.py"]
    assert load_history(root) == []


# --- imports stay in the scope they were written in -------------------------------


def test_an_import_inside_the_moved_function_is_not_copied_to_the_top(
    temp_repo: Path,
) -> None:
    """The import table holds function-local imports too, and every one
    whose name the moved text mentions was copied to the destination's top
    level: an import the function ran only when asked became one the
    module ran on load."""
    fixture = dict(FIXTURE)
    fixture["pkg/util.py"] = (
        "def helper(enabled):\n"
        "    if enabled:\n"
        "        import optional_backend\n\n"
        "        return optional_backend.run()\n"
        "    return 'off'\n" + OTHER
    )
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    report = _move(root, store, updater)
    assert report.applied, report.message
    assert report.copied_imports == ()
    core = (root / "pkg/core.py").read_text()
    assert core.startswith("def helper(enabled):\n")
    assert "        import optional_backend\n" in core
    probe = _python(root, "from pkg.core import helper; print(helper(False))")
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "off"


# --- names bound under module-level control flow ----------------------------------


@pytest.mark.parametrize(
    ("prelude", "call", "expected"),
    [
        pytest.param(
            "import sys\n\nif sys.maxsize > 0:\n    SEP = '-'\nelse:\n    SEP = '+'\n",
            "helper(['a', 'b'])",
            "a-b",
            id="if-else",
        ),
        pytest.param(
            "try:\n    import optional_backend as SEP\n"
            "except ImportError:\n    SEP = '-'\n",
            "helper(['a', 'b'])",
            "a-b",
            id="try-except-import",
        ),
    ],
)
def test_a_name_bound_in_both_branches_travels_with_the_function(
    temp_repo: Path, prelude: str, call: str, expected: str
) -> None:
    """Only top-level simple statements counted as module bindings, so a
    constant set in both branches of an `if` (or by a `try` and its handler)
    was left behind and the moved helper died with a NameError. The `try`
    form also copied its guarded import unconditionally."""
    fixture = dict(FIXTURE)
    fixture["pkg/util.py"] = (
        prelude + "\n\ndef helper(a):\n    return SEP.join(a)\n" + OTHER
    )
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    report = _move(root, store, updater)
    assert report.applied, report.message
    assert report.copied_imports == ()
    assert "from pkg.util import SEP\n" in (root / "pkg/core.py").read_text()
    probe = _python(root, f"from pkg.core import helper; print({call})")
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == expected


@pytest.mark.parametrize(
    "prelude",
    [
        pytest.param(
            "import os\n\nif os.environ.get('UNSET'):\n    SEP = '-'\n", id="if"
        ),
        pytest.param("for SEP in ['-']:\n    pass\n", id="for"),
        pytest.param(
            "try:\n    import optional_backend as SEP\nexcept ImportError:\n    pass\n",
            id="try",
        ),
    ],
)
def test_a_name_that_may_be_unbound_refuses_the_move(
    temp_repo: Path, prelude: str
) -> None:
    """`from pkg.util import SEP` at the destination would fail on load
    wherever the old module left SEP unbound, where before only a call
    reaching it failed: the move cannot carry it, so it refuses."""
    fixture = dict(FIXTURE)
    fixture["pkg/util.py"] = (
        prelude + "\n\ndef helper(a):\n    return SEP.join(a)\n" + OTHER
    )
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    with pytest.raises(MoveRefused, match="SEP"):
        _move(root, store, updater)
    assert (root / "pkg/util.py").read_text() == fixture["pkg/util.py"]
    assert not (root / "pkg/core.py").exists()


def test_a_local_name_that_a_module_loop_also_binds_does_not_refuse(
    temp_repo: Path,
) -> None:
    """Only names the moved code reads from the module scope count: its own
    `i` is a local, whatever a top-level loop binds."""
    fixture = dict(FIXTURE)
    fixture["pkg/util.py"] = (
        "TOTAL = 0\nfor i in range(3):\n    TOTAL += i\n\n\n"
        "def helper(a):\n    return [i for i in a]\n" + OTHER
    )
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    report = _move(root, store, updater)
    assert report.applied, report.message
    probe = _python(root, "from pkg.core import helper; print(helper(['a']))")
    assert probe.returncode == 0, probe.stderr


# --- what the move adds must not rebind a destination name ------------------------

RATE_CORE = "RATE = 0.1\n\n\ndef rate():\n    return RATE\n"
RATE_HELPER = "\n\ndef helper(a):\n    return a * RATE\n" + OTHER


@pytest.mark.parametrize(
    ("util", "core", "extra"),
    [
        pytest.param(
            "RATE = 0.5\n" + RATE_HELPER, RATE_CORE, {}, id="old-module-constant"
        ),
        pytest.param(
            "from pkg.cfg import RATE\n" + RATE_HELPER,
            RATE_CORE,
            {"pkg/cfg.py": "RATE = 0.5\n"},
            id="copied-import",
        ),
        pytest.param(
            "RATE = 0.5\n" + RATE_HELPER,
            "from pkg.cfg import RATE\n\n\ndef rate():\n    return RATE\n",
            {"pkg/cfg.py": "RATE = 0.1\n"},
            id="destination-import",
        ),
    ],
)
def test_an_import_that_would_rebind_a_destination_name_refuses_the_move(
    temp_repo: Path, util: str, core: str, extra: dict[str, str]
) -> None:
    """Only the moved name was checked against the destination, so the
    `from pkg.util import RATE` pasted for the helper silently replaced the
    destination's own RATE, and its `rate()` started answering 0.5."""
    fixture = {**FIXTURE, "pkg/util.py": util, "pkg/core.py": core, **extra}
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    with pytest.raises(MoveRefused, match="already binds RATE"):
        _move(root, store, updater)
    assert (root / "pkg/core.py").read_text() == core
    assert (root / "pkg/util.py").read_text() == util
    probe = _python(root, "from pkg.core import rate; print(rate())")
    assert probe.stdout.strip() == "0.1", probe.stderr


def test_an_import_the_destination_already_has_is_not_a_collision(
    temp_repo: Path,
) -> None:
    """Binding a name to what it is already bound to replaces nothing."""
    core = "import os\n\n\ndef sep():\n    return os.sep\n"
    fixture = {**FIXTURE, "pkg/core.py": core}
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    report = _move(root, store, updater)
    assert report.applied, report.message
    probe = _python(
        root, "from pkg.core import helper, sep; print(helper(['a']), sep())"
    )
    assert probe.returncode == 0, probe.stderr


# --- JavaScript: the moved name stays exported where it was -----------------------

NODE = shutil.which("node")
# The move writes extensionless specifiers (`./core`), as a bundler takes
# them; this hook resolves them the same way so plain node can load them.
RESOLVE_HOOK = """export async function resolve(specifier, context, next) {
  try {
    return await next(specifier, context);
  } catch (error) {
    if (!specifier.startsWith('.')) throw error;
    return next(`${specifier}.js`, context);
  }
}
"""
REGISTER_HOOK = (
    "import { register } from 'node:module';\n"
    "register('./hooks.mjs', import.meta.url);\n"
)
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _node(root: Path, hooks: Path, code: str) -> subprocess.CompletedProcess[str]:
    assert NODE is not None
    (root / "package.json").write_text('{"type": "module"}\n')
    (hooks / "hooks.mjs").write_text(RESOLVE_HOOK)
    (hooks / "register.mjs").write_text(REGISTER_HOOK)
    (root / "probe.mjs").write_text(code)
    return subprocess.run(
        [NODE, "--import", (hooks / "register.mjs").as_uri(), "probe.mjs"],
        cwd=root,
        check=False,
        capture_output=True,
        encoding=cs.ENCODING_UTF8,
    )


@needs_node
def test_keep_alias_still_exports_a_js_name_the_old_module_uses(
    temp_repo: Path, tmp_path: Path
) -> None:
    """With the old module still calling the name, `keep_alias` wrote only
    the local `import { helper }`, so the old path stopped exporting it."""
    root = temp_repo / PROJECT
    _write(
        root,
        "pkg/util.js",
        "export function helper(a) {\n  return a + 1;\n}\n\n"
        "export function run() {\n  return helper(1);\n}\n",
    )
    store, updater = _index(root)
    report = _move(root, store, updater, target="pkg/core.js", keep_alias=True)
    assert report.applied, report.message
    probe = _node(
        root,
        tmp_path,
        "import { helper, run } from './pkg/util.js';\nconsole.log(helper(2), run());\n",
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "3 2"


@needs_node
@pytest.mark.parametrize("keep_alias", [False, True], ids=["moved", "kept"])
def test_a_js_export_list_entry_follows_the_moved_definition(
    temp_repo: Path, tmp_path: Path, keep_alias: bool
) -> None:
    """`export { helper }` is its own statement, and the cut took only the
    declaration: the destination did not export `helper`, so the old
    module's import of it back, and every rewritten importer, failed to
    link."""
    root = temp_repo / PROJECT
    _write(
        root,
        "pkg/util.js",
        "function helper(a) {\n  return a + 1;\n}\n\n"
        "function other() {\n  return 'o';\n}\n\n"
        "export function run() {\n  return helper(1);\n}\n\n"
        "export { helper, other };\n",
    )
    _write(
        root,
        "pkg/a.js",
        "import { helper } from './util';\n\n"
        "export function go() {\n  return helper(2);\n}\n",
    )
    store, updater = _index(root)
    report = _move(root, store, updater, target="pkg/core.js", keep_alias=keep_alias)
    assert report.applied, report.message
    util = (root / "pkg/util.js").read_text()
    assert ("helper, other" in util) is keep_alias
    probe = _node(
        root,
        tmp_path,
        "import { go } from './pkg/a.js';\n"
        "import { run, other } from './pkg/util.js';\n"
        "import { helper } from './pkg/core.js';\n"
        + ("import { helper as kept } from './pkg/util.js';\n" if keep_alias else "")
        + "console.log(go(), run(), other(), helper(0));\n",
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "3 2 o 1"


# --- the cut takes the definition, not the code sharing its lines -----------------


@needs_node
def test_a_statement_after_the_definition_on_its_last_line_stays(
    temp_repo: Path, tmp_path: Path
) -> None:
    """The cut took whole lines, so a statement sharing the definition's
    last line was deleted from the old module (and pasted at the
    destination, where it ran on the wrong module's load)."""
    root = temp_repo / PROJECT
    _write(
        root,
        "pkg/util.js",
        "export function helper(x) { return x + 1; } console.log('ready');\n\n"
        "export function run() {\n  return helper(1);\n}\n",
    )
    store, updater = _index(root)
    report = _move(root, store, updater, target="pkg/core.js")
    assert report.applied, report.message
    assert "console.log" not in (root / "pkg/core.js").read_text()
    probe = _node(
        root, tmp_path, "import { run } from './pkg/util.js';\nconsole.log(run());\n"
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.split() == ["ready", "2"]


@needs_node
def test_a_statement_before_the_definition_on_its_first_line_stays(
    temp_repo: Path, tmp_path: Path
) -> None:
    root = temp_repo / PROJECT
    _write(
        root,
        "pkg/util.js",
        "export const SEP = '-'; export function helper(a) {\n"
        "  return a.join(SEP);\n}\n",
    )
    store, updater = _index(root)
    report = _move(root, store, updater, target="pkg/core.js")
    assert report.applied, report.message
    assert "export const SEP = '-';" in (root / "pkg/util.js").read_text()
    probe = _node(
        root,
        tmp_path,
        "import { helper } from './pkg/core.js';\n"
        "import { SEP } from './pkg/util.js';\n"
        "console.log(helper(['a', 'b']), SEP);\n",
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "a-b -"


# --- imports for type checkers only stay guarded ----------------------------------

# A module only a type checker may import: loading it at runtime fails.
HEAVY = (
    "raise ImportError('pkg.heavy is for type checkers only')\n\n\n"
    "class Model:\n    pass\n\n\nclass Other:\n    pass\n"
)
GUARDED_HELPER = "\n\ndef helper(m: Model) -> str:\n    return 'ok'\n" + OTHER


@pytest.mark.parametrize(
    ("typing_import", "guard"),
    [
        pytest.param("from typing import TYPE_CHECKING", "TYPE_CHECKING", id="name"),
        pytest.param("import typing", "typing.TYPE_CHECKING", id="attribute"),
    ],
)
def test_a_type_checking_import_moves_under_the_guard(
    temp_repo: Path, typing_import: str, guard: str
) -> None:
    """A name imported under `if TYPE_CHECKING:` is bound for the type
    checker only. Copying the import unconditionally made the destination
    load a module that must not be loaded at runtime; refusing it refused
    the commonest annotation pattern there is."""
    fixture = dict(FIXTURE)
    fixture["pkg/heavy.py"] = HEAVY
    fixture["pkg/util.py"] = (
        f"from __future__ import annotations\n\n{typing_import}\n\n"
        f"if {guard}:\n    from pkg.heavy import Model\n" + GUARDED_HELPER
    )
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    report = _move(root, store, updater)
    assert report.applied, report.message
    core = (root / "pkg/core.py").read_text()
    assert "from typing import TYPE_CHECKING\n" in core
    assert "if TYPE_CHECKING:\n    from pkg.heavy import Model\n" in core
    probe = _python(root, "from pkg.core import helper; print(helper(None))")
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "ok"


def test_a_type_checking_import_merges_into_the_destinations_guard(
    temp_repo: Path,
) -> None:
    fixture = dict(FIXTURE)
    fixture["pkg/heavy.py"] = HEAVY
    fixture["pkg/util.py"] = (
        "from __future__ import annotations\n\nfrom typing import TYPE_CHECKING\n\n"
        "if TYPE_CHECKING:\n    from pkg.heavy import Model\n" + GUARDED_HELPER
    )
    fixture["pkg/core.py"] = (
        "from __future__ import annotations\n\nfrom typing import TYPE_CHECKING\n\n"
        "if TYPE_CHECKING:\n    from pkg.heavy import Other\n\n\n"
        "def existing(o: Other) -> int:\n    return 1\n"
    )
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    report = _move(root, store, updater)
    assert report.applied, report.message
    core = (root / "pkg/core.py").read_text()
    assert core.count("TYPE_CHECKING") == 2
    assert (
        "if TYPE_CHECKING:\n"
        "    from pkg.heavy import Other\n"
        "    from pkg.heavy import Model\n"
    ) in core
    probe = _python(
        root,
        "from pkg.core import existing, helper; print(existing(None), helper(None))",
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "1 ok"


def test_a_type_checking_import_that_would_rebind_a_destination_name_refuses(
    temp_repo: Path,
) -> None:
    fixture = dict(FIXTURE)
    fixture["pkg/heavy.py"] = HEAVY
    fixture["pkg/util.py"] = (
        "from __future__ import annotations\n\nfrom typing import TYPE_CHECKING\n\n"
        "if TYPE_CHECKING:\n    from pkg.heavy import Model\n" + GUARDED_HELPER
    )
    fixture["pkg/core.py"] = "Model = 1\n"
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    with pytest.raises(MoveRefused, match="already binds Model"):
        _move(root, store, updater)
    assert (root / "pkg/core.py").read_text() == "Model = 1\n"


# --- removing the export takes the export, not its line ---------------------------


@needs_node
@pytest.mark.parametrize(
    "line",
    [
        "export { helper }; registerPlugin();",
        "registerPlugin(); export { helper };",
        "export { helper, other }; registerPlugin();",
    ],
)
def test_a_statement_sharing_the_export_line_stays(
    temp_repo: Path, tmp_path: Path, line: str
) -> None:
    """The sole `export { helper }` was removed with its whole line, and
    `registerPlugin()` on it with it: the old module silently stopped
    registering anything on load."""
    root = temp_repo / PROJECT
    _write(
        root,
        "pkg/util.js",
        "export const plugins = [];\n\n"
        "function registerPlugin() {\n  plugins.push('p');\n}\n\n"
        "function helper(a) {\n  return a + 1;\n}\n\n"
        "function other() {\n  return 'o';\n}\n\n"
        f"{line}\n",
    )
    store, updater = _index(root)
    report = _move(root, store, updater, target="pkg/core.js")
    assert report.applied, report.message
    probe = _node(
        root,
        tmp_path,
        "import { plugins } from './pkg/util.js';\n"
        "import { helper } from './pkg/core.js';\n"
        "console.log(plugins.length, helper(1));\n",
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "1 2"


# --- relative imports inside the moved code keep their target ---------------------

NESTED = {
    "pkg/__init__.py": "",
    "pkg/deps.py": "X = 'pkg.deps'\n",
    "pkg/sub/__init__.py": "NAME = 'pkg.sub'\n",
    "pkg/sub/deps.py": "X = 'pkg.sub.deps'\n",
    "pkg/sub/inner/__init__.py": "",
    "pkg/sub/inner/mod.py": "X = 'pkg.sub.inner.mod'\n",
}


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param("from .deps import X\n    return X", "pkg.sub.deps", id="sibling"),
        pytest.param("from . import deps\n    return deps.X", "pkg.sub.deps", id="dot"),
        pytest.param("from .. import deps\n    return deps.X", "pkg.deps", id="dotdot"),
        pytest.param(
            "from .inner.mod import X\n    return X", "pkg.sub.inner.mod", id="deep"
        ),
        pytest.param("from . import NAME\n    return NAME", "pkg.sub", id="package"),
    ],
)
def test_a_relative_import_inside_the_moved_function_keeps_its_target(
    temp_repo: Path, body: str, expected: str
) -> None:
    """The function-local import travelled verbatim, and its dots now
    counted from the destination's package: `from .deps import X` moved
    from pkg/sub/util.py to pkg/core.py silently read `pkg.deps`."""
    fixture = {
        **NESTED,
        "pkg/sub/util.py": f"def helper():\n    {body}\n" + OTHER,
    }
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    report = _move(root, store, updater, qn=f"{PROJECT}.pkg.sub.util.helper")
    assert report.applied, report.message
    probe = _python(root, "from pkg.core import helper; print(helper())")
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == expected


def test_a_relative_import_above_the_root_refuses_the_move(temp_repo: Path) -> None:
    """It names no module the destination could spell."""
    fixture = dict(FIXTURE)
    fixture["pkg/util.py"] = (
        "def helper():\n    from .. import deps\n    return deps\n" + OTHER
    )
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    with pytest.raises(MoveRefused, match="from .. import deps"):
        _move(root, store, updater, target="pkg.sub.core")
    assert (root / "pkg/util.py").read_text() == fixture["pkg/util.py"]


# --- module state the moved code rebinds stays one variable -----------------------


@pytest.mark.parametrize(
    ("util", "probe"),
    [
        pytest.param(
            "COUNT = 0\n\n\ndef helper():\n    global COUNT\n    COUNT += 1\n"
            "    return COUNT\n" + OTHER,
            "import pkg.util\nfrom pkg.util import helper\n"
            "helper()\nprint(pkg.util.COUNT)\n",
            id="moved-writes",
        ),
        pytest.param(
            "COUNT = 0\n\n\ndef bump():\n    global COUNT\n    COUNT += 1\n\n\n"
            "def helper():\n    return COUNT\n" + OTHER,
            "from pkg.util import bump, helper\nbump()\nprint(helper())\n",
            id="old-module-writes",
        ),
    ],
)
def test_a_global_the_move_would_split_refuses_the_move(
    temp_repo: Path, util: str, probe: str
) -> None:
    """The destination imported COUNT by value, so `global COUNT` then
    rebound the destination's own copy: the moved helper counted in one
    module while every reader of `pkg.util.COUNT` saw the other."""
    fixture = {**FIXTURE, "pkg/util.py": util}
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    with pytest.raises(MoveRefused, match="COUNT"):
        _move(root, store, updater)
    assert (root / "pkg/util.py").read_text() == util
    assert not (root / "pkg/core.py").exists()
    result = _python(root, probe)
    assert result.stdout.strip() == "1", result.stderr


def test_a_global_only_the_moved_code_uses_does_not_refuse(temp_repo: Path) -> None:
    """A cache the moved function alone creates and reads moves with it."""
    fixture = dict(FIXTURE)
    fixture["pkg/util.py"] = (
        "def helper():\n    global _CACHE\n    _CACHE = 'warm'\n    return _CACHE\n"
        + OTHER
    )
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    report = _move(root, store, updater)
    assert report.applied, report.message
    probe = _python(root, "from pkg.core import helper; print(helper())")
    assert probe.stdout.strip() == "warm", probe.stderr


@needs_node
@pytest.mark.parametrize("write", ["count += 1;", "count++;", "count = count + 1;"])
def test_a_js_module_variable_the_moved_code_assigns_refuses_the_move(
    temp_repo: Path, tmp_path: Path, write: str
) -> None:
    """An imported binding is read-only, so the moved `count += 1` that
    updated the module's `let` threw a TypeError at the destination."""
    root = temp_repo / PROJECT
    util = (
        "export let count = 0;\n\n"
        f"export function helper() {{\n  {write}\n  return count;\n}}\n\n"
        "export function run() {\n  return 'r';\n}\n"
    )
    _write(root, "pkg/util.js", util)
    store, updater = _index(root)
    with pytest.raises(MoveRefused, match="count"):
        _move(root, store, updater, target="pkg/core.js")
    assert (root / "pkg/util.js").read_text() == util
    probe = _node(
        root,
        tmp_path,
        "import { helper } from './pkg/util.js';\nconsole.log(helper());\n",
    )
    assert probe.stdout.strip() == "1", probe.stderr


@needs_node
def test_a_js_local_that_shadows_a_module_variable_does_not_refuse(
    temp_repo: Path, tmp_path: Path
) -> None:
    root = temp_repo / PROJECT
    _write(
        root,
        "pkg/util.js",
        "export let count = 0;\n\n"
        "export function helper() {\n  let count = 1;\n  count += 1;\n  return count;\n}\n\n"
        "export function run() {\n  return count;\n}\n",
    )
    store, updater = _index(root)
    report = _move(root, store, updater, target="pkg/core.js")
    assert report.applied, report.message
    probe = _node(
        root,
        tmp_path,
        "import { helper } from './pkg/core.js';\nconsole.log(helper());\n",
    )
    assert probe.stdout.strip() == "2", probe.stderr


# --- a wildcard importer still reaches the moved name -----------------------------


@pytest.mark.parametrize(
    "util",
    [
        pytest.param("def helper(a):\n    return a + 1\n", id="last-definition"),
        pytest.param("def helper(a):\n    return a + 1\n" + OTHER, id="with-sibling"),
        pytest.param(
            "def helper(a):\n    return a + 1\n"
            + OTHER
            + "\n\n__all__ = [n for n in dir() if not n.startswith('_')]\n",
            id="computed-all",
        ),
    ],
)
def test_a_wildcard_reexport_of_the_moved_name_refuses_the_move(
    temp_repo: Path, util: str
) -> None:
    """`from pkg.util import *` is recorded with the name `*`, which neither
    the move rewrote nor the contract counted as reaching `helper`: the
    move passed, and `from pkg.api import helper` raised ImportError."""
    fixture = {
        "pkg/__init__.py": "",
        "pkg/util.py": util,
        "pkg/api.py": "from pkg.util import *  # noqa: F403\n",
    }
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    with pytest.raises(MoveRefused, match="pkg/api.py"):
        _move(root, store, updater)
    assert (root / "pkg/util.py").read_text() == util
    probe = _python(root, "from pkg.api import helper; print(helper(1))")
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "2"


@pytest.mark.parametrize(
    ("util", "keep_alias"),
    [
        pytest.param(
            "def helper(a):\n    return a + 1\n\n\nVALUE = helper(1)\n",
            False,
            id="old-module-imports-it-back",
        ),
        pytest.param("def helper(a):\n    return a + 1\n", True, id="keep-alias"),
        pytest.param(
            "def _helper(a):\n    return a + 1\n" + OTHER, False, id="private-name"
        ),
    ],
)
def test_a_wildcard_reexport_that_still_reaches_the_name_does_not_refuse(
    temp_repo: Path, util: str, keep_alias: bool
) -> None:
    """When the old module binds the name again, or the star never exported
    it, the wildcard importer sees what it saw before."""
    name = "_helper" if "_helper" in util else "helper"
    fixture = {
        "pkg/__init__.py": "",
        "pkg/util.py": util,
        "pkg/api.py": "from pkg.util import *  # noqa: F403\n",
    }
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    report = _move(
        root, store, updater, qn=f"{PROJECT}.pkg.util.{name}", keep_alias=keep_alias
    )
    assert report.applied, report.message
    source = "pkg.util" if name == "_helper" else "pkg.api"
    probe = _python(root, f"from {source} import {name}; print({name}(1))")
    if name == "_helper":
        # Only the old path changed; the wildcard never carried it.
        assert probe.returncode != 0
        probe = _python(root, "from pkg.core import _helper; print(_helper(1))")
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "2"


def test_the_contract_counts_a_wildcard_importer_of_a_vacated_module(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The contract's own check, with the planner's refusal out of the way:
    the stale-importer exemption cleared any import whose names held
    neither "" nor the moved name, and `*` holds neither."""
    monkeypatch.setattr(
        move_mod.Mover, "_refuse_wildcard_loss", lambda *_args, **_kwargs: None
    )
    util = "def helper(a):\n    return a + 1\n"
    fixture = {
        "pkg/__init__.py": "",
        "pkg/util.py": util,
        "pkg/api.py": "from pkg.util import *  # noqa: F403\n",
    }
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    report = _move(root, store, updater)
    assert not report.applied
    assert "pkg/api.py" in report.message, report.message
    assert (root / "pkg/util.py").read_text() == util
    probe = _python(root, "from pkg.api import helper; print(helper(1))")
    assert probe.stdout.strip() == "2", probe.stderr
