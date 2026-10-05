# A Clang thread-safety or attribute macro written AFTER a C++ declarator
# (`bool Check() const LOCKS_REQUIRED(mu) { ... }`: Abseil, gRPC, Chromium,
# LLVM, RocksDB, googletest/gmock) is not C++ syntax to tree-sitter, which
# never sees the #define. Its error recovery closes the declaration before
# the macro and parses `LOCKS_REQUIRED(mu) { ... }` as a type-less function
# definition named after the macro. The method's body calls were credited
# to a `Box.LOCKS_REQUIRED` node that does not exist, and a free function
# lost its body to the artifact (issue #2552).
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.cpp.preproc_recovery import parse_with_preproc_recovery
from codebase_rag.parsers.cpp_frontend import cpp_frontend_available
from codebase_rag.tests.conftest import run_updater

PROJECT = "proj"

# The issue's minimal repro (`clang++ -fsyntax-only` accepts it).
BOX_H = """\
#pragma once
#define LOCKS_REQUIRED(mu)
#define MUST_USE_RESULT

struct Mutex { void AssertHeld() const {} };
extern Mutex mu;

class Box {
 public:
  bool Check() const LOCKS_REQUIRED(mu) { mu.AssertHeld(); return helper(); }
  bool Plain() const                    { mu.AssertHeld(); return helper(); }
  int Count() const MUST_USE_RESULT     { return helper() ? 1 : 0; }
 private:
  bool helper() const { return true; }
};
int run(const Box& b) LOCKS_REQUIRED(mu);
"""

BOX_CPP = """\
#include "box.h"
Mutex mu;
int run(const Box& b) LOCKS_REQUIRED(mu) { return b.Check() ? b.Count() : 0; }
"""

LOCKS_H = """\
#pragma once
#define LOCKS_REQUIRED(mu)
#define LOCKS_EXCLUDED(mu)
#define GUARDED_BY(mu)
#define PT_GUARDED_BY(mu)
#define NOEXCEPT_MACRO

struct Mutex { void AssertHeld() const {} };
extern Mutex mu;
void x();
void y();
"""

MACRO_NAMES = (
    "LOCKS_REQUIRED",
    "LOCKS_EXCLUDED",
    "GUARDED_BY",
    "PT_GUARDED_BY",
    "NOEXCEPT_MACRO",
    "MUST_USE_RESULT",
)


class _Graph:
    def __init__(self, ingestor: MagicMock) -> None:
        self.defs: dict[str, str] = {}
        for c in ingestor.ensure_node_batch.call_args_list:
            label = str(c.args[0])
            if label in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD):
                self.defs[c.args[1][cs.KEY_QUALIFIED_NAME]] = label
        self.calls: set[tuple[str, str]] = {
            (c.args[0][2], c.args[2][2])
            for c in ingestor.ensure_relationship_batch.call_args_list
            if c.args[1] == cs.RelationshipType.CALLS
        }

    def qn(self, suffix: str) -> str:
        # A lone header's module is `proj.c`, one sharing a stem with a
        # source file is `proj.c.h`; match on the in-module path instead.
        matches = [qn for qn in self.defs if qn.endswith(f"{cs.SEPARATOR_DOT}{suffix}")]
        assert len(matches) == 1, (suffix, sorted(self.defs))
        return matches[0]

    def callees(self, caller_suffix: str) -> set[str]:
        caller_qn = self.qn(caller_suffix)
        return {callee for caller, callee in self.calls if caller == caller_qn}

    def has(self, suffix: str) -> bool:
        return any(qn.endswith(f"{cs.SEPARATOR_DOT}{suffix}") for qn in self.defs)

    def macro_named(self) -> list[str]:
        # Any definition, caller or callee named after an annotation macro is
        # the recovery artifact standing in for the real function.
        qns = set(self.defs) | {qn for edge in self.calls for qn in edge}
        return sorted(
            qn for qn in qns if qn.rsplit(cs.SEPARATOR_DOT, 1)[-1] in MACRO_NAMES
        )


