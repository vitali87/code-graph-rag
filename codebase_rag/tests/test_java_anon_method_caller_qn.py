# (H) A method-body anonymous-class method (`make(){ return new Reader(){ read(){
# (H) helper(); } }; }`) was registered by the definition pass as `Class.read` (the
# (H) unified-FQN scope walk dropped the enclosing method `make`), but the call pass
# (H) attributes its outgoing calls to `Class.make.read` -- a phantom qn with no node.
# (H) So every edge FROM such a method dangled and its callees looked dead. The two
# (H) passes must agree on the qn (both `Class.make.read`).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def test_anon_method_call_edge_joins_a_real_node(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "jfqn"
    pkg = root / "com" / "example"
    pkg.mkdir(parents=True)
    (pkg / "M.java").write_text(
        "package com.example;\n"
        "public class M {\n"
        "  interface Reader { int read(); }\n"
        "  static int helper(int x) { return x; }\n"
        "  static Reader make() {\n"
        "    return new Reader() {\n"
        "      @Override public int read() { return helper(1); }\n"
        "    };\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    updater = create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    registry = updater.factory.function_registry
    calls = get_relationships(mock_ingestor, "CALLS")
    # (H) the CALLS edge into helper must originate from a qn that is a registered
    # (H) node -- not a phantom `Class.make.read` that no node carries.
    helper_callers = [
        c.args[0][2] for c in calls if c.args[2][2].endswith(".helper(int)")
    ]
    assert helper_callers, "no caller recorded for helper"
    for caller_qn in helper_callers:
        assert caller_qn in registry, (
            f"caller {caller_qn} is a phantom (no node); "
            f"def-pass and call-pass disagree on the anon method qn"
        )


def test_anon_override_unqualified_call_binds_to_base(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) An unqualified call inside a method-body anonymous override targets the anon's
    # (H) own/inherited method, not the enclosing named class. `helper()` inside
    # (H) `new Base(){ read(){ return helper(); } }` must resolve to Base.helper (the
    # (H) anon's inherited method), not drop or bind to an outer class.
    root = temp_repo / "janonbase"
    pkg = root / "com" / "example"
    pkg.mkdir(parents=True)
    # (H) M also declares helper() as a decoy: without anon-base scoping the call would
    # (H) mis-bind to the enclosing M.helper via lexical-class resolution.
    (pkg / "M.java").write_text(
        "package com.example;\n"
        "class Base {\n"
        "  int helper() { return 1; }\n"
        "  int read() { return 0; }\n"
        "}\n"
        "public class M {\n"
        "  int helper() { return 99; }\n"
        "  Base make() {\n"
        "    return new Base() {\n"
        "      @Override int read() { return helper(); }\n"
        "    };\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    calls = {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }
    assert any(
        f.endswith(".read") and t.endswith(".Base.helper()") for f, t in calls
    ), sorted(t for f, t in calls if "helper" in t)
    assert not any(
        f.endswith(".read") and t.endswith(".M.helper()") for f, t in calls
    ), "anon override call wrongly bound to the enclosing class's decoy helper"


def test_anon_override_explicit_this_call_binds_to_base(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) `this.helper()` inside a method-body anonymous override dispatches on the anon
    # (H) (its base), not the enclosing named class -- same as the bare-call case but via
    # (H) the explicit-`this` receiver path.
    root = temp_repo / "janonthis"
    pkg = root / "com" / "example"
    pkg.mkdir(parents=True)
    (pkg / "M.java").write_text(
        "package com.example;\n"
        "class Base {\n"
        "  int helper() { return 1; }\n"
        "  int read() { return 0; }\n"
        "}\n"
        "public class M {\n"
        "  int helper() { return 99; }\n"
        "  Base make() {\n"
        "    return new Base() {\n"
        "      @Override int read() { return this.helper(); }\n"
        "    };\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    calls = {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }
    assert any(
        f.endswith(".read") and t.endswith(".Base.helper()") for f, t in calls
    ), sorted(t for f, t in calls if "helper" in t)
    assert not any(
        f.endswith(".read") and t.endswith(".M.helper()") for f, t in calls
    ), "explicit this.helper() in anon wrongly bound to enclosing class"


def test_anon_own_method_unqualified_call_resolves(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) An unqualified call to the anonymous class's OWN (non-inherited) method
    # (H) (gson's `delegate().read()` where `delegate()` is defined in the same anon)
    # (H) must resolve; the anon's own methods register as Function nodes, which the
    # (H) module-wide fallback previously skipped.
    root = temp_repo / "janonown"
    pkg = root / "com" / "example"
    pkg.mkdir(parents=True)
    (pkg / "M.java").write_text(
        "package com.example;\n"
        "public class M {\n"
        "  Object make() {\n"
        "    return new Object() {\n"
        "      int helper() { return 1; }\n"
        "      int read() { return helper(); }\n"
        "    };\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    calls = {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }
    assert any(f.endswith(".read") and t.endswith(".helper") for f, t in calls), sorted(
        t for f, t in calls if "helper" in t
    )


def test_outside_call_does_not_bind_to_anon_local_method(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) An anon class's OWN method registers as a module-scoped Function; the
    # (H) unqualified module-wide fallback must NOT let a call OUTSIDE that anon bind to
    # (H) it. `M.use()` calling `helper()` is not lexically inside the anon that declares
    # (H) `helper()`, so no CALLS edge to the anon-local helper may be emitted.
    root = temp_repo / "janonscope"
    pkg = root / "com" / "example"
    pkg.mkdir(parents=True)
    (pkg / "M.java").write_text(
        "package com.example;\n"
        "public class M {\n"
        "  Object make() {\n"
        "    return new Object() {\n"
        "      int helper() { return 1; }\n"
        "    };\n"
        "  }\n"
        "  int use() { return helper(); }\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    calls = {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }
    assert not any(
        f.endswith(".M.use()") and t.endswith(".make.helper") for f, t in calls
    ), "outside call wrongly bound to an anon-class-local method (scope violation)"
