# A Java method called through `import static C.m;` or `import static C.*;`
# got no CALLS edge: the static import was recorded like a type import, so
# the bare call `m(...)` had nothing that led to class C's member, and
# precondition helpers, assertion helpers and project-local utility classes
# reported dead (square/javapoet's Util.checkArgument: 63 call sites, 1 edge).
# Issue #2544.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.import_processor import ImportProcessor
from codebase_rag.types_defs import JavaStaticImport

PROJECT = "repo"
SRC = "src/main/java"
ACME = f"{PROJECT}.src.main.java.com.acme"

UTIL = """package com.acme;

final class Util {
  private Util() {}

  static void checkArgument(boolean cond, String fmt, Object... args) {
    if (!cond) throw new IllegalArgumentException(String.format(fmt, args));
  }

  static String quote(String s) { return "\\"" + s + "\\""; }

  static int twice(int n) { return n * 2; }

  static int pad(int n) { return n; }

  static int pad(int n, int width) { return n + width; }
}
"""

# The issue's repro, verbatim in shape: one single-member and one on-demand
# static import of the same class, plus the qualified control.
WRITER = """package com.acme;

import static com.acme.Util.checkArgument;
import static com.acme.Util.*;

public final class Writer {
  public String emit(String s, int indent) {
    checkArgument(indent >= 0, "bad indent %s", indent);
    checkArgument(s != null, "null");
    int n = twice(indent);
    return quote(s) + n;
  }

  public String qualified(String s) {
    Util.checkArgument(s != null, "null");
    return Util.quote(s);
  }
}
"""

PADDER = """package com.acme;

import static com.acme.Util.pad;

public class Padder {
  public int one(int n) { return pad(n); }

  public int two(int n) { return pad(n, 4); }
}
"""

PRECONDITIONS = """package com.acme.util;

public final class Preconditions {
  private Preconditions() {}

  public static <T> T checkNotNull(T ref) { return ref; }

  public static <T> T checkNotNull(T ref, String message) { return ref; }

  public static void checkState(boolean state) {}
}
"""

EMITTER = """package com.acme.io;

import static com.acme.util.Preconditions.checkNotNull;
import static com.acme.util.Preconditions.*;

public class Emitter {
  public void single(Object o) { checkNotNull(o); }

  public void singleTwo(Object o) { checkNotNull(o, "o"); }

  public void onDemand(boolean b) { checkState(b); }
}
"""

ON_DEMAND_ONLY = """package com.acme.io;

import static com.acme.util.Preconditions.*;

public class OnDemandOnly {
  public void one(Object o) { checkNotNull(o); }

  public void two(Object o) { checkNotNull(o, "o"); }
}
"""

HOLDER = """package com.acme;

public class Holder {
  public static class Strings {
    public static String trim(String s) { return s.trim(); }
  }
}
"""

# A static import of a NESTED class's member: the file is Holder.java, the
# class is Holder.Strings.
NESTED_USER = """package com.acme.io;

import static com.acme.Holder.Strings.trim;

public class NestedUser {
  public String use(String s) { return trim(s); }
}
"""

FIRST = """package com.acme;

public class First {
  public static void go() {}
}
"""

SECOND = """package com.acme;

public class Second {
  public static void go() {}
}
"""

# JLS 6.4.1: a single-static-import shadows an on-demand one of the same name.
PRECEDENCE = """package com.acme;

import static com.acme.First.*;
import static com.acme.Second.go;

public class Precedence {
  public void run() { go(); }
}
"""

# First-party look-alikes of the JDK / JUnit classes the next file imports
# from: a static import of the REAL class must not bind to them.
FAKE_MATH = """package com.acme.num;

public final class Math {
  public static int max(int a, int b) { return a; }
}
"""

FAKE_ASSERTIONS = """package com.acme.testing;

public final class Assertions {
  public static void assertEquals(int expected, int actual) {}
}
"""

MATH_USER = """package com.acme;

import static java.lang.Math.max;
import static org.junit.jupiter.api.Assertions.assertEquals;

public class MathUser {
  public int biggest(int a, int b) { return max(a, b); }

  public void check(int a) { assertEquals(1, a); }
}

class Local {
  static int max(int a, int b) { return b; }

  static void assertEquals(int expected, int actual) {}
}
"""