def _index(temp_repo: Path, files: dict[str, str], compdb: bool = False) -> _Graph:
    root = temp_repo / PROJECT
    root.mkdir()
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    if compdb:
        entries = [
            {
                "directory": str(root),
                "arguments": ["c++", "-std=c++17", f"-I{root}", str(root / name)],
                "file": str(root / name),
            }
            for name in files
            if name.endswith(".cpp")
        ]
        (root / "compile_commands.json").write_text(
            json.dumps(entries), encoding="utf-8"
        )
    ingestor = MagicMock()
    run_updater(root, ingestor)
    return _Graph(ingestor)


def _class_header(members: str, bases: str = "") -> str:
    return (
        LOCKS_H
        + """
class Base {
 public:
  virtual ~Base() {}
  virtual bool Check() const { return false; }
  virtual void f() const {}
};

class C"""
        + bases
        + """ {
 public:
"""
        + members
        + """
};
"""
    )


@pytest.mark.parametrize(
    "member",
    [
        pytest.param(
            "  bool Check() const LOCKS_REQUIRED(mu) { mu.AssertHeld(); return h(); }",
            id="same-line",
        ),
        pytest.param(
            "  bool Check() const\n"
            "      LOCKS_REQUIRED(mu) {\n"
            "    mu.AssertHeld();\n"
            "    return h();\n"
            "  }",
            id="macro-on-next-line-gmock",
        ),
        pytest.param(
            "  bool Check(int v) const LOCKS_REQUIRED(mu) { mu.AssertHeld(); return h(); }",
            id="named-parameter",
        ),
        pytest.param(
            "  bool Check() const LOCKS_REQUIRED(mu) LOCKS_EXCLUDED(other) {\n"
            "    mu.AssertHeld(); return h(); }",
            id="two-macros",
        ),
        pytest.param(
            "  bool Check() const  // caller holds mu\n"
            "      LOCKS_REQUIRED(mu) { mu.AssertHeld(); return h(); }",
            id="comment-before-macro",
        ),
        pytest.param(
            "  bool Check() const override LOCKS_REQUIRED(mu) {\n"
            "    mu.AssertHeld(); return h(); }",
            id="override-then-macro",
        ),
        pytest.param(
            "  bool Check() const LOCKS_REQUIRED(mu) override {\n"
            "    mu.AssertHeld(); return h(); }",
            id="macro-then-override",
        ),
        pytest.param(
            "  bool Check() const final LOCKS_REQUIRED(mu) {\n"
            "    mu.AssertHeld(); return h(); }",
            id="final-then-macro",
        ),
        pytest.param(
            "  bool Check() const NOEXCEPT_MACRO { mu.AssertHeld(); return h(); }",
            id="macro-without-arguments",
        ),
    ],
)
def test_method_with_trailing_macro_keeps_its_calls(
    temp_repo: Path, member: str
) -> None:
    members = member + "\n  bool h() const { return true; }"
    graph = _index(temp_repo, {"c.h": _class_header(members, " : public Base")})

    assert graph.defs[graph.qn("C.Check")] == cs.NodeLabel.METHOD
    assert graph.callees("C.Check") >= {
        graph.qn("Mutex.AssertHeld"),
        graph.qn("C.h"),
    }, sorted(graph.calls)
    assert not graph.macro_named(), graph.macro_named()


def test_issue_repro_methods_and_free_function_keep_their_calls(
    temp_repo: Path,
) -> None:
    graph = _index(temp_repo, {"box.h": BOX_H, "box.cpp": BOX_CPP})

    assert graph.callees("Box.Check") == {
        graph.qn("Mutex.AssertHeld"),
        graph.qn("Box.helper"),
    }, sorted(graph.calls)
    assert graph.callees("box.run") == {
        graph.qn("Box.Check"),
        graph.qn("Box.Count"),
    }, sorted(graph.calls)
    assert not graph.macro_named(), graph.macro_named()


