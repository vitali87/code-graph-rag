# Python `@typing.overload` stubs (issue #2590). A stub has no runtime
# existence: the implementation that follows rebinds the name, so every call
# runs the implementation. Indexing each stub as its own definition gave the
# FIRST stub the bare qualified name (click's `decorators.command` answered
# `cgr graph definition` with a one-line `...`), pushed the implementation to
# `command@168`, and fanned every call out to all N+1 nodes as `overload`,
# which also made `rename` refuse. Stubs are now folded into the
# implementation the way C/C++ header prototypes are dropped beside their
# definition (issue #893); a stub with no implementation keeps its node.
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.editing.rename import RenameRefused, rename
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.py.overloads import (
    folded_overload_stubs,
    overload_stub_names,
)
from codebase_rag.tests.conftest import create_and_run_updater
from codebase_rag.tests.test_edit_contract import PROJECT, _real_project

# (import line, decorator) for every way a module spells typing's overload.
OVERLOAD_SPELLINGS = [
    pytest.param("import typing as t", "@t.overload", id="click-t-alias"),
    pytest.param("import typing", "@typing.overload", id="typing-dotted"),
    pytest.param("from typing import overload", "@overload", id="from-typing"),
    pytest.param("from typing import overload as ovl", "@ovl", id="from-typing-alias"),
    pytest.param(
        "import typing_extensions",
        "@typing_extensions.overload",
        id="typing-extensions-dotted",
    ),
    pytest.param(
        "import typing_extensions as te", "@te.overload", id="typing-extensions-alias"
    ),
    pytest.param(
        "from typing_extensions import overload",
        "@overload",
        id="from-typing-extensions",
    ),
]

FUNCTION_MODULE = """{imports}


{deco}
def command(name: str) -> int: ...


{deco}
def command(name: None) -> int: ...


def command(name=None):
    def decorator(f):
        return f

    return decorator


def use():
    return command("x")
"""

METHOD_MODULE = """{imports}


class Box:
    {deco}
    def get(self, key: int) -> int: ...

    {deco}
    def get(self, key: str) -> str: ...

    def get(self, key):
        return key


def use():
    return Box().get(1)
"""


def _write(root: Path, text: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "deco.py").write_text(text, encoding="utf-8")
    return root


def _line_of(text: str, needle: str) -> int:
    return text.splitlines().index(needle) + 1


def _defs(mock: MagicMock, label: cs.NodeLabel) -> dict[str, dict]:
    return {
        str(c.args[1][cs.KEY_QUALIFIED_NAME]): dict(c.args[1])
        for c in mock.ensure_node_batch.call_args_list
        if str(c.args[0]) == label.value
    }


def _edges(mock: MagicMock, rel: str) -> set[tuple[str, str, str | None]]:
    out: set[tuple[str, str, str | None]] = set()
    for c in mock.ensure_relationship_batch.call_args_list:
        if str(c.args[1]) != rel:
            continue
        props = (
            c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {}) or {}
        )
        resolution = props.get(cs.KEY_RESOLUTION)
        out.add(
            (
                str(c.args[0][2]),
                str(c.args[2][2]),
                str(resolution) if resolution is not None else None,
            )
        )
    return out


def _named[V](defs: dict[str, V], stem: str) -> dict[str, V]:
    # Every node for `stem`, its @line variants included.
    return {
        qn: props
        for qn, props in defs.items()
        if qn == stem or qn.startswith(f"{stem}{cs.DUP_QN_MARKER}")
    }


def _calls_from(mock: MagicMock, caller_suffix: str) -> set[tuple[str, str | None]]:
    return {
        (dst, how)
        for src, dst, how in _edges(mock, cs.RelationshipType.CALLS.value)
        if src.endswith(caller_suffix)
    }


# --- the implementation owns the bare name --------------------------------------


@pytest.mark.parametrize(("imports", "deco"), OVERLOAD_SPELLINGS)
def test_module_function_implementation_takes_the_bare_name(
    temp_repo: Path, mock_ingestor: MagicMock, imports: str, deco: str
) -> None:
    text = FUNCTION_MODULE.format(imports=imports, deco=deco)
    root = _write(temp_repo / "ovl", text)
    create_and_run_updater(root, mock_ingestor)

    functions = _defs(mock_ingestor, cs.NodeLabel.FUNCTION)
    command = _named(functions, "ovl.deco.command")
    assert list(command) == ["ovl.deco.command"], sorted(command)
    impl = command["ovl.deco.command"]
    assert impl[cs.KEY_START_LINE] == _line_of(text, "def command(name=None):")
    assert impl[cs.KEY_END_LINE] == _line_of(text, "    return decorator")
    assert impl[cs.KEY_DECORATORS] == []

    # One exact edge to the implementation, not one `overload` edge per stub.
    assert _calls_from(mock_ingestor, ".deco.use") == {
        ("ovl.deco.command", cs.EdgeResolution.EXACT.value)
    }
    # The nested def hangs under the implementation, which now owns the name.
    defines = _edges(mock_ingestor, cs.RelationshipType.DEFINES.value)
    assert ("ovl.deco.command", "ovl.deco.command.decorator", None) in defines


@pytest.mark.parametrize(("imports", "deco"), OVERLOAD_SPELLINGS)
def test_method_implementation_takes_the_bare_name(
    temp_repo: Path, mock_ingestor: MagicMock, imports: str, deco: str
) -> None:
    text = METHOD_MODULE.format(imports=imports, deco=deco)
    root = _write(temp_repo / "ovm", text)
    create_and_run_updater(root, mock_ingestor)

    methods = _named(_defs(mock_ingestor, cs.NodeLabel.METHOD), "ovm.deco.Box.get")
    assert list(methods) == ["ovm.deco.Box.get"], sorted(methods)
    impl = methods["ovm.deco.Box.get"]
    assert impl[cs.KEY_START_LINE] == _line_of(text, "    def get(self, key):")
    assert impl[cs.KEY_DECORATORS] == []
    assert _calls_from(mock_ingestor, ".deco.use") >= {
        ("ovm.deco.Box.get", cs.EdgeResolution.EXACT.value)
    }
    assert not any(
        dst.startswith(f"ovm.deco.Box.get{cs.DUP_QN_MARKER}")
        for dst, _how in _calls_from(mock_ingestor, ".deco.use")
    )


