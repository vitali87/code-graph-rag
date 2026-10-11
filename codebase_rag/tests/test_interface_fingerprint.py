# The interface fingerprint (issue #2531) decides whether a re-parsed file's
# callers may keep their edges, so each rule is pinned from both sides: an
# edit no cross-file reader can see keeps the fingerprint, and an edit one
# can see changes it.
from __future__ import annotations

import pytest

from codebase_rag import constants as cs
from codebase_rag.interface_skip import stable_interface_modules
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.interface_fingerprint import interface_fingerprint
from codebase_rag.types_defs import ResultRow

PY = cs.SupportedLanguage.PYTHON
JAVA = cs.SupportedLanguage.JAVA
TS = cs.SupportedLanguage.TS

PY_BASE = """class Widget:
    def size(self) -> int:
        return 2


def make():
    return Widget()


def helper(x: int) -> int:
    y = x + 1
    return y + 1


def log_event(name):
    message = "event " + name
    print(message)
"""

JAVA_BASE = """package p;

public class A {
    private int count;

    public int twice(int x) {
        int y = x * 2;
        return y;
    }

    public B make() {
        return new B();
    }
}
"""

TS_BASE = """export function helper(x: number): number {
  return x + 1;
}

export function logEvent(name: string): void {
  const message = "event " + name;
  console.log(message);
}
"""


def _fingerprint(language: cs.SupportedLanguage, source: str) -> str | None:
    _parsers, queries = load_parsers()
    parser = queries[language][cs.KEY_PARSER]
    assert parser is not None
    return interface_fingerprint(parser.parse(source.encode()).root_node, language)


def _same(language: cs.SupportedLanguage, before: str, after: str) -> bool:
    return _fingerprint(language, before) == _fingerprint(language, after)


@pytest.mark.parametrize(
    ("language", "before", "after"),
    [
        (PY, PY_BASE, PY_BASE + "# trailing\n"),
        (PY, PY_BASE, "# header\n\n" + PY_BASE),
        (PY, PY_BASE, PY_BASE.replace("return 2", "return 2  # two")),
        (PY, PY_BASE, PY_BASE.replace("    print(message)", "    print(message, 1)")),
        (
            PY,
            PY_BASE,
            PY_BASE.replace("    y = x + 1\n", '    """Doc."""\n    y = x * 3\n'),
        ),
        # A returned arithmetic expression infers no type, whatever it is.
        (PY, PY_BASE, PY_BASE.replace("return y + 1", "return y - 7")),
        (JAVA, JAVA_BASE, JAVA_BASE + "// trailing\n"),
        (JAVA, JAVA_BASE, JAVA_BASE.replace("int y = x * 2;", "int y = x << 1; y++;")),
        (JAVA, JAVA_BASE, JAVA_BASE.replace("return y;", "/* doubled */ return y;")),
        (TS, TS_BASE, TS_BASE + "// trailing\n"),
        (TS, TS_BASE, TS_BASE.replace("console.log(message);", "if (message) {}")),
    ],
    ids=[
        "py-trailing-comment",
        "py-leading-comment-moves-every-line",
        "py-inline-comment",
        "py-procedure-body",
        "py-docstring-and-local",
        "py-uninferable-return",
        "java-trailing-comment",
        "java-method-body",
        "java-block-comment-in-body",
        "ts-trailing-comment",
        "ts-procedure-body",
    ],
)
def test_an_edit_no_caller_reads_keeps_the_fingerprint(
    language: cs.SupportedLanguage, before: str, after: str
) -> None:
    assert _same(language, before, after)


PY_PROCEDURE = "def setup(app):\n    app.ready()\n"
PY_HOLDER = "class Holder:\n    def fill(self, value):\n        x = value\n"

