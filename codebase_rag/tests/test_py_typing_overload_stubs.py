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

from codebase_rag import constants as cs
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.editing.rename import RenameRefused, rename
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


def test_rename_rewrites_every_stub_with_the_implementation(temp_repo: Path) -> None:
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
        f"{PROJECT}.pkg.util.command",
        "cmd",
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.ambiguous == ()
    assert report.verdict is not None and report.verdict.ok, report.verdict
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


@pytest.mark.parametrize(("imports", "deco"), NOT_TYPING_OVERLOAD)
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


def test_rename_of_a_plain_duplicate_still_refuses_as_overload(
    temp_repo: Path,
) -> None:
    # A non-typing `overload` leaves the fan-out, so the rename still sees
    # ambiguous sites and refuses rather than guessing.
    root = temp_repo / PROJECT
    root.mkdir()
    util = RENAME_UTIL.replace("import typing as t", "import mylib as t")
    store, _updater = _real_project(
        root, {"pkg/__init__.py": "", "pkg/util.py": util, "pkg/app.py": RENAME_APP}
    )
    with pytest.raises(RenameRefused):
        rename(root, store.fetch_all, PROJECT, f"{PROJECT}.pkg.util.command", "cmd")
    assert (root / "pkg" / "util.py").read_text(encoding="utf-8") == util
