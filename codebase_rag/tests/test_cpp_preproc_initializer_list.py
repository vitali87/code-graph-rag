# Issue #2614: a conditional member initialiser (`#if !defined(NDEBUG)
# begin_(iov), #endif`, snappy's SnappyIOVecWriter) sits where the grammar
# allows no directive. tree-sitter-cpp opens a preproc_if inside the class
# body that pairs with no `#endif`, so the class swallows the rest of the
# file: later classes nest under it, free functions become its Methods, the
# constructor vanishes behind a phantom `limit_` Method and the enclosing
# namespace drops out of every qualified name.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tree_sitter import Parser

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import (
    get_nodes,
    get_qualified_names,
    get_relationships,
    run_updater,
)

_WRITER = """\
#include <cstddef>
struct iovec { void* iov_base; size_t iov_len; };

namespace ns {
static const int kMaxTag = 5;

class Writer {
 private:
  const struct iovec* end_;
  const struct iovec* begin_;
  size_t limit_;
  size_t remaining_;
 public:
  inline Writer(const struct iovec* iov, size_t iov_count)
      : end_(iov + iov_count),
#if !defined(NDEBUG)
        begin_(iov),
#endif
        limit_(-1) {
  }

  inline bool TryFastAppend(const char* ip, size_t available, size_t len,
                            char**) {
    if (len <= 16 && available >= 16 + kMaxTag && limit_ >= 16 &&
        remaining_ >= 16) {
      if (end_ != begin_) {
        return true;
      }
    }
    return false;
  }

  inline void Flush() {}

};

class Reader {
 public:
  int Get() { return 1; }
};

int Compress(const struct iovec* iov, size_t n) {
  Writer w(iov, n);
  w.Flush();
  return Reader().Get();
}

}  // namespace ns
"""

# Each variant puts the conditional initialiser in a different shape; every
# one collapses the tree on main. The `else` shape has no separating comma
# inside its branches, so keeping both branches' tokens still misparses
# (`begin_(iov) begin_(0) {`) and only the first branch may be kept.
_VARIANTS = {
    "else_no_comma": """\
namespace ns {
class Writer {
  int end_;
  int begin_;
 public:
  Writer(int iov)
      : end_(iov),
#ifdef FOO
        begin_(iov)
#else
        begin_(0)
#endif
  {
  }
  void Flush() {}
};
int Compress(int n) { Writer w(n); w.Flush(); return n; }
}
""",
    "leading_comma": """\
namespace ns {
class Writer {
  int end_;
  int begin_;
  int limit_;
 public:
  Writer(int iov)
      : end_(iov)
#if !defined(NDEBUG)
      , begin_(iov)
#endif
      , limit_(-1) {
  }
  void Flush() {}
};
int Compress(int n) { Writer w(n); w.Flush(); return n; }
}
""",
    "colon_start": """\
namespace ns {
class Writer {
  int end_;
  int begin_;
 public:
  Writer(int iov)
      :
#ifndef NDEBUG
        begin_(iov),
#endif
        end_(iov) {
  }
  void Flush() {}
};
int Compress(int n) { Writer w(n); w.Flush(); return n; }
}
""",
    "continued_condition": """\
namespace ns {
class Writer {
  int end_;
  int begin_;
  int limit_;
 public:
  Writer(int iov)
      : end_(iov),
#if !defined(NDEBUG) && \\
    defined(EXTRA_CHECKS)
        begin_(iov),
#endif
        limit_(-1) {
  }
  void Flush() {}
};
int Compress(int n) { Writer w(n); w.Flush(); return n; }
}
""",
}


def _write(root: Path, name: str, source: str) -> None:
    root.mkdir()
    (root / name).write_text(source, encoding="utf-8")


def _start_lines(mock_ingestor: MagicMock, label: str) -> dict[str, int]:
    return {
        c[0][1][cs.KEY_QUALIFIED_NAME]: c[0][1][cs.KEY_START_LINE]
        for c in get_nodes(mock_ingestor, label)
    }


def _calls(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2])
        for c in get_relationships(mock_ingestor, cs.RelationshipType.CALLS)
    }


def test_later_definitions_stay_out_of_the_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "snap"
    _write(root, "writer.cc", _WRITER)
    run_updater(root, mock_ingestor, skip_if_missing="cpp")

    classes = get_qualified_names(get_nodes(mock_ingestor, cs.NodeLabel.CLASS))
    functions = _start_lines(mock_ingestor, cs.NodeLabel.FUNCTION)
    methods = get_qualified_names(get_nodes(mock_ingestor, cs.NodeLabel.METHOD))

    # the free function after the class is a module Function inside ns, at
    # its real line, never a Method of Writer
    assert functions.get("snap.writer.ns.Compress") == 42, sorted(functions)
    assert not any(q.endswith("Writer.Compress") for q in methods), sorted(methods)

    # the next top-level class is a sibling of Writer, not nested in it
    assert "snap.writer.ns.Reader" in classes, sorted(classes)
    assert "snap.writer.ns.Reader.Get" in methods, sorted(methods)
    assert not any(".Writer.Reader" in q for q in classes | methods), sorted(
        classes | methods
    )

    # the namespace survives in the qualified names
    assert "snap.writer.ns.Writer" in classes, sorted(classes)
    assert "snap.writer.ns.Writer.Flush" in methods, sorted(methods)
    assert "snap.writer.ns.Writer.TryFastAppend" in methods, sorted(methods)