@pytest.mark.parametrize(
    "declaration",
    [
        pytest.param("  void f() const LOCKS_REQUIRED(mu);", id="const-macro"),
        pytest.param(
            "  void f() const override LOCKS_REQUIRED(mu);", id="override-macro"
        ),
        pytest.param(
            "  void f() const LOCKS_REQUIRED(mu) override;", id="macro-override"
        ),
        pytest.param("  void f() const NOEXCEPT_MACRO;", id="macro-without-arguments"),
    ],
)
def test_declared_method_with_trailing_macro_is_named_after_itself(
    temp_repo: Path, declaration: str
) -> None:
    graph = _index(temp_repo, {"c.h": _class_header(declaration, " : public Base")})

    assert graph.defs[graph.qn("C.f")] == cs.NodeLabel.METHOD
    assert not graph.macro_named(), graph.macro_named()


@pytest.mark.parametrize(
    ("header_decl", "definition"),
    [
        pytest.param(
            "  void f() const;",
            "void C::f() const LOCKS_EXCLUDED(mu) { mu.AssertHeld(); y(); }",
            id="plain-declaration",
        ),
        pytest.param(
            "  void f() const LOCKS_EXCLUDED(mu);",
            "void C::f() const LOCKS_EXCLUDED(mu) { mu.AssertHeld(); y(); }",
            id="annotated-declaration",
        ),
        # Without `const`, `void C::f()` cut off before the macro reads as a
        # variable initialised with `()`.
        pytest.param(
            "  void f();",
            "void C::f() LOCKS_EXCLUDED(mu) { mu.AssertHeld(); y(); }",
            id="no-qualifier",
        ),
    ],
)
def test_out_of_class_definition_with_trailing_macro_is_the_method(
    temp_repo: Path, header_decl: str, definition: str
) -> None:
    cpp = f"""\
#include "c.h"
Mutex mu;
void x() {{}}
void y() {{}}
{definition}
"""
    graph = _index(temp_repo, {"c.h": _class_header(header_decl), "c.cpp": cpp})

    assert graph.defs[graph.qn("C.f")] == cs.NodeLabel.METHOD
    assert graph.callees("C.f") == {
        graph.qn("Mutex.AssertHeld"),
        graph.qn("c.y"),
    }, sorted(graph.calls)
    # The prototype split off before the macro used to register as a
    # module-level Function standing in for the method.
    assert not graph.has("c.f"), sorted(graph.defs)
    assert not graph.macro_named(), graph.macro_named()


@pytest.mark.parametrize(
    ("members", "definitions"),
    [
        pytest.param(
            "  C() LOCKS_EXCLUDED(mu) { x(); }\n  ~C() LOCKS_EXCLUDED(mu) { y(); }",
            "",
            id="in-class",
        ),
        pytest.param(
            "  C(int v);\n  ~C();\n  int v_;",
            "C::C(int v) LOCKS_EXCLUDED(mu) : v_(v) { x(); }\n"
            "C::~C() LOCKS_EXCLUDED(mu) { y(); }\n",
            id="out-of-class",
        ),
    ],
)
def test_constructor_and_destructor_with_trailing_macro_keep_their_calls(
    temp_repo: Path, members: str, definitions: str
) -> None:
    cpp = '#include "c.h"\nvoid x() {}\nvoid y() {}\n' + definitions
    graph = _index(temp_repo, {"c.h": _class_header(members), "c.cpp": cpp})

    assert graph.callees("C.C") == {graph.qn("c.x")}, sorted(graph.calls)
    assert graph.callees("C.~C") == {graph.qn("c.y")}, sorted(graph.calls)
    assert not graph.macro_named(), graph.macro_named()


NAMESPACED_H = (
    LOCKS_H
    + """
namespace ns {
class N {
 public:
  N();
  ~N();
  void f();
  void g() const;
};
}  // namespace ns
"""
)

NAMESPACED_CPP = """\
#include "n.h"
void x() {}
void y() {}
namespace ns {
N::N() LOCKS_EXCLUDED(mu) { x(); }
N::~N() LOCKS_EXCLUDED(mu) { y(); }
void N::f() LOCKS_REQUIRED(mu) { x(); }
}  // namespace ns
void ns::N::g() const LOCKS_REQUIRED(mu) { y(); }
"""