def test_static_and_class_method_overloads_fold_too(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `@overload` stacked over another decorator is still a stub; the
    # implementation keeps its own `@staticmethod` / `@classmethod`.
    text = (
        "from typing import overload\n"
        "\n"
        "\n"
        "class Parser:\n"
        "    @overload\n"
        "    @staticmethod\n"
        "    def parse(raw: str) -> str: ...\n"
        "\n"
        "    @overload\n"
        "    @staticmethod\n"
        "    def parse(raw: bytes) -> bytes: ...\n"
        "\n"
        "    @staticmethod\n"
        "    def parse(raw):\n"
        "        return raw\n"
        "\n"
        "    @classmethod\n"
        "    @overload\n"
        "    def build(cls, raw: str) -> str: ...\n"
        "\n"
        "    @classmethod\n"
        "    @overload\n"
        "    def build(cls, raw: bytes) -> bytes: ...\n"
        "\n"
        "    @classmethod\n"
        "    def build(cls, raw):\n"
        "        return cls.parse(raw)\n"
    )
    root = _write(temp_repo / "ovs", text)
    create_and_run_updater(root, mock_ingestor)

    methods = _defs(mock_ingestor, cs.NodeLabel.METHOD)
    parse = _named(methods, "ovs.deco.Parser.parse")
    build = _named(methods, "ovs.deco.Parser.build")
    assert list(parse) == ["ovs.deco.Parser.parse"], sorted(parse)
    assert list(build) == ["ovs.deco.Parser.build"], sorted(build)
    assert parse["ovs.deco.Parser.parse"][cs.KEY_DECORATORS] == ["@staticmethod"]
    assert build["ovs.deco.Parser.build"][cs.KEY_DECORATORS] == ["@classmethod"]
    assert parse["ovs.deco.Parser.parse"][cs.KEY_START_LINE] == _line_of(
        text, "    def parse(raw):"
    )
    build_calls = _calls_from(mock_ingestor, ".Parser.build")
    assert {dst for dst, _how in build_calls} == {"ovs.deco.Parser.parse"}
    assert ("ovs.deco.Parser.parse", cs.EdgeResolution.EXACT.value) in build_calls


def test_an_uncalled_overloaded_function_is_one_dead_candidate(
    temp_repo: Path,
) -> None:
    # Dead code saw one candidate per stub; the stubs are not functions. The
    # name is private so the public-API root rule does not keep it alive.
    root = temp_repo / PROJECT
    root.mkdir()
    util = (
        FUNCTION_MODULE.format(imports="import typing as t", deco="@t.overload")
        .replace("def command(", "def _command(")
        .replace('    return command("x")\n', "    return 1\n")
    )
    store, _updater = _real_project(root, {"pkg/__init__.py": "", "pkg/util.py": util})
    config = default_dead_code_config(include_tests=False, include_classes=False)
    dead = {
        str(row[cs.KEY_QUALIFIED_NAME]): row.get(cs.KEY_START_LINE)
        for row in collect_dead_code(store, PROJECT, config)
    }
    assert _named(dead, f"{PROJECT}.pkg.util._command") == {
        f"{PROJECT}.pkg.util._command": _line_of(util, "def _command(name=None):")
    }, dead


# --- rename rewrites the stubs with the implementation -----------------------------


RENAME_UTIL = """import typing as t


@t.overload
def command(name: str) -> int: ...


@t.overload
def command(name: None) -> int: ...


def command(name=None):
    return 1 if name is None else 2


class Other:
    def command(self):
        return 3
"""

RENAME_APP = """from pkg.util import command


def run():
    return command() + command("x")
"""


@pytest.mark.parametrize(
    "before",
    [
        pytest.param(RENAME_UTIL, id="typing-alias"),
        pytest.param(
            RENAME_UTIL.replace(
                "import typing as t\n",
                "import typing as t\n\nt = None\n\nimport typing as t\n",
            ),
            id="rebound-then-reimported-from-typing",
        ),
        pytest.param(RENAME_UTIL + "\n\nt = None\n", id="rebound-after-the-stubs"),
        pytest.param(
            RENAME_UTIL + "\n\ndef _peek():\n    global t\n    return t\n",
            id="read-only-global-in-a-helper",
        ),
    ],
)
def test_rename_rewrites_every_stub_with_the_implementation(
    temp_repo: Path, before: str
) -> None:
    root = temp_repo / PROJECT
    root.mkdir()
    store, updater = _real_project(
        root,
        {"pkg/__init__.py": "", "pkg/util.py": before, "pkg/app.py": RENAME_APP},
    )
    report = rename(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.util.command",
        "cmd",
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.ambiguous == ()
    assert report.verdict is not None, report.verdict
    assert report.verdict.ok, report.verdict
    util = (root / "pkg" / "util.py").read_text(encoding="utf-8")
    # Both stubs and the implementation, never the unrelated method.
    assert util.count("def cmd(") == 3, util
    assert "def command(self):" in util
    assert "\ndef command(" not in util
    app = (root / "pkg" / "app.py").read_text(encoding="utf-8")
    assert app == RENAME_APP.replace("command", "cmd")
    ran = subprocess.run(
        [sys.executable, "-c", "from pkg.app import run; print(run())"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert ran.stdout.strip() == "3", ran.stderr


def test_renaming_a_same_named_method_leaves_the_stubs_alone(
    temp_repo: Path,
) -> None:
    root = temp_repo / PROJECT
    root.mkdir()
    store, updater = _real_project(
        root,
        {"pkg/__init__.py": "", "pkg/util.py": RENAME_UTIL, "pkg/app.py": RENAME_APP},
    )
    report = rename(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.util.Other.command",
        "cmd",
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    util = (root / "pkg" / "util.py").read_text(encoding="utf-8")
    assert util == RENAME_UTIL.replace("def command(self):", "def cmd(self):")


# --- negative: what must stay exactly as it was -------------------------------------


NOT_TYPING_OVERLOAD = [
    pytest.param(
        "def overload(fn):\n    return fn\n",
        "@overload",
        id="local-overload-function",
    ),
    pytest.param("import mylib as t", "@t.overload", id="t-is-not-typing"),
    pytest.param("from mylib import overload", "@overload", id="overload-from-mylib"),
    pytest.param("import typing", "@typing.no_type_check", id="other-typing-decorator"),
    pytest.param("import functools", "@functools.cache", id="ordinary-decorator"),
]

# typing's overload is imported, then its root name is rebound before the
# stubs, so the decorator calls whatever the rebinding put there.
REBOUND_OVERLOAD = [
    pytest.param(
        "from typing import overload\n\n\ndef overload(fn):\n    return fn\n",
        "@overload",
        id="import-then-local-def",
    ),
    pytest.param(
        "from typing import overload\n\n\nclass overload:\n    pass\n",
        "@overload",
        id="import-then-local-class",
    ),
    pytest.param(
        "import typing as t\n\nt = something\n", "@t.overload", id="alias-reassigned"
    ),
    pytest.param(
        "from typing import overload\n\noverload: object = something\n",
        "@overload",
        id="annotated-reassignment",
    ),
    pytest.param(
        "from typing import overload\nfrom mylib import overload\n",
        "@overload",
        id="reimported-from-another-module",
    ),
    pytest.param(
        "from typing import overload\nimport mylib as overload\n",
        "@overload",
        id="module-imported-as-overload",
    ),
    pytest.param(
        "from typing import overload\n\nfor overload in (identity,):\n    pass\n",
        "@overload",
        id="loop-target",
    ),
    pytest.param(
        "from typing import overload\n\n"
        "for _key, (_first, *overload) in registry.items():\n    pass\n",
        "@overload",
        id="unpacked-loop-target",
    ),
]

# Rebound inside the body of an `if`, `for`, `while`, `with`, `try` or `match`
# before the stubs. Those bodies are no scope of their own: the assignment
# binds the module's name, so past that statement the decorator may be the
# rebinding's (on one branch or one iteration is enough), and folding a
# definition that may be real would drop it from the graph.
COMPOUND_BODY_REBINDINGS = [
    pytest.param(
        "from typing import overload\n\n"
        "for item in items:\n    if item:\n        overload = item\n",
        "@overload",
        id="if-in-a-for-body",
    ),
    pytest.param(
        "from typing import overload\n\nfor item in items:\n    overload = item\n",
        "@overload",
        id="for-body",
    ),
    pytest.param(
        "from typing import overload\n\n"
        "while pending:\n    if ready:\n        overload = identity\n",
        "@overload",
        id="if-in-a-while-body",
    ),
    pytest.param(
        "from typing import overload\n\n"
        "with registry:\n    if ready:\n        overload = identity\n",
        "@overload",
        id="if-in-a-with-body",
    ),
    pytest.param(
        "from typing import overload\n\ntry:\n    pass\n"
        "except LookupError:\n    if ready:\n        overload = identity\n",
        "@overload",
        id="if-in-an-except-body",
    ),
    pytest.param(
        "try:\n    from typing import overload\nexcept ImportError:\n"
        "    def overload(fn):\n        return fn\n",
        "@overload",
        id="local-fallback-in-an-except-body",
    ),
    pytest.param(
        "from typing import overload\n\n"
        "if ready:\n    pass\nelif pending:\n    pass\n"
        "else:\n    overload = identity\n",
        "@overload",
        id="else-branch",
    ),
    pytest.param(
        "from typing import overload\n\n"
        "match mode:\n    case 1:\n        overload = identity\n",
        "@overload",
        id="case-body",
    ),
    pytest.param(
        "import typing as t\n\n"
        "for item in items:\n    while item:\n        with registry:\n"
        "            try:\n                t = item\n            finally:\n"
        "                pass\n",
        "@t.overload",
        id="alias-four-blocks-deep",
    ),
]

# Rebound by a binding form other than `name = ...`: every name an unpacking
# target, chained or augmented assignment, walrus, `with`/`except ... as`,
# match capture, `type` alias or `del` names is rebound in its scope.
TARGET_REBINDINGS = [
    pytest.param(
        "from typing import overload\n\noverload, other = identity, 1\n",
        "@overload",
        id="tuple-unpacking",
    ),
    pytest.param(
        "from typing import overload\n\n[overload] = [identity]\n",
        "@overload",
        id="list-unpacking",
    ),
    pytest.param(
        "from typing import overload\n\n(overload,) = (identity,)\n",
        "@overload",
        id="parenthesized-unpacking",
    ),
    pytest.param(
        "from typing import overload\n\n*rest, overload = registry\n",
        "@overload",
        id="starred-unpacking",
    ),
    pytest.param(
        "from typing import overload\n\n"
        "(first, (second, [*rest, overload])) = registry\n",
        "@overload",
        id="nested-unpacking",
    ),
    pytest.param(
        "import typing as t\n\nt, other = registry\n",
        "@t.overload",
        id="alias-unpacking",
    ),
    pytest.param(
        "from typing import overload\n\nalias = overload = identity\n",
        "@overload",
        id="chained-assignment",
    ),
    pytest.param(
        "from typing import overload\n\noverload |= registry\n",
        "@overload",
        id="augmented-assignment",
    ),
    pytest.param(
        "from typing import overload\n\nif (overload := identity):\n    pass\n",
        "@overload",
        id="walrus-in-a-condition",
    ),
    pytest.param(
        "from typing import overload\n\n"
        "hooks = [overload := hook for hook in registry]\n",
        "@overload",
        id="walrus-in-a-comprehension",
    ),
    pytest.param(
        "from typing import overload\n\nwith registry as overload:\n    pass\n",
        "@overload",
        id="with-as",
    ),
    pytest.param(
        "from typing import overload\n\n"
        "with registry as (first, overload):\n    pass\n",
        "@overload",
        id="with-as-tuple",
    ),
    pytest.param(
        "from typing import overload\n\n"
        "try:\n    pass\nexcept LookupError as overload:\n    pass\n",
        "@overload",
        id="except-as",
    ),
    pytest.param(
        "from typing import overload\nfrom mylib import hook as overload\n",
        "@overload",
        id="imported-as-overload",
    ),
    pytest.param(
        "from typing import overload\nimport overload.helpers\n",
        "@overload",
        id="dotted-import-binds-its-package",
    ),
    pytest.param(
        "from typing import overload\n\n"
        "match registry:\n    case [first, *overload]:\n        pass\n",
        "@overload",
        id="match-capture",
    ),
    pytest.param(
        "from typing import overload\n\ntype overload = Callable\n",
        "@overload",
        id="type-alias",
    ),
    pytest.param(
        "from typing import overload\n\ndel overload\n",
        "@overload",
        id="del",
    ),
]

# Rebound through a `global` declaration: the declaring function (or class
# body) binds the module's name, by any binding form, whenever it runs.
DECLARED_REBINDINGS = [
    pytest.param(
        "from typing import overload\n\n\n"
        "def reset():\n    global overload\n    overload = identity\n",
        "@overload",
        id="global-then-assigned",
    ),
    pytest.param(
        "from typing import overload\n\n\ndef reset():\n    global overload\n"
        "    if ready:\n        overload, other = identity, 1\n",
        "@overload",
        id="global-then-unpacked-in-an-if",
    ),
    pytest.param(
        "from typing import overload\n\n\ndef reset():\n    if ready:\n"
        "        global overload\n\n    def overload(fn):\n        return fn\n",
        "@overload",
        id="global-in-an-if-then-def",
    ),
    pytest.param(
        "from typing import overload\n\n\n"
        "def reset():\n    global overload\n    import mylib as overload\n",
        "@overload",
        id="global-then-imported",
    ),
    pytest.param(
        "from typing import overload\n\n\ndef reset():\n    global overload\n"
        "    for overload in hooks:\n        pass\n",
        "@overload",
        id="global-then-loop-target",
    ),
    pytest.param(
        "from typing import overload\n\n\n"
        "class Holder:\n    global overload\n    overload = identity\n",
        "@overload",
        id="global-in-a-class-body",
    ),
]

EVERY_REBINDING = [
    *REBOUND_OVERLOAD,
    *COMPOUND_BODY_REBINDINGS,
    *TARGET_REBINDINGS,
    *DECLARED_REBINDINGS,
]


@pytest.mark.parametrize(("imports", "deco"), [*NOT_TYPING_OVERLOAD, *EVERY_REBINDING])
def test_a_decorator_that_is_not_typing_overload_keeps_every_definition(
    temp_repo: Path, mock_ingestor: MagicMock, imports: str, deco: str
) -> None:
    # Same-named redefinitions under any other decorator are real rebinds
    # and keep today's `name` / `name@line` nodes and fan-out.
    text = FUNCTION_MODULE.format(imports=imports, deco=deco)
    root = _write(temp_repo / "neg", text)
    create_and_run_updater(root, mock_ingestor)

    command = _named(_defs(mock_ingestor, cs.NodeLabel.FUNCTION), "neg.deco.command")
    first = _line_of(text, "def command(name: str) -> int: ...")
    second = _line_of(text, "def command(name: None) -> int: ...")
    impl = _line_of(text, "def command(name=None):")
    assert {qn: props[cs.KEY_START_LINE] for qn, props in command.items()} == {
        "neg.deco.command": first,
        f"neg.deco.command{cs.DUP_QN_MARKER}{second}": second,
        f"neg.deco.command{cs.DUP_QN_MARKER}{impl}": impl,
    }
    assert {dst for dst, _how in _calls_from(mock_ingestor, ".deco.use")} == set(
        command
    )


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            METHOD_MODULE.format(
                imports=REBOUND_OVERLOAD[0].values[0], deco="@overload"
            ),
            id="rebound-at-module-level",
        ),
        pytest.param(
            METHOD_MODULE.format(
                imports="from typing import overload", deco="@overload"
            ).replace(
                "class Box:\n",
                "class Box:\n    def overload(fn):\n        return fn\n\n",
            ),
            id="rebound-in-the-class-body",
        ),
    ],
)
def test_a_rebound_overload_keeps_every_method(
    temp_repo: Path, mock_ingestor: MagicMock, text: str
) -> None:
    # A method's decorators see the class body, then the module.
    root = _write(temp_repo / "rbm", text)
    create_and_run_updater(root, mock_ingestor)

    get = _named(_defs(mock_ingestor, cs.NodeLabel.METHOD), "rbm.deco.Box.get")
    assert len(get) == 3, sorted(get)
    assert "rbm.deco.Box.get" in get


# typing's overload IS the binding live at the stubs, though the same name is
# rebound somewhere: re-imported from typing after the rebinding, rebound only
# after the stubs, or rebound inside another scope.
LIVE_AT_THE_STUBS = [
    pytest.param(
        FUNCTION_MODULE.format(
            imports=(
                "from typing import overload\n\n\ndef overload(fn):\n    return fn\n"
                "\n\nfrom typing import overload"
            ),
            deco="@overload",
        ),
        id="reimported-from-typing",
    ),
    pytest.param(
        FUNCTION_MODULE.format(
            imports="import typing as t\n\nt = something\n\nimport typing as t",
            deco="@t.overload",
        ),
        id="alias-reimported-from-typing",
    ),
    pytest.param(
        FUNCTION_MODULE.format(imports="from typing import overload", deco="@overload")
        + "\n\ndef overload(fn):\n    return fn\n",
        id="rebound-after-the-stubs",
    ),
    pytest.param(
        FUNCTION_MODULE.format(
            imports=(
                "from typing import overload\n\n\nclass Holder:\n"
                "    overload = something\n\n\ndef helper():\n"
                "    overload = something\n    return overload"
            ),
            deco="@overload",
        ),
        id="rebound-in-other-scopes",
    ),
    pytest.param(
        FUNCTION_MODULE.format(
            imports=(
                "from typing import overload\n\nfor item in items:\n    pass\n\n"
                "for holder.overload in items:\n    pass"
            ),
            deco="@overload",
        ),
        id="loops-binding-other-names",
    ),
    pytest.param(
        FUNCTION_MODULE.format(
            imports=(
                "from typing import overload\n\n\ndef helper():\n"
                "    for item in items:\n        if item:\n"
                "            overload, other = item, 1\n"
                "    with registry as (first, overload):\n        pass\n"
                "    return [overload := hook for hook in registry]"
            ),
            deco="@overload",
        ),
        id="compound-and-unpacking-rebinds-in-a-function",
    ),
    pytest.param(
        FUNCTION_MODULE.format(
            imports=(
                "from typing import overload\n\n\nclass Holder:\n"
                "    if ready:\n        overload = identity\n"
                "    [overload] = [identity]"
            ),
            deco="@overload",
        ),
        id="compound-and-unpacking-rebinds-in-a-class-body",
    ),
    pytest.param(
        FUNCTION_MODULE.format(
            imports=(
                "from typing import overload\n\nfor item in items:\n"
                "    if item:\n        first, *rest = item\n"
                "        holder.overload = item\n        table[overload] = item\n"
                "    with registry as (first, holder.overload):\n        pass"
            ),
            deco="@overload",
        ),
        id="compound-bodies-binding-other-names",
    ),
    pytest.param(
        FUNCTION_MODULE.format(
            imports=(
                "try:\n    from typing import overload\n"
                "except ImportError:\n    from typing_extensions import overload"
            ),
            deco="@overload",
        ),
        id="typing-on-every-branch",
    ),
    pytest.param(
        FUNCTION_MODULE.format(
            imports=(
                "from typing import TYPE_CHECKING\n\n"
                "if TYPE_CHECKING:\n    from typing import overload"
            ),
            deco="@overload",
        ),
        id="imported-under-type-checking",
    ),
    pytest.param(
        FUNCTION_MODULE.format(
            imports=(
                "from typing import overload\n\nfor item in items:\n"
                "    if item:\n        overload, other = item, 1\n\n"
                "from typing import overload"
            ),
            deco="@overload",
        ),
        id="reimported-after-a-compound-rebinding",
    ),
    pytest.param(
        FUNCTION_MODULE.format(
            imports=(
                "from typing import overload\n\n\n"
                "def helper():\n    global overload\n    return overload"
            ),
            deco="@overload",
        ),
        id="read-only-global-in-a-helper",
    ),
    pytest.param(
        FUNCTION_MODULE.format(
            imports=(
                "from typing import overload\n\n\ndef helper():\n"
                "    global overload, counter\n    counter = 1\n    return overload"
            ),
            deco="@overload",
        ),
        id="global-helper-rebinding-another-name",
    ),
    pytest.param(
        FUNCTION_MODULE.format(
            imports=(
                "from typing import overload\n\n\ndef outer():\n"
                "    overload = identity\n\n    def inner():\n"
                "        nonlocal overload\n        return overload\n\n"
                "    return inner"
            ),
            deco="@overload",
        ),
        id="read-only-nonlocal-in-a-closure",
    ),
]


@pytest.mark.parametrize("text", LIVE_AT_THE_STUBS)
def test_a_typing_overload_live_at_the_stubs_still_folds(
    temp_repo: Path, mock_ingestor: MagicMock, text: str
) -> None:
    root = _write(temp_repo / "live", text)
    create_and_run_updater(root, mock_ingestor)

    command = _named(_defs(mock_ingestor, cs.NodeLabel.FUNCTION), "live.deco.command")
    assert list(command) == ["live.deco.command"], sorted(command)
    assert command["live.deco.command"][cs.KEY_START_LINE] == _line_of(
        text, "def command(name=None):"
    )
    assert _calls_from(mock_ingestor, ".deco.use") == {
        ("live.deco.command", cs.EdgeResolution.EXACT.value)
    }


def _parse(text: str) -> Node:
    parsers, _queries = load_parsers()
    return parsers[cs.SupportedLanguage.PYTHON].parse(text.encode()).root_node


@pytest.mark.parametrize(
    ("text", "stubs"),
    [
        *(
            pytest.param(
                FUNCTION_MODULE.format(imports=p.values[0], deco=p.values[1]),
                0,
                id=f"rebound-{p.id}",
            )
            for p in EVERY_REBINDING
        ),
        *(pytest.param(p.values[0], 2, id=f"live-{p.id}") for p in LIVE_AT_THE_STUBS),
    ],
)
def test_indexing_and_rename_read_the_same_binding(text: str, stubs: int) -> None:
    # The fold the graph applies and the stubs a rename rewrites come from
    # one binding-aware reading of the decorators.
    root = _parse(text)
    line = _line_of(text, "def command(name=None):") - 1
    token = root.descendant_for_point_range((line, 4), (line, 4))
    assert token is not None
    assert token.parent is not None
    names = overload_stub_names(token.parent, root)
    assert len(folded_overload_stubs(root)) == stubs
    assert [name.start_point[0] for name in names] == [
        _line_of(text, "def command(name: None) -> int: ...") - 1,
        _line_of(text, "def command(name: str) -> int: ...") - 1,
    ][:stubs]


LOOP_BODY_STUBS = """from typing import overload

for {target} in (identity,):
    @overload
    def command(name: str) -> int: ...

    @overload
    def command(name: None) -> int: ...

    def command(name=None):
        return name
"""


@pytest.mark.parametrize(
    ("target", "stubs"),
    [
        pytest.param("overload", 0, id="the-loop-rebinds-overload"),
        pytest.param("item", 2, id="the-loop-binds-another-name"),
    ],
)
def test_a_loop_body_sees_its_own_target(target: str, stubs: int) -> None:
    # The target is bound before the body runs, so a decorator in the body
    # reads the loop's value, not typing's.
    root = _parse(LOOP_BODY_STUBS.format(target=target))
    assert len(folded_overload_stubs(root)) == stubs


LOOP_BODY_REBOUND = """from typing import overload

for item in items:
{before}
    @overload
    def command(name: str) -> int: ...

    @overload
    def command(name: None) -> int: ...

    def command(name=None):
        return name
{after}"""


@pytest.mark.parametrize(
    ("before", "after", "stubs"),
    [
        pytest.param(
            "    if item:\n        overload = item\n",
            "",
            0,
            id="rebound-in-an-if-before-the-stubs",
        ),
        pytest.param(
            "    with registry as (first, overload):\n        pass\n",
            "",
            0,
            id="rebound-by-a-with-target-before-the-stubs",
        ),
        pytest.param(
            "",
            "    if item:\n        overload, other = item, 1\n",
            0,
            id="rebound-after-the-stubs-for-the-next-iteration",
        ),
        pytest.param(
            "    if item:\n        other, *rest = item\n",
            "    holder.overload = item\n",
            2,
            id="other-names-rebound-around-the-stubs",
        ),
    ],
)
def test_a_rebinding_anywhere_in_a_loop_reaches_its_stubs(
    before: str, after: str, stubs: int
) -> None:
    # The body runs again after its last statement, so a rebinding anywhere
    # in it (in a nested `if` too) is live at every stub in it.
    text = LOOP_BODY_REBOUND.format(before=before, after=after)
    root = _parse(text)
    assert len(folded_overload_stubs(root)) == stubs
    line = _line_of(text, "    def command(name=None):") - 1
    token = root.descendant_for_point_range((line, 8), (line, 8))
    assert token is not None
    assert token.parent is not None
    assert len(overload_stub_names(token.parent, root)) == stubs


@pytest.mark.parametrize(
    ("pattern", "stubs"),
    [
        pytest.param("overload", 0, id="bare-capture"),
        pytest.param("(overload)", 0, id="parenthesized-capture"),
        pytest.param("[first, *overload]", 0, id="star-capture"),
        pytest.param("{'k': overload}", 0, id="mapping-value-capture"),
        pytest.param("{**overload}", 0, id="mapping-rest-capture"),
        pytest.param("Hook(k=overload)", 0, id="keyword-capture"),
        pytest.param("Hook() as overload", 0, id="as-capture"),
        pytest.param("overload.KIND", 2, id="dotted-value-pattern"),
        pytest.param("Hook(overload=1)", 2, id="keyword-name"),
        pytest.param("Hook(first, *rest)", 2, id="other-captures"),
    ],
)
def test_a_case_pattern_rebinds_only_what_it_captures(pattern: str, stubs: int) -> None:
    # A `case` binds its captures in the enclosing scope; a dotted value
    # pattern and a class pattern's keyword names bind nothing.
    imports = (
        "from typing import overload\n\n"
        f"match registry:\n    case {pattern}:\n        pass"
    )
    root = _parse(FUNCTION_MODULE.format(imports=imports, deco="@overload"))
    assert len(folded_overload_stubs(root)) == stubs


# Stubs inside a function body: `outer`'s own, and those of a class that a
# method body defines. A def's body runs when it is called, so a decorator in
# it reads `overload` as the enclosing scopes bind it THEN; a module rebinding
# anywhere, after the def too, may be the one in force.
NESTED_STUBS_MODULE = """from typing import overload


def outer():
{local}
    @overload
    def command(name: str) -> int: ...

    @overload
    def command(name: None) -> int: ...

    def command(name=None):
        return name

    return command("x")


class Holder:
    def method(self):
        class Box:
            @overload
            def get(self, key: int) -> int: ...

            @overload
            def get(self, key: str) -> str: ...

            def get(self, key):
                return key

        return Box().get(1)
{after}"""

# (outer's body prefix, module tail, `command` nodes, `Box.get` nodes)
NESTED_STUBS = [
    pytest.param("", "", 1, 1, id="only-typing"),
    pytest.param(
        "", "\n\ndef overload(fn):\n    return fn\n", 3, 3, id="def-after-the-defs"
    ),
    pytest.param("", "\n\noverload = identity\n", 3, 3, id="assigned-after-the-defs"),
    pytest.param(
        "", "\n\noverload, other = identity, 1\n", 3, 3, id="unpacked-after-the-defs"
    ),
    pytest.param(
        "",
        "\n\nif ready:\n    overload = identity\n",
        3,
        3,
        id="rebound-in-an-if-after-the-defs",
    ),
    pytest.param(
        "",
        "\n\ndef setup():\n    global overload\n    overload = identity\n",
        3,
        3,
        id="rebound-through-global",
    ),
    pytest.param(
        "",
        "\n\ndef helper():\n    global overload\n    return overload\n",
        1,
        1,
        id="read-only-global",
    ),
    # `rebind` rebinds `outer`'s own `overload` through `nonlocal`; such a
    # rebinding is honoured module-wide, which errs toward keeping `Box`'s
    # definitions apart too.
    pytest.param(
        "    from typing import overload\n\n    def rebind():\n"
        "        nonlocal overload\n        overload = identity\n\n    rebind()\n",
        "",
        3,
        3,
        id="rebound-through-nonlocal",
    ),
    pytest.param(
        "    from typing import overload\n\n    def peek():\n"
        "        nonlocal overload\n        return overload\n",
        "",
        1,
        1,
        id="read-only-nonlocal",
    ),
    pytest.param(
        "    from typing import overload\n",
        "\n\noverload = identity\n",
        1,
        3,
        id="a-local-import-shadows-the-module",
    ),
    pytest.param(
        "",
        "\n\nfrom typing import overload\n",
        1,
        1,
        id="reimported-from-typing-after-the-defs",
    ),
    pytest.param(
        "",
        "\n\ndef helper():\n    overload = identity\n    return overload\n",
        1,
        1,
        id="rebound-in-another-function",
    ),
]


@pytest.mark.parametrize(("local", "after", "commands", "gets"), NESTED_STUBS)
def test_stubs_in_a_function_body_see_every_module_binding(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    local: str,
    after: str,
    commands: int,
    gets: int,
) -> None:
    text = NESTED_STUBS_MODULE.format(local=local, after=after)
    root = _write(temp_repo / "nest", text)
    create_and_run_updater(root, mock_ingestor)

    command = _named(
        _defs(mock_ingestor, cs.NodeLabel.FUNCTION), "nest.deco.outer.command"
    )
    get = _named(
        _defs(mock_ingestor, cs.NodeLabel.METHOD), "nest.deco.Holder.method.Box.get"
    )
    assert len(command) == commands, sorted(command)
    assert len(get) == gets, sorted(get)
    assert "nest.deco.outer.command" in command


@pytest.mark.parametrize(("local", "after", "commands", "gets"), NESTED_STUBS)
def test_nested_stubs_fold_and_rename_alike(
    local: str, after: str, commands: int, gets: int
) -> None:
    # Indexing and rename read the nested stubs through the same binding.
    text = NESTED_STUBS_MODULE.format(local=local, after=after)
    root = _parse(text)
    # One node left means its two stubs folded; three, that none did.
    command_stubs = 2 if commands == 1 else 0
    get_stubs = 2 if gets == 1 else 0
    assert len(folded_overload_stubs(root)) == command_stubs + get_stubs
    line = _line_of(text, "    def command(name=None):") - 1
    token = root.descendant_for_point_range((line, 8), (line, 8))
    assert token is not None
    assert token.parent is not None
    assert len(overload_stub_names(token.parent, root)) == command_stubs


RENAME_NESTED_UTIL = """import typing as t


def make():
    @t.overload
    def command(name: str) -> int: ...

    @t.overload
    def command(name: None) -> int: ...

    def command(name=None):
        return 1 if name is None else 2

    return command() + command("x")
"""


@pytest.mark.parametrize(
    "util",
    [
        pytest.param(
            RENAME_NESTED_UTIL + "\n\nt = None\n", id="alias-reassigned-after"
        ),
        pytest.param(
            RENAME_NESTED_UTIL
            + "\n\nfor _item in (None,):\n    t, _other = _item, 1\n",
            id="alias-unpacked-in-a-loop-after",
        ),
    ],
)
def test_rename_of_a_nested_duplicate_rebound_later_still_refuses(
    temp_repo: Path, util: str
) -> None:
    # `make`'s decorators run when it is called, after `t` is rebound, so
    # its three `command` defs are real rebinds: the rename must refuse
    # rather than rewrite two of them as stubs.
    root = temp_repo / PROJECT
    root.mkdir()
    store, _updater = _real_project(root, {"pkg/__init__.py": "", "pkg/util.py": util})
    with pytest.raises(RenameRefused):
        rename(
            root, store.fetch_all, PROJECT, f"{PROJECT}.pkg.util.make.command", "cmd"
        )
    assert (root / "pkg" / "util.py").read_text(encoding="utf-8") == util


def test_rename_of_a_nested_overloaded_function_rewrites_its_stubs(
    temp_repo: Path,
) -> None:
    root = temp_repo / PROJECT
    root.mkdir()
    store, updater = _real_project(
        root, {"pkg/__init__.py": "", "pkg/util.py": RENAME_NESTED_UTIL}
    )
    report = rename(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.util.make.command",
        "cmd",
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    util = (root / "pkg" / "util.py").read_text(encoding="utf-8")
    assert util.count("def cmd(") == 3, util
    assert "def command(" not in util


def test_overload_stubs_without_an_implementation_keep_their_nodes(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A Protocol (like a header prototype with no definition) declares
    # overloads with no implementation; they are all there is to point at.
    text = (
        "from typing import Protocol, overload\n"
        "\n"
        "\n"
        "class Reader(Protocol):\n"
        "    @overload\n"
        "    def read(self, n: int) -> bytes: ...\n"
        "\n"
        "    @overload\n"
        "    def read(self, n: None) -> str: ...\n"
    )
    root = _write(temp_repo / "proto", text)
    create_and_run_updater(root, mock_ingestor)

    read = _named(_defs(mock_ingestor, cs.NodeLabel.METHOD), "proto.deco.Reader.read")
    second = _line_of(text, "    def read(self, n: None) -> str: ...")
    assert set(read) == {
        "proto.deco.Reader.read",
        f"proto.deco.Reader.read{cs.DUP_QN_MARKER}{second}",
    }


def test_a_stub_after_the_implementation_is_not_folded_into_it(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The implementation must FOLLOW its stubs; a stub written after the last
    # plain def rebinds the name and has no implementation of its own.
    text = (
        "from typing import overload\n"
        "\n"
        "\n"
        "def f(x):\n"
        "    return x\n"
        "\n"
        "\n"
        "@overload\n"
        "def f(x: int) -> int: ...\n"
    )
    root = _write(temp_repo / "late", text)
    create_and_run_updater(root, mock_ingestor)

    f = _named(_defs(mock_ingestor, cs.NodeLabel.FUNCTION), "late.deco.f")
    stub = _line_of(text, "def f(x: int) -> int: ...")
    assert set(f) == {"late.deco.f", f"late.deco.f{cs.DUP_QN_MARKER}{stub}"}


def test_a_single_plain_function_is_unchanged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    text = (
        "import functools\n"
        "from typing import overload\n"
        "\n"
        "\n"
        "def solo(x):\n"
        "    return x\n"
        "\n"
        "\n"
        "@functools.cache\n"
        "def cached(x):\n"
        "    return x\n"
        "\n"
        "\n"
        "def use():\n"
        "    return solo(1) + cached(2)\n"
    )
    root = _write(temp_repo / "plain", text)
    create_and_run_updater(root, mock_ingestor)

    functions = _defs(mock_ingestor, cs.NodeLabel.FUNCTION)
    assert {
        qn: (props[cs.KEY_START_LINE], props[cs.KEY_DECORATORS])
        for qn, props in functions.items()
    } == {
        "plain.deco.solo": (5, []),
        "plain.deco.cached": (10, ["@functools.cache"]),
        "plain.deco.use": (14, []),
    }
    assert _calls_from(mock_ingestor, ".deco.use") == {
        ("plain.deco.solo", cs.EdgeResolution.EXACT.value),
        ("plain.deco.cached", cs.EdgeResolution.EXACT.value),
    }


def test_a_pyi_stub_beside_the_module_changes_nothing(
    temp_repo: Path,
) -> None:
    # `.pyi` files are not indexed, so a stub file next to a module (the
    # other place overloads live) must leave the module's graph as it was.
    text = "def helper(x):\n    return x\n\n\ndef use():\n    return helper(1)\n"
    stub = (
        "from typing import overload\n\n"
        "@overload\ndef helper(x: int) -> int: ...\n"
        "@overload\ndef helper(x: str) -> str: ...\n"
    )
    graphs = []
    for name, with_stub in (("bare", False), ("stubbed", True)):
        root = _write(temp_repo / name / "pkg", text)
        if with_stub:
            (root / "deco.pyi").write_text(stub, encoding="utf-8")
        mock = MagicMock()
        create_and_run_updater(root, mock)
        graphs.append(
            (
                {
                    qn.removeprefix("pkg.")
                    for label in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD)
                    for qn in _defs(mock, label)
                },
                {
                    (src.removeprefix("pkg."), dst.removeprefix("pkg."), how)
                    for src, dst, how in _edges(mock, cs.RelationshipType.CALLS.value)
                },
            )
        )
    assert graphs[0] == graphs[1]
    assert graphs[0][0] == {"deco.helper", "deco.use"}


@pytest.mark.parametrize(
    "util",
    [
        pytest.param(
            RENAME_UTIL.replace("import typing as t", "import mylib as t"),
            id="t-is-not-typing",
        ),
        pytest.param(
            RENAME_UTIL.replace("import typing as t", "import typing as t\n\nt = None"),
            id="alias-reassigned",
        ),
        pytest.param(
            RENAME_UTIL.replace(
                "import typing as t",
                "from typing import overload\n\n\ndef overload(fn):\n    return fn",
            ).replace("@t.overload", "@overload"),
            id="import-then-local-def",
        ),
        pytest.param(
            RENAME_UTIL.replace(
                "import typing as t",
                "import typing as t\n\nfor t in (None,):\n    pass",
            ),
            id="alias-rebound-by-a-loop",
        ),
        pytest.param(
            RENAME_UTIL.replace(
                "import typing as t",
                "import typing as t\n\nfor _item in (None,):\n"
                "    if _item is None:\n        t = None",
            ),
            id="alias-rebound-in-a-loop-body-if",
        ),
        pytest.param(
            RENAME_UTIL.replace(
                "import typing as t", "import typing as t\n\nt, _other = None, 1"
            ),
            id="alias-rebound-by-unpacking",
        ),
        pytest.param(
            RENAME_UTIL + "\n\ndef _reset():\n    global t\n    t = None\n",
            id="alias-rebound-through-global",
        ),
    ],
)
def test_rename_of_a_plain_duplicate_still_refuses_as_overload(
    temp_repo: Path, util: str
) -> None:
    # A non-typing `overload` (or typing's, rebound before the stubs) leaves
    # the fan-out, so the rename still sees ambiguous sites and refuses rather
    # than guessing; it never rewrites the real definitions as stubs.
    root = temp_repo / PROJECT
    root.mkdir()
    store, _updater = _real_project(
        root, {"pkg/__init__.py": "", "pkg/util.py": util, "pkg/app.py": RENAME_APP}
    )
    with pytest.raises(RenameRefused):
        rename(root, store.fetch_all, PROJECT, f"{PROJECT}.pkg.util.command", "cmd")
    assert (root / "pkg" / "util.py").read_text(encoding="utf-8") == util