CHANGED: list[tuple[str, cs.SupportedLanguage, str, str]] = [
    ("py-rename", PY, PY_BASE, PY_BASE.replace("def helper(", "def helper2(")),
    (
        "py-parameter",
        PY,
        PY_BASE,
        PY_BASE.replace("helper(x: int)", "helper(x: int, y: int = 0)"),
    ),
    ("py-annotation", PY, PY_BASE, PY_BASE.replace("helper(x: int)", "helper(x: str)")),
    (
        "py-return-annotation",
        PY,
        PY_BASE,
        PY_BASE.replace("-> int:\n    y", "-> str:\n    y"),
    ),
    # Return-type inference reads a returned call...
    (
        "py-returned-call",
        PY,
        PY_BASE,
        PY_BASE.replace("return Widget()", "return Other()"),
    ),
    # ...and a returned local's every binding.
    (
        "py-returned-local",
        PY,
        "def make():\n    w = Widget()\n    return w\n",
        "def make():\n    w = Gadget()\n    return w\n",
    ),
    (
        "py-attribute-assignment",
        PY,
        PY_HOLDER,
        PY_HOLDER.replace("x = value", "self.x = value"),
    ),
    (
        "py-attribute-assignment-value",
        PY,
        PY_HOLDER.replace("x = value", "self.x = Widget()"),
        PY_HOLDER.replace("x = value", "self.x = Gadget()"),
    ),
    (
        "py-dunder-body",
        PY,
        "class A:\n    def __init__(self):\n        pass\n",
        "class A:\n    def __init__(self):\n        print(1)\n",
    ),
    (
        "py-global",
        PY,
        PY_PROCEDURE,
        "def setup(app):\n    global READY\n    app.ready()\n",
    ),
    (
        "py-function-level-import",
        PY,
        PY_PROCEDURE,
        "def setup(app):\n    import os\n    app.ready()\n",
    ),
    (
        "py-generator",
        PY,
        "def items():\n    pass\n",
        "def items():\n    yield 1\n",
    ),
    (
        "py-callable-parameter",
        PY,
        "def run(callback):\n    print(callback)\n",
        "def run(callback):\n    callback()\n",
    ),
    (
        "py-nested-definition",
        PY,
        "def outer():\n    def inner():\n        pass\n",
        "def outer():\n    def inner2():\n        pass\n",
    ),
    (
        "py-nested-return-of-a-local",
        PY,
        "def outer():\n    w = Widget()\n\n    def inner():\n        return w\n",
        "def outer():\n    w = Gadget()\n\n    def inner():\n        return w\n",
    ),
    ("py-module-level", PY, PY_BASE, PY_BASE + "\nVALUE = 1\n"),
    (
        "py-all",
        PY,
        PY_BASE + '\n__all__ = ["helper"]\n',
        PY_BASE + '\n__all__ = ["make"]\n',
    ),
    (
        "py-base-class",
        PY,
        "class A(B):\n    pass\n",
        "class A(C):\n    pass\n",
    ),
    (
        "py-moved-definition",
        PY,
        "def a():\n    pass\n\n\ndef b():\n    pass\n",
        "def b():\n    pass\n\n\ndef a():\n    pass\n",
    ),
    ("java-rename", JAVA, JAVA_BASE, JAVA_BASE.replace("twice(", "doubled(")),
    ("java-parameter", JAVA, JAVA_BASE, JAVA_BASE.replace("int x)", "long x)")),
    (
        "java-return-type",
        JAVA,
        JAVA_BASE,
        JAVA_BASE.replace("public B make", "public C make"),
    ),
    (
        "java-field",
        JAVA,
        JAVA_BASE,
        JAVA_BASE.replace("private int count", "private long count"),
    ),
    (
        "java-modifier",
        JAVA,
        JAVA_BASE,
        JAVA_BASE.replace("public int twice", "private int twice"),
    ),
    (
        "java-overload",
        JAVA,
        JAVA_BASE,
        JAVA_BASE.replace(
            "    public B make",
            "    public int twice(long x) { return 1; }\n\n    public B make",
        ),
    ),
    (
        "java-anonymous-class-in-body",
        JAVA,
        JAVA_BASE,
        JAVA_BASE.replace(
            "int y = x * 2;",
            "Runnable r = new Runnable() { public void run() {} }; int y = x * 2;",
        ),
    ),
    (
        "ts-rename",
        TS,
        TS_BASE,
        TS_BASE.replace("function helper(", "function helper2("),
    ),
    ("ts-return-value", TS, TS_BASE, TS_BASE.replace("return x + 1;", "return x - 1;")),
    (
        "ts-member-assignment",
        TS,
        TS_BASE,
        TS_BASE.replace("console.log(message);", "exports.extra = message;"),
    ),
    (
        "ts-require",
        TS,
        TS_BASE,
        TS_BASE.replace("console.log(message);", "require('./other');"),
    ),
]


@pytest.mark.parametrize(
    ("language", "before", "after"),
    [case[1:] for case in CHANGED],
    ids=[case[0] for case in CHANGED],
)
def test_an_edit_a_caller_can_read_changes_the_fingerprint(
    language: cs.SupportedLanguage, before: str, after: str
) -> None:
    assert not _same(language, before, after)


def test_a_language_without_rules_has_no_fingerprint() -> None:
    _parsers, queries = load_parsers()
    parser = queries[cs.SupportedLanguage.GO][cs.KEY_PARSER]
    assert parser is not None
    tree = parser.parse(b"package main\n\nfunc main() {}\n")
    assert interface_fingerprint(tree.root_node, cs.SupportedLanguage.GO) is None


def test_the_restored_relationships_are_ones_the_inbound_capture_reads() -> None:
    """A dependent keeps its edges only through CYPHER_INBOUND_EDGES; a
    relationship this set allows and that query does not capture would be
    lost with the re-parsed file's subtree."""
    captured = cs.CYPHER_INBOUND_EDGES.split("[r:", 1)[1].split("]", 1)[0]
    assert {rel.value for rel in cs.INTERFACE_RESTORED_RELS} <= set(captured.split("|"))


def _module_row(path: str, qn: str, fingerprint: str | None) -> ResultRow:
    return {
        cs.KEY_PATH: path,
        cs.KEY_QUALIFIED_NAME: qn,
        cs.KEY_INTERFACE_FINGERPRINT: fingerprint,
    }


def test_a_module_with_nothing_to_compare_is_never_stable() -> None:
    """No stored fingerprint (a graph indexed before it existed, or one read
    back from protobuf) and a path the sync did not fingerprint both mean
    there is no evidence the interface held."""
    rows = [
        _module_row("a.py", "proj.a", None),
        _module_row("b.py", "proj.b", None),
        _module_row("c.py", "proj.c", "same"),
    ]
    assert stable_interface_modules(rows, {"a.py": "new", "c.py": "same"}, []) == {
        "c.py": "proj.c"
    }


def test_a_longer_projects_module_at_the_same_path_is_not_ours() -> None:
    rows = [
        _module_row("a.py", "proj.sub.a", "same"),
        _module_row("a.py", "proj.a", "same"),
    ]
    assert stable_interface_modules(rows, {"a.py": "same"}, ["proj.sub"]) == {
        "a.py": "proj.a"
    }
    # Two modules claiming one path, with nothing to tell them apart.
    assert stable_interface_modules(rows, {"a.py": "same"}, []) == {}
