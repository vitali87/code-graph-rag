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

pytestmark = pytest.mark.usefixtures("both_interface_walks")

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


# A body that hands functions to other files through call arguments: a
# constructor field (read by the callable-field pre-pass) and a forwarded
# callback (read by the callable-flow pass).
PY_BINDS = """def setup():
    label = "x"
    spec = Spec(handler=a, retries=3)
    total = 1 + 2
    relay(a, label)
    print("ready", total, spec)
"""

TS_BINDS = """export function setup(): void {
  const label = 'x';
  relay(a, label);
  console.log('ready', label);
}
"""


# What a body returns, read by return-type inference and the
# returned-callable reader: off no local, a construction of one (the
# constructor alone is read), and off a parameter (typed by the signature).
PY_RETURNS_DIRECT = 'def make():\n    print("direct")\n    return A().run\n'
PY_RETURNS_BUILT = "def make():\n    data = load()\n    return Response(data)\n"
PY_RETURNS_PARAM = 'def make(r: A):\n    print("x")\n    return r.run\n'
PY_RETURNS_LOCAL = "def make():\n    r = A()\n    return r.run\n"
TS_RETURNS_LOCAL = (
    "export function make() {\n  const r = new A();\n  return r.run;\n}\n"
)


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
        (
            TS,
            TS_BASE,
            TS_BASE.replace(
                "console.log(message);",
                "if (message) {\n    console.log(message);\n  }",
            ),
        ),
        (PY, PY_BINDS, PY_BINDS.replace('"ready"', '"set up"')),
        (PY, PY_BINDS, PY_BINDS.replace("total = 1 + 2", "total = 2 * 3 - 1")),
        (PY, PY_BINDS, PY_BINDS.replace("retries=3", "retries=4")),
        (PY, PY_BINDS, PY_BINDS.replace('label = "x"', 'label = "y"')),
        (PY, PY_BINDS, PY_BINDS.replace("    total =", "    # note\n    total =")),
        (TS, TS_BINDS, TS_BINDS.replace("'ready'", "'set up'")),
        (TS, TS_BINDS, TS_BINDS.replace("const label = 'x'", "let label = 'y'")),
        (PY, PY_RETURNS_DIRECT, PY_RETURNS_DIRECT.replace('"direct"', '"made"')),
        (PY, PY_RETURNS_BUILT, PY_RETURNS_BUILT.replace("load()", "fetch()")),
        (PY, PY_RETURNS_PARAM, PY_RETURNS_PARAM.replace('"x"', '"y"')),
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
        "py-binding-body-log-string",
        "py-binding-body-local-arithmetic",
        "py-binding-body-literal-argument",
        "py-binding-body-local-argument-value",
        "py-binding-body-comment-outside-arguments",
        "ts-binding-body-log-string",
        "ts-binding-body-local-argument-value",
        "py-returned-method-off-no-local",
        "py-returned-construction-of-a-local",
        "py-returned-method-of-a-parameter",
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
    # A constructor argument binds the class's field for every caller of
    # `spec.handler()`, and a forwarded callable binds the function that
    # calls it: what the call names, by position or keyword, and the class
    # or function it calls.
    (
        "py-constructor-keyword-argument",
        PY,
        PY_BINDS,
        PY_BINDS.replace("handler=a", "handler=b"),
    ),
    (
        "py-constructor-positional-argument",
        PY,
        PY_BINDS.replace("Spec(handler=a", "Spec(a"),
        PY_BINDS.replace("Spec(handler=a", "Spec(b"),
    ),
    (
        "py-argument-keyword",
        PY,
        PY_BINDS,
        PY_BINDS.replace("handler=a", "callback=a"),
    ),
    ("py-constructed-class", PY, PY_BINDS, PY_BINDS.replace("Spec(", "Other(")),
    (
        "py-argument-position",
        PY,
        PY_BINDS,
        PY_BINDS.replace("relay(a, label)", "relay(label, a)"),
    ),
    # The collector numbers arguments as the argument list holds them.
    (
        "py-comment-between-arguments",
        PY,
        PY_BINDS,
        PY_BINDS.replace(
            "relay(a, label)", "relay(\n        # first\n        a, label)"
        ),
    ),
    (
        "py-attribute-argument",
        PY,
        "class C:\n    def go(self):\n        relay(self.a)\n",
        "class C:\n    def go(self):\n        relay(self.b)\n",
    ),
    # A local of the same name makes the argument the local's, which binds
    # nothing; a closure's local does the same for a nested def.
    (
        "py-argument-shadowed-by-a-local",
        PY,
        PY_BINDS,
        PY_BINDS.replace("    relay(a, label)", "    a = 1\n    relay(a, label)"),
    ),
    (
        "py-argument-shadowed-by-an-enclosing-local",
        PY,
        "def outer():\n    def inner():\n        relay(a)\n",
        "def outer():\n    a = 1\n\n    def inner():\n        relay(a)\n",
    ),
    # A receiver or an argument read through a local is typed by the body.
    (
        "py-local-receiver",
        PY,
        "def go():\n    r = Relay()\n    r.relay(a)\n",
        "def go():\n    r = Quiet()\n    r.relay(a)\n",
    ),
    (
        "py-attribute-of-a-local-argument",
        PY,
        "def go():\n    o = make()\n    relay(o.handle)\n",
        "def go():\n    o = other()\n    relay(o.handle)\n",
    ),
    ("ts-argument", TS, TS_BINDS, TS_BINDS.replace("relay(a,", "relay(b,")),
    (
        "ts-bound-argument",
        TS,
        TS_BINDS.replace("relay(a,", "relay(a.bind(null),"),
        TS_BINDS.replace("relay(a,", "relay(b.bind(null),"),
    ),
    (
        "ts-cast-argument",
        TS,
        TS_BINDS.replace("relay(a,", "relay(a as any,"),
        TS_BINDS.replace("relay(a,", "relay(b as any,"),
    ),
    (
        "ts-constructor-argument",
        TS,
        TS_BINDS.replace("relay(a, label)", "new Spec(a)"),
        TS_BINDS.replace("relay(a, label)", "new Spec(b)"),
    ),
    (
        "ts-removed-call",
        TS,
        TS_BASE,
        TS_BASE.replace("console.log(message);", "if (message) {}"),
    ),
    (
        "ts-local-receiver",
        TS,
        "export function go(): void {\n  const r = new Relay();\n  r.relay(a);\n}\n",
        "export function go(): void {\n  const r = new Quiet();\n  r.relay(a);\n}\n",
    ),
    # A symbol-keyed installation types `recv[kImpl]` everywhere, through
    # whatever the body declared its value as.
    (
        "ts-symbol-installation",
        TS,
        "export function install(): void {\n  const host = { [kImpl]: new Impl() };\n}\n",
        "export function install(): void {\n  const host = { [kImpl]: new Other() };\n}\n",
    ),
    (
        "ts-symbol-installation-through-a-local",
        TS,
        "export function install(): void {\n  const impl = new Impl();\n"
        "  const host = { [kImpl]: impl };\n}\n",
        "export function install(): void {\n  const impl = new Other();\n"
        "  const host = { [kImpl]: impl };\n}\n",
    ),
    # A returned or called member of a local is typed by the local's
    # binding, which the body alone holds.
    (
        "py-returned-method-of-a-local",
        PY,
        PY_RETURNS_LOCAL,
        PY_RETURNS_LOCAL.replace("A()", "B()"),
    ),
    (
        "py-returned-call-on-a-local",
        PY,
        PY_RETURNS_LOCAL.replace("r.run", "r.build()"),
        PY_RETURNS_LOCAL.replace("r.run", "r.build()").replace("A()", "B()"),
    ),
    (
        "py-callee-indexed-by-a-local",
        PY,
        "def go():\n    k = 'x'\n    handlers[k].run(a)\n",
        "def go():\n    k = 'y'\n    handlers[k].run(a)\n",
    ),
    (
        "ts-returned-method-of-a-local",
        TS,
        TS_RETURNS_LOCAL,
        TS_RETURNS_LOCAL.replace("new A()", "new B()"),
    ),
    (
        "ts-returned-bound-method-of-a-local",
        TS,
        TS_RETURNS_LOCAL.replace("r.run;", "r.run.bind(r);"),
        TS_RETURNS_LOCAL.replace("r.run;", "r.run.bind(r);").replace("A()", "B()"),
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