# Java shadowing: a method the enclosing class declares (or inherits) hides a
# statically imported one of the same name.
SHADOW = """package com.acme;

import static com.acme.Util.quote;
import static com.acme.Util.*;

public class Shadow {
  static String quote(String s) { return s; }

  static int twice(int n) { return n + n; }

  public String useQuote(String s) { return quote(s); }

  public int useTwice(int n) { return twice(n); }
}
"""

OUTER = """package com.acme;

import static com.acme.Util.quote;

public class Outer {
  static String quote(String s) { return s; }

  static class Inner {
    String use(String s) { return quote(s); }
  }
}
"""

BASE = """package com.acme;

public class Base {
  protected static String quote(String s) { return s; }
}
"""

CHILD = """package com.acme;

import static com.acme.Util.quote;

public class Child extends Base {
  public String use(String s) { return quote(s); }
}
"""

# A plain (non-static) import brings the TYPE into scope, not its members.
TYPE_IMPORT_ONLY = """package com.acme.io;

import com.acme.util.Preconditions;

public class TypeImportOnly {
  public void bare(Object o) { checkNotNull(o); }

  public void qualified(Object o) { Preconditions.checkNotNull(o); }
}
"""

PACKAGE_IMPORT_ONLY = """package com.acme.io;

import com.acme.util.*;
import com.acme.util.Preconditions.*;

public class PackageImportOnly {
  public void bare(Object o) { checkNotNull(o); }
}
"""

FILES = {
    "com/acme/Util.java": UTIL,
    "com/acme/Writer.java": WRITER,
    "com/acme/Padder.java": PADDER,
    "com/acme/util/Preconditions.java": PRECONDITIONS,
    "com/acme/io/Emitter.java": EMITTER,
    "com/acme/io/OnDemandOnly.java": ON_DEMAND_ONLY,
    "com/acme/Holder.java": HOLDER,
    "com/acme/io/NestedUser.java": NESTED_USER,
    "com/acme/First.java": FIRST,
    "com/acme/Second.java": SECOND,
    "com/acme/Precedence.java": PRECEDENCE,
    "com/acme/num/Math.java": FAKE_MATH,
    "com/acme/testing/Assertions.java": FAKE_ASSERTIONS,
    "com/acme/MathUser.java": MATH_USER,
    "com/acme/Shadow.java": SHADOW,
    "com/acme/Outer.java": OUTER,
    "com/acme/Base.java": BASE,
    "com/acme/Child.java": CHILD,
    "com/acme/io/TypeImportOnly.java": TYPE_IMPORT_ONLY,
    "com/acme/io/PackageImportOnly.java": PACKAGE_IMPORT_ONLY,
}

UTIL_CLS = f"{ACME}.Util.Util"
PRECONDITIONS_CLS = f"{ACME}.util.Preconditions.Preconditions"
CHECK_ARGUMENT = f"{UTIL_CLS}.checkArgument(boolean,String,Object...)"
QUOTE = f"{UTIL_CLS}.quote(String)"
TWICE = f"{UTIL_CLS}.twice(int)"
CHECK_NOT_NULL_1 = f"{PRECONDITIONS_CLS}.checkNotNull(T)"
CHECK_NOT_NULL_2 = f"{PRECONDITIONS_CLS}.checkNotNull(T,String)"
CHECK_STATE = f"{PRECONDITIONS_CLS}.checkState(boolean)"


