# (H) The Java resolver matched a call to a method by NAME only ("any signature"),
# (H) returning the first overload, so a same-named overload with a different arity got
# (H) no CALLS edge and looked dead (gson's recursive `GsonTypes.resolve` 3-arg public
# (H) vs 4-arg private, `TypeToken.isAssignableFrom` overloads). A call must bind to the
# (H) overload whose parameter count matches the call's argument count.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def _calls(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }


def test_call_binds_to_arity_matching_overload(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "jovl"
    pkg = root / "com" / "example"
    pkg.mkdir(parents=True)
    # (H) resolve(a,b,c) is public API; it calls the 4-arg overload resolve(a,b,c,m).
    # (H) Both must be reachable -- the 4-arg call must bind to the 4-arg overload, not
    # (H) the 3-arg one that merely shares the name.
    (pkg / "M.java").write_text(
        "package com.example;\n"
        "import java.util.HashMap;\n"
        "public class M {\n"
        "  public static int resolve(int a, int b, int c) {\n"
        "    return resolve(a, b, c, new HashMap<Integer, Integer>());\n"
        "  }\n"
        "  private static int resolve(int a, int b, int c, HashMap<Integer, Integer> m) {\n"
        "    return a + b + c;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    calls = _calls(mock_ingestor)
    # (H) the 3-arg resolve calls the 4-arg overload (arity 4 matches the 4-arg call).
    assert any(
        f.endswith(".M.resolve(int,int,int)")
        and t.endswith(".M.resolve(int,int,int,HashMap<Integer, Integer>)")
        for f, t in calls
    ), sorted(t for f, t in calls if "resolve" in t)


def test_inherited_overload_binds_to_arity_match(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) Overloads declared on a BASE class, called through a subclass receiver: the
    # (H) inherited-method lookup must also pick the arity-matching overload, not the
    # (H) first same-named one (a 2-arg call must reach resolve(int,int), not
    # (H) resolve(int)).
    root = temp_repo / "jovl2"
    pkg = root / "com" / "example"
    pkg.mkdir(parents=True)
    (pkg / "M.java").write_text(
        "package com.example;\n"
        "class Base {\n"
        "  int resolve(int a) { return a; }\n"
        "  int resolve(int a, int b) { return a + b; }\n"
        "}\n"
        "class Sub extends Base {}\n"
        "public class M {\n"
        "  int use(Sub s) { return s.resolve(1, 2); }\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    calls = _calls(mock_ingestor)
    assert any(
        f.endswith(".M.use(Sub)") and t.endswith(".Base.resolve(int,int)")
        for f, t in calls
    ), sorted(t for f, t in calls if "resolve" in t)
    assert not any(
        f.endswith(".M.use(Sub)") and t.endswith(".Base.resolve(int)") for f, t in calls
    ), "2-arg call wrongly bound to the 1-arg inherited overload"


def test_same_arity_overload_binds_by_argument_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) Two overloads of the SAME arity (isX(Class) vs isX(String)) can't be told
    # (H) apart by arg count; a call with a String-typed argument must bind to the
    # (H) isX(String) overload (gson's isJavaType/isAndroidType String overloads).
    root = temp_repo / "jovl3"
    pkg = root / "com" / "example"
    pkg.mkdir(parents=True)
    (pkg / "M.java").write_text(
        "package com.example;\n"
        "public class M {\n"
        "  static boolean isX(Class<?> c) { return false; }\n"
        "  static boolean isX(String s) { return true; }\n"
        "  static boolean use(String name) { return isX(name); }\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    calls = _calls(mock_ingestor)
    assert any(
        f.endswith(".M.use(String)") and t.endswith(".M.isX(String)") for f, t in calls
    ), sorted(t for f, t in calls if "isX" in t)
    assert not any(
        f.endswith(".M.use(String)") and t.endswith(".M.isX(Class<?>)") for f, t in calls
    ), "String-arg call wrongly bound to the Class overload"