def test_constructor_registers_without_phantom_member(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "ctor"
    _write(root, "writer.cc", _WRITER)
    run_updater(root, mock_ingestor, skip_if_missing="cpp")

    methods = _start_lines(mock_ingestor, cs.NodeLabel.METHOD)
    assert methods.get("ctor.writer.ns.Writer.Writer") == 14, sorted(methods)
    # the trailing initialiser `limit_(-1) {}` is not a method declaration
    functions = _start_lines(mock_ingestor, cs.NodeLabel.FUNCTION)
    assert not any(q.endswith(".limit_") for q in {*methods, *functions}), sorted(
        {*methods, *functions}
    )


def test_calls_from_the_free_function_resolve(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "calls"
    _write(root, "writer.cc", _WRITER)
    run_updater(root, mock_ingestor, skip_if_missing="cpp")

    calls = _calls(mock_ingestor)
    caller = "calls.writer.ns.Compress"
    assert (caller, "calls.writer.ns.Writer.Flush") in calls, sorted(calls)
    assert (caller, "calls.writer.ns.Reader.Get") in calls, sorted(calls)
    assert (caller, "calls.writer.ns.Writer.Writer") in calls, sorted(calls)


@pytest.mark.parametrize("variant", sorted(_VARIANTS))
def test_initializer_directive_shapes(
    variant: str, temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "shape"
    _write(root, "writer.cc", _VARIANTS[variant])
    run_updater(root, mock_ingestor, skip_if_missing="cpp")

    classes = get_qualified_names(get_nodes(mock_ingestor, cs.NodeLabel.CLASS))
    functions = get_qualified_names(get_nodes(mock_ingestor, cs.NodeLabel.FUNCTION))
    methods = get_qualified_names(get_nodes(mock_ingestor, cs.NodeLabel.METHOD))

    assert "shape.writer.ns.Writer" in classes, sorted(classes)
    assert "shape.writer.ns.Writer.Writer" in methods, sorted(methods)
    assert "shape.writer.ns.Writer.Flush" in methods, sorted(methods)
    assert "shape.writer.ns.Compress" in functions, sorted(functions)
    assert "shape.writer.ns.Writer.Compress" not in methods, sorted(methods)


def test_out_of_line_constructor_keeps_its_node(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # the same initialiser on an out-of-line definition: the scope survives,
    # but the open preproc_if swallows the constructor and every later
    # definition
    source = """\
namespace ns {
class Writer {
 public:
  Writer(int iov);
  void Flush();
 private:
  int end_;
  int begin_;
  int limit_;
};

Writer::Writer(int iov)
    : end_(iov),
#if !defined(NDEBUG)
      begin_(iov),
#endif
      limit_(-1) {
}

void Writer::Flush() {}
}
"""
    root = temp_repo / "ool"
    _write(root, "writer.cc", source)
    run_updater(root, mock_ingestor, skip_if_missing="cpp")

    methods = _start_lines(mock_ingestor, cs.NodeLabel.METHOD)
    functions = get_qualified_names(get_nodes(mock_ingestor, cs.NodeLabel.FUNCTION))
    # the declarations in the class register too; the definitions, written
    # last, must not be lost
    assert methods.get("ool.writer.ns.Writer.Writer") == 12, methods
    assert methods.get("ool.writer.ns.Writer.Flush") == 20, methods
    assert not any(q.endswith(".limit_") for q in {*methods, *functions}), sorted(
        {*methods, *functions}
    )


# Two broken initialiser lists in one file that need DIFFERENT repairs:
# Writer's branches carry no separator, so only its first branch may stay;
# Reader's branches each end in `,`, so splicing both back in is valid and
# its `#else` initialiser call must survive Writer's fallback.
_MIXED = """\
namespace ns {
int MakeFast() { return 1; }
int MakeSlow() { return 2; }

class Writer {
  int end_;
  int begin_;
 public:
  Writer(int iov)
      : end_(iov),
#ifdef FOO
        begin_(iov)
#else
        begin_(0)
#endif
  {
  }
  void Flush() {}
};

class Reader {
  int a_;
  int b_;
  int c_;
 public:
  Reader()
      : a_(0),
#ifdef FAST
        b_(MakeFast()),
#else
        b_(MakeSlow()),
#endif
        c_(0) {
  }
  int Get() { return b_; }
};

int Compress(int n) { Writer w(n); w.Flush(); return Reader().Get(); }
}
"""


def test_fallback_is_decided_per_conditional(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "mixed"
    _write(root, "writer.cc", _MIXED)
    run_updater(root, mock_ingestor, skip_if_missing="cpp")

    calls = _calls(mock_ingestor)
    ctor = "mixed.writer.ns.Reader.Reader"
    assert (ctor, "mixed.writer.ns.MakeFast") in calls, sorted(calls)
    assert (ctor, "mixed.writer.ns.MakeSlow") in calls, sorted(calls)

    # both classes still close where they should
    classes = get_qualified_names(get_nodes(mock_ingestor, cs.NodeLabel.CLASS))
    methods = get_qualified_names(get_nodes(mock_ingestor, cs.NodeLabel.METHOD))
    functions = get_qualified_names(get_nodes(mock_ingestor, cs.NodeLabel.FUNCTION))
    assert {"mixed.writer.ns.Writer", "mixed.writer.ns.Reader"} <= classes
    assert "mixed.writer.ns.Writer.Writer" in methods, sorted(methods)
    assert "mixed.writer.ns.Reader.Get" in methods, sorted(methods)
    assert "mixed.writer.ns.Compress" in functions, sorted(functions)


def test_fallback_blanks_only_the_conditional_that_needs_it() -> None:
    from codebase_rag.parsers.cpp.preproc_recovery import (
        _retry_without_list_directives,
    )

    source = _MIXED.encode()
    cpp = _parser("cpp")
    tree, recovered = _retry_without_list_directives(cpp, cpp.parse(source), source)

    assert not tree.root_node.has_error
    assert b"b_(MakeFast())" in recovered
    assert b"b_(MakeSlow())" in recovered
    assert b"begin_(iov)" in recovered
    assert b"begin_(0)" not in recovered
    assert b"#ifdef" not in recovered
    assert b"#else" not in recovered


# --- what the recovery must leave alone ---


def test_declaration_level_alternatives_survive_first_branch_fallback(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # the `else_no_comma` initialiser forces the keep-first-branch retry; a
    # platform `#ifdef/#else` between member declarations is valid grammar
    # and must keep BOTH branches' methods
    source = _VARIANTS["else_no_comma"].replace(
        "  void Flush() {}\n",
        "  void Flush() {}\n"
        "#ifdef _WIN32\n"
        "  void WinOnly() {}\n"
        "#else\n"
        "  void PosixOnly() {}\n"
        "#endif\n",
    )
    root = temp_repo / "plat"
    _write(root, "writer.cc", source)
    run_updater(root, mock_ingestor, skip_if_missing="cpp")

    methods = get_qualified_names(get_nodes(mock_ingestor, cs.NodeLabel.METHOD))
    functions = get_qualified_names(get_nodes(mock_ingestor, cs.NodeLabel.FUNCTION))
    assert "plat.writer.ns.Writer.WinOnly" in methods, sorted(methods)
    assert "plat.writer.ns.Writer.PosixOnly" in methods, sorted(methods)
    assert "plat.writer.ns.Writer.Writer" in methods, sorted(methods)
    assert "plat.writer.ns.Compress" in functions, sorted(functions)


def _parser(language: str) -> Parser:
    from codebase_rag.parser_loader import load_parsers

    parsers, _ = load_parsers()
    if language not in parsers:
        pytest.skip(f"{language} parser not available")
    return parsers[language]


def test_well_parsed_list_conditional_keeps_every_branch() -> None:
    from codebase_rag.parsers.cpp.preproc_recovery import (
        _retry_without_list_directives,
    )

    # the enum's `#if/#else` follows a `,` (list context) but the grammar
    # accepts it: only the broken initialiser may lose its alternative branch
    source = (
        _VARIANTS["else_no_comma"]
        .replace(
            "class Writer {",
            "enum Mode {\n  kA,\n#ifdef FAST\n  kFast,\n#else\n  kSlow,\n#endif\n"
            "  kB\n};\nclass Writer {",
        )
        .encode()
    )
    cpp = _parser("cpp")
    original = cpp.parse(source)
    tree, recovered = _retry_without_list_directives(cpp, original, source)

    assert tree is not original
    assert not tree.root_node.has_error
    assert b"kFast" in recovered
    assert b"kSlow" in recovered
    assert b"#ifdef FAST" in recovered
    # the broken initialiser's directives and alternative branch are blanked
    assert b"begin_(0)" not in recovered
    assert b"#ifdef FOO" not in recovered
    # offsets and line numbers survive the blanking
    assert len(recovered) == len(source)
    assert recovered.count(b"\n") == source.count(b"\n")


def test_untargeted_errors_keep_the_original_tree() -> None:
    from codebase_rag.parsers.cpp.preproc_recovery import (
        _retry_without_list_directives,
    )

    cpp = _parser("cpp")
    # a clean file is returned untouched
    clean = b"class A {\n public:\n  A(int x) : x_(x) {}\n  int x_;\n};\n"
    clean_tree = cpp.parse(clean)
    assert _retry_without_list_directives(cpp, clean_tree, clean) == (
        clean_tree,
        clean,
    )

    # an error with no list-context directive anywhere: nothing to blank
    garbage = b"#if A\nint g();\n#endif\n" + b"}}}} class {{{{\n" * 4
    garbage_tree = cpp.parse(garbage)
    assert _retry_without_list_directives(cpp, garbage_tree, garbage) == (
        garbage_tree,
        garbage,
    )

    # a list-context directive the grammar parsed cleanly is not a target,
    # even when the file is broken elsewhere
    enum_then_garbage = (
        b"enum E {\n  A,\n#if X\n  B,\n#endif\n  C\n};\n" + b"}}}} class {{{{\n" * 4
    )
    enum_tree = cpp.parse(enum_then_garbage)
    assert _retry_without_list_directives(cpp, enum_tree, enum_then_garbage) == (
        enum_tree,
        enum_then_garbage,
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # a list left open before the directive
        (b"  : a_(1),\n#if X\n  b_(2),\n#endif\n  c_(3) {}\n", 1),
        (b"  f(a,\n#ifdef X\n  b,\n#endif\n  c);\n", 1),
        (b"  Foo(int x) :\n#if X\n  b_(2),\n#endif\n  c_(3) {}\n", 1),
        (b"  Foo() noexcept :\n#if X\n  b_(2),\n#endif\n  c_(3) {}\n", 1),
        (b"  :\n#if X\n  b_(2),\n#endif\n  c_(3) {}\n", 1),
        # a list continued after the directive (leading-comma style)
        (b"  : a_(1)\n#if X\n  , b_(2)\n#endif\n  , c_(3) {}\n", 1),
        # a trailing comment or a blank line does not hide the list
        (b"  : a_(1),  // debug\n\n#if X /* dbg */\n  b_(2),\n#endif\n  c_(3)\n", 1),
        # declaration level: an access label, a goto label, a statement end
        (b" public:\n#if X\n  void f();\n#endif\n", 0),
        (b"done:\n#if X\n  g();\n#endif\n", 0),
        (b"int a;\n#ifdef X\nint b;\n#endif\n", 0),
        # an include guard opens the file
        (b"#ifndef A_H\n#define A_H\nint a;\n#endif\n", 0),
        # a macro body ending in `,` is preprocessor text, not an open list
        (b"#define LIST a, \\\n  b,\n#if X\nint c;\n#endif\n", 0),
        # a qualified name opening the branch is not a list continuation
        (b"int a;\n#if X\n::ns::Foo f;\n#endif\n", 0),
    ],
)
def test_list_context_detection(source: bytes, expected: int) -> None:
    from codebase_rag.parsers.cpp.preproc_recovery import _list_context_conditionals

    found = _list_context_conditionals(source.split(b"\n"))
    assert len(found) == expected, found


def test_conditional_groups_shapes() -> None:
    from codebase_rag.parsers.cpp.preproc_recovery import _conditional_groups

    lines = (
        b"#if A && \\\n    B\nx,\n#elif C\ny,\n#else\nz,\n#endif\n#endif\n#if OPEN\n"
    ).split(b"\n")
    # the continued `#if` spans two lines; the stray `#endif` and the
    # never-closed `#if OPEN` are ignored
    assert _conditional_groups(lines) == [
        ([(0, 1), (3, 3), (5, 5), (7, 7)], [(2, 2), (4, 4), (6, 6)])
    ]

    nested = b"#if A\n#if B\nx,\n#endif\n#endif\n".split(b"\n")
    assert _conditional_groups(nested) == [
        ([(1, 1), (3, 3)], [(2, 2)]),
        ([(0, 0), (4, 4)], [(1, 3)]),
    ]


def test_c_parameter_list_directive_recovers() -> None:
    from codebase_rag.parsers.cpp.preproc_recovery import (
        parse_with_preproc_recovery,
    )

    source = (
        b"static int call(int x,\n#ifdef EXTRA\n                int y,\n#endif\n"
        b"                int z) {\n  return x;\n}\n"
        b"int after(void) { return call(1, 2); }\n"
    )
    c_parser = _parser("c")
    assert c_parser.parse(source).root_node.has_error
    tree = parse_with_preproc_recovery(c_parser, source, cs.SupportedLanguage.C)
    assert not tree.root_node.has_error