def test_namespaced_out_of_class_definitions_with_trailing_macro(
    temp_repo: Path,
) -> None:
    graph = _index(temp_repo, {"n.h": NAMESPACED_H, "n.cpp": NAMESPACED_CPP})

    x, y = graph.qn("n.x"), graph.qn("n.y")
    assert graph.callees("ns.N.N") == {x}, sorted(graph.calls)
    assert graph.callees("ns.N.~N") == {y}, sorted(graph.calls)
    assert graph.callees("ns.N.f") == {x}, sorted(graph.calls)
    assert graph.callees("ns.N.g") == {y}, sorted(graph.calls)
    assert not graph.macro_named(), graph.macro_named()


def test_guarded_data_member_is_not_a_method(temp_repo: Path) -> None:
    members = """\
  int count_ GUARDED_BY(mu);
  int* ptr_ PT_GUARDED_BY(mu) = nullptr;
  void g() const { x(); }"""
    header = _class_header(members) + "extern int g_count GUARDED_BY(mu);\n"
    cpp = '#include "c.h"\nint g_count GUARDED_BY(mu) = 0;\n'
    graph = _index(temp_repo, {"c.h": header, "c.cpp": cpp})

    assert graph.defs[graph.qn("C.g")] == cs.NodeLabel.METHOD
    assert not graph.macro_named(), graph.macro_named()