def _build_calls(root: Path, files: dict[str, str]) -> set[tuple[str, str]]:
    parsers, queries = load_parsers()
    if "java" not in parsers:
        pytest.skip("java parser not available")
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run()
    return {
        (c.args[0][2], c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if c.args[1] == "CALLS"
    }


@pytest.fixture(scope="module")
def calls(tmp_path_factory: pytest.TempPathFactory) -> set[tuple[str, str]]:
    root = tmp_path_factory.mktemp("jstatic") / PROJECT
    return _build_calls(root, {f"{SRC}/{rel}": text for rel, text in FILES.items()})


def _callees(calls: set[tuple[str, str]], caller: str) -> set[str]:
    return {callee for src, callee in calls if src == caller}


# --- the bug: static-import calls get an edge ---------------------------------


def test_single_static_import_call_binds(calls: set[tuple[str, str]]) -> None:
    emit = f"{ACME}.Writer.Writer.emit(String,int)"
    assert CHECK_ARGUMENT in _callees(calls, emit)


def test_on_demand_static_import_calls_bind(calls: set[tuple[str, str]]) -> None:
    emit = f"{ACME}.Writer.Writer.emit(String,int)"
    assert {TWICE, QUOTE} <= _callees(calls, emit)


def test_issue_repro_emit_has_exactly_the_imported_callees(
    calls: set[tuple[str, str]],
) -> None:
    emit = f"{ACME}.Writer.Writer.emit(String,int)"
    assert _callees(calls, emit) == {CHECK_ARGUMENT, TWICE, QUOTE}


def test_static_import_overload_picked_by_arity(calls: set[tuple[str, str]]) -> None:
    assert _callees(calls, f"{ACME}.Padder.Padder.one(int)") == {f"{UTIL_CLS}.pad(int)"}
    assert _callees(calls, f"{ACME}.Padder.Padder.two(int)") == {
        f"{UTIL_CLS}.pad(int,int)"
    }


def test_cross_package_single_static_import_overloads(
    calls: set[tuple[str, str]],
) -> None:
    emitter = f"{ACME}.io.Emitter.Emitter"
    assert _callees(calls, f"{emitter}.single(Object)") == {CHECK_NOT_NULL_1}
    assert _callees(calls, f"{emitter}.singleTwo(Object)") == {CHECK_NOT_NULL_2}
    assert _callees(calls, f"{emitter}.onDemand(boolean)") == {CHECK_STATE}


def test_cross_package_on_demand_static_import_overloads(
    calls: set[tuple[str, str]],
) -> None:
    only = f"{ACME}.io.OnDemandOnly.OnDemandOnly"
    assert _callees(calls, f"{only}.one(Object)") == {CHECK_NOT_NULL_1}
    assert _callees(calls, f"{only}.two(Object)") == {CHECK_NOT_NULL_2}


def test_static_import_of_nested_class_member_binds(
    calls: set[tuple[str, str]],
) -> None:
    assert _callees(calls, f"{ACME}.io.NestedUser.NestedUser.use(String)") == {
        f"{ACME}.Holder.Holder.Strings.trim(String)"
    }


def test_static_import_across_build_modules_binds(tmp_path: Path) -> None:
    # A multi-module build (lib/src/main/java, app/src/main/java) is not a
    # source root the import probe knows, so the import arrives unprefixed
    # (com.lib.Checks) and must still reach the repo file of that package.
    files = {
        "lib/src/main/java/com/lib/Checks.java": (
            "package com.lib;\n\n"
            "public final class Checks {\n"
            "  public static void require(boolean ok) {}\n"
            "}\n"
        ),
        "app/src/main/java/com/app/Main.java": (
            "package com.app;\n\n"
            "import static com.lib.Checks.require;\n\n"
            "public class Main {\n"
            "  public void run() { require(true); }\n"
            "}\n"
        ),
    }
    edges = _build_calls(tmp_path / PROJECT, files)
    caller = f"{PROJECT}.app.src.main.java.com.app.Main.Main.run()"
    assert _callees(edges, caller) == {
        f"{PROJECT}.lib.src.main.java.com.lib.Checks.Checks.require(boolean)"
    }


def test_single_static_import_shadows_on_demand(
    calls: set[tuple[str, str]],
) -> None:
    assert _callees(calls, f"{ACME}.Precedence.Precedence.run()") == {
        f"{ACME}.Second.Second.go()"
    }


def test_external_static_import_does_not_bind_same_file_lookalike(
    calls: set[tuple[str, str]],
) -> None:
    # `Local` shares the file but is not an enclosing class, so its max /
    # assertEquals are not in scope: the calls mean java.lang.Math.max and
    # JUnit's assertEquals, which are not in the repo.
    user = f"{ACME}.MathUser.MathUser"
    assert _callees(calls, f"{user}.biggest(int,int)") == set()
    assert _callees(calls, f"{user}.check(int)") == set()


# --- negative: neighbouring behaviour that must not change -------------------


def test_external_static_import_does_not_bind_first_party_lookalike_class(
    calls: set[tuple[str, str]],
) -> None:
    callees = {callee for _, callee in calls}
    assert f"{ACME}.num.Math.Math.max(int,int)" not in callees
    assert f"{ACME}.testing.Assertions.Assertions.assertEquals(int,int)" not in callees


def test_own_class_method_shadows_single_static_import(
    calls: set[tuple[str, str]],
) -> None:
    assert _callees(calls, f"{ACME}.Shadow.Shadow.useQuote(String)") == {
        f"{ACME}.Shadow.Shadow.quote(String)"
    }


def test_own_class_method_shadows_on_demand_static_import(
    calls: set[tuple[str, str]],
) -> None:
    assert _callees(calls, f"{ACME}.Shadow.Shadow.useTwice(int)") == {
        f"{ACME}.Shadow.Shadow.twice(int)"
    }


def test_enclosing_class_method_shadows_static_import(
    calls: set[tuple[str, str]],
) -> None:
    assert _callees(calls, f"{ACME}.Outer.Outer.Inner.use(String)") == {
        f"{ACME}.Outer.Outer.quote(String)"
    }


def test_inherited_method_shadows_static_import(
    calls: set[tuple[str, str]],
) -> None:
    assert _callees(calls, f"{ACME}.Child.Child.use(String)") == {
        f"{ACME}.Base.Base.quote(String)"
    }


def test_type_import_does_not_make_static_members_callable_bare(
    calls: set[tuple[str, str]],
) -> None:
    assert _callees(calls, f"{ACME}.io.TypeImportOnly.TypeImportOnly.bare(Object)") == (
        set()
    )


def test_type_on_demand_imports_do_not_make_static_members_callable_bare(
    calls: set[tuple[str, str]],
) -> None:
    caller = f"{ACME}.io.PackageImportOnly.PackageImportOnly.bare(Object)"
    assert _callees(calls, caller) == set()


def test_qualified_static_calls_still_bind(calls: set[tuple[str, str]]) -> None:
    assert _callees(calls, f"{ACME}.Writer.Writer.qualified(String)") == {
        CHECK_ARGUMENT,
        QUOTE,
    }
    assert _callees(
        calls, f"{ACME}.io.TypeImportOnly.TypeImportOnly.qualified(Object)"
    ) == {CHECK_NOT_NULL_1}


# --- the recorded import state ------------------------------------------------


def test_static_imports_recorded_apart_from_type_imports(tmp_path: Path) -> None:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.JAVA not in parsers:
        pytest.skip("java parser not available")
    processor = ImportProcessor(tmp_path, PROJECT)
    root = (
        parsers[cs.SupportedLanguage.JAVA]
        .parse(
            b"package p;\n"
            b"import static java.lang.Math.max;\n"
            b"import static org.junit.Assert.*;\n"
            b"import java.util.List;\n"
            b"import java.util.*;\n"
        )
        .root_node
    )
    processor.parse_imports(root, "repo.p.A", cs.SupportedLanguage.JAVA, queries)
    assert processor.java_static_imports["repo.p.A"] == [
        JavaStaticImport(class_path="java.lang.Math", member="max"),
        JavaStaticImport(class_path="org.junit.Assert", member=None),
    ]


def test_reparse_drops_removed_static_imports(tmp_path: Path) -> None:
    # A watch-mode re-parse of the same module must not keep binding bare
    # calls through a static import the edit removed.
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.JAVA not in parsers:
        pytest.skip("java parser not available")
    java = parsers[cs.SupportedLanguage.JAVA]
    processor = ImportProcessor(tmp_path, PROJECT)
    with_import = java.parse(b"package p;\nimport static java.lang.Math.max;\n")
    processor.parse_imports(
        with_import.root_node, "repo.p.A", cs.SupportedLanguage.JAVA, queries
    )
    assert processor.java_static_imports.get("repo.p.A")
    without_import = java.parse(b"package p;\nimport java.util.List;\n")
    processor.parse_imports(
        without_import.root_node, "repo.p.A", cs.SupportedLanguage.JAVA, queries
    )
    assert "repo.p.A" not in processor.java_static_imports