@pytest.mark.skipif(not cpp_frontend_available(), reason="libclang not available")
def test_hybrid_frontend_keeps_trailing_macro_calls(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # HYBRID (the default when libclang is found) keeps tree-sitter as the
    # owner of definitions and CALLS, so it inherited the bug.
    monkeypatch.setattr(gu.settings, "CPP_FRONTEND", cs.CppFrontend.HYBRID)
    graph = _index(temp_repo, {"box.h": BOX_H, "box.cpp": BOX_CPP}, compdb=True)

    assert graph.callees("Box.Check") >= {
        graph.qn("Mutex.AssertHeld"),
        graph.qn("Box.helper"),
    }, sorted(graph.calls)
    assert graph.callees("box.run") >= {
        graph.qn("Box.Check"),
        graph.qn("Box.Count"),
    }, sorted(graph.calls)
    # The macro's own Function node (a libclang fact) is a legitimate callee;
    # a METHOD named after the macro is the artifact.
    assert not graph.has("Box.LOCKS_REQUIRED")
    assert not any(
        caller.endswith("Box.LOCKS_REQUIRED") for caller, _ in graph.calls
    ), sorted(graph.calls)


def _parse(source: str) -> tuple[bytes, bytes, bool]:
    parsers, _ = load_parsers()
    raw = source.encode()
    tree = parse_with_preproc_recovery(
        parsers[cs.SupportedLanguage.CPP], raw, cs.SupportedLanguage.CPP
    )
    text = tree.root_node.text or b""
    return raw, text, tree.root_node.has_error


def test_trailing_macro_is_blanked_in_place() -> None:
    source = (
        "class Box {\n"
        "  bool Check() const\n"
        "      LOCKS_REQUIRED(mu) { return helper(); }\n"
        "  bool helper() const;\n"
        "};\n"
    )
    raw, text, has_error = _parse(source)

    assert not has_error
    # Space-filled, so byte offsets and line numbers match the file on disk.
    assert len(text) == len(raw)
    assert text.count(b"\n") == raw.count(b"\n")
    assert b"LOCKS_REQUIRED" not in text
    assert text.replace(b" ", b"") == raw.replace(b"LOCKS_REQUIRED(mu)", b"").replace(
        b" ", b""
    )


# Negative tests: shapes that must parse and index exactly as before.

ORDINARY_H = (
    LOCKS_H
    + """
class Base {
 public:
  virtual void o() {}
};

class C : public Base {
 public:
  void a() const { x(); }
  void n() noexcept { x(); }
  void o() override { x(); }
  void q() const noexcept(true) { y(); }
  auto t() -> int { y(); return 1; }
  void d() = delete;
  virtual void p() const = 0;
};

auto free_fn() -> int { x(); return 0; }
"""
)


def test_ordinary_qualified_methods_and_trailing_return_type_unchanged(
    temp_repo: Path,
) -> None:
    raw, text, has_error = _parse(ORDINARY_H)
    assert not has_error
    assert text == raw

    graph = _index(temp_repo, {"c.h": ORDINARY_H})
    for name, callee in (("a", "x"), ("n", "x"), ("o", "x"), ("q", "y"), ("t", "y")):
        assert graph.callees(f"C.{name}") == {graph.qn(f"c.{callee}")}, (
            name,
            sorted(graph.calls),
        )
    assert graph.callees("c.free_fn") == {graph.qn("c.x")}
    assert graph.has("C.d"), sorted(graph.defs)
    assert graph.has("C.p"), sorted(graph.defs)


CLASS_SCOPE_MACROS_H = (
    LOCKS_H
    + """
#define DECLARE_THING(T)
#define REGISTER_FIELD(name) int name
#define DISALLOW_COPY_AND_ASSIGN(T) T(const T&) = delete

class V {
 public:
  void f() const;
  DECLARE_THING(V);
  void g() const LOCKS_REQUIRED(mu) { x(); }
  REGISTER_FIELD(width);
 private:
  DISALLOW_COPY_AND_ASSIGN(V);
};

void helper() {}
TEST_CASE(Suite, Name) { helper(); }
"""
)


def test_class_scope_macro_invocation_is_not_a_method(temp_repo: Path) -> None:
    raw, text, _ = _parse(CLASS_SCOPE_MACROS_H)
    # Only the annotation after g()'s declarator is blanked; macro
    # invocations that stand on their own keep their text.
    for invocation in (
        b"DECLARE_THING(V);",
        b"REGISTER_FIELD(width);",
        b"DISALLOW_COPY_AND_ASSIGN(V);",
        b"TEST_CASE(Suite, Name) {",
    ):
        assert invocation in text, invocation
    assert len(text) == len(raw)

    graph = _index(temp_repo, {"v.h": CLASS_SCOPE_MACROS_H})
    names = {qn.rsplit(cs.SEPARATOR_DOT, 1)[-1] for qn in graph.defs}
    assert not names & {
        "DECLARE_THING",
        "REGISTER_FIELD",
        "DISALLOW_COPY_AND_ASSIGN",
        "TEST_CASE",
    }, sorted(graph.defs)
    assert graph.defs[graph.qn("V.f")] == cs.NodeLabel.METHOD
    assert graph.callees("V.g") == {graph.qn("v.x")}, sorted(graph.calls)


@pytest.mark.parametrize(
    "source",
    [
        # Macro lines with no `;` glue onto each other and read as a typed
        # declaration; the "declarator" there is itself a macro.
        pytest.param(
            "class W {\n"
            " private:\n"
            "  Q_DISABLE_COPY(W)\n"
            "  Q_DECLARE_PRIVATE(W)\n"
            "  DECLARE_THING(W);\n"
            "};\n",
            id="glued-class-scope-macros",
        ),
        # A macro BEFORE the name: the ALL_CAPS word after it is the
        # function, and `(int a)` is a parameter list, not macro arguments.
        pytest.param(
            "int ATTR(x) NAME(int a) { return a; }\n",
            id="macro-before-all-caps-name",
        ),
        # Only an ALL_CAPS word reads as an annotation macro; any other stray
        # word is left for tree-sitter to report.
        pytest.param(
            "class K {\n"
            "  void f() const stray_word { }\n"
            "  void g() const stray_call(x) { }\n"
            "};\n",
            id="lowercase-words",
        ),
    ],
)
def test_macro_that_is_not_a_trailing_annotation_is_not_blanked(source: str) -> None:
    raw, text, has_error = _parse(source)

    assert has_error
    assert text == raw
