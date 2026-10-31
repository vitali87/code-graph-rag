# Try-with-resources variables and catch parameters are never typed by the
# default Java frontend (issue #3341): a call through one gets no CALLS edge,
# while the same declaration as a plain local binds `exact`. Every test here
# asserts the CALLS edge a typed variable produces, with a same-named decoy
# method on another class so an unqualified fallback cannot pass it.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def _write_pkg(root: Path, files: dict[str, str]) -> None:
    pkg = root / "com" / "example"
    pkg.mkdir(parents=True)
    for name, text in files.items():
        (pkg / name).write_text(text, encoding="utf-8")


def _calls(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }


def _assert_calls(
    calls: set[tuple[str, str]],
    caller_suffix: str,
    target_suffix: str,
    decoy_suffix: str,
) -> None:
    assert any(
        f.endswith(caller_suffix) and t.endswith(target_suffix) for f, t in calls
    ), sorted(t for f, t in calls if f.endswith(caller_suffix))
    assert not any(
        f.endswith(caller_suffix) and t.endswith(decoy_suffix) for f, t in calls
    ), f"call wrongly bound to the decoy {decoy_suffix}"


_RES = "package com.example;\npublic class Res { public int read() { return 1; } }\n"
_OTHER = (
    "package com.example;\npublic class Other { public int read() { return 0; } }\n"
)
_APP_ERROR = (
    "package com.example;\n"
    "public class AppError extends RuntimeException {\n"
    '  public String detail() { return "a"; }\n'
    "}\n"
)
_OTHER_ERROR = (
    "package com.example;\n"
    "public class OtherError extends RuntimeException {\n"
    '  public String detail() { return "o"; }\n'
    "}\n"
)
_ERROR_DECOY = (
    "package com.example;\npublic class ErrorDecoy {\n"
    '  public String detail() { return "d"; }\n'
    "}\n"
)


def test_try_with_resources_explicit_type_resolves(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "jtwr"
    _write_pkg(
        root,
        {
            "Res.java": _RES,
            "Other.java": _OTHER,
            "Main.java": (
                "package com.example;\n"
                "public class Main {\n"
                "  int twr() throws Exception {\n"
                "    try (Res r = new Res()) {\n"
                "      return r.read();\n"
                "    }\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    _assert_calls(_calls(mock_ingestor), ".Main.twr()", ".Res.read()", ".Other.read()")


def test_try_with_resources_var_resolves(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "jtwrvar"
    _write_pkg(
        root,
        {
            "Res.java": _RES,
            "Other.java": _OTHER,
            "Main.java": (
                "package com.example;\n"
                "public class Main {\n"
                "  int twrVar() throws Exception {\n"
                "    try (var r = new Res()) {\n"
                "      return r.read();\n"
                "    }\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    _assert_calls(
        _calls(mock_ingestor), ".Main.twrVar()", ".Res.read()", ".Other.read()"
    )


def test_try_with_resources_final_resolves(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "jtwrfin"
    _write_pkg(
        root,
        {
            "Res.java": _RES,
            "Other.java": _OTHER,
            "Main.java": (
                "package com.example;\n"
                "public class Main {\n"
                "  int twrFinal() throws Exception {\n"
                "    try (final Res r = new Res()) {\n"
                "      return r.read();\n"
                "    }\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    _assert_calls(
        _calls(mock_ingestor), ".Main.twrFinal()", ".Res.read()", ".Other.read()"
    )


def test_two_resources_both_typed(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    root = temp_repo / "jtwrtwo"
    _write_pkg(
        root,
        {
            "Res.java": _RES,
            "Other.java": _OTHER,
            "Main.java": (
                "package com.example;\n"
                "public class Main {\n"
                "  int two() throws Exception {\n"
                "    try (Res a = new Res(); Res b = new Res()) {\n"
                "      return a.read() + b.read();\n"
                "    }\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    calls = _calls(mock_ingestor)
    targets = {
        t for f, t in calls if f.endswith(".Main.two()") and t.endswith(".read()")
    }
    assert len(targets) == 1 and next(iter(targets)).endswith(".Res.read()"), sorted(
        targets
    )


def test_interdependent_resources_var_inferred_from_first(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "jtwrdep"
    _write_pkg(
        root,
        {
            "Res.java": (
                "package com.example;\n"
                "public class Res {\n"
                "  public Wrap wrap() { return new Wrap(); }\n"
                "}\n"
            ),
            "Wrap.java": (
                "package com.example;\n"
                "public class Wrap { public int read() { return 2; } }\n"
            ),
            "Other.java": _OTHER,
            "Main.java": (
                "package com.example;\n"
                "public class Main {\n"
                "  int chained() throws Exception {\n"
                "    try (Res a = new Res(); var b = a.wrap()) {\n"
                "      return b.read();\n"
                "    }\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    _assert_calls(
        _calls(mock_ingestor), ".Main.chained()", ".Wrap.read()", ".Other.read()"
    )


def test_catch_parameter_resolves(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    root = temp_repo / "jcatch"
    _write_pkg(
        root,
        {
            "AppError.java": _APP_ERROR,
            "OtherError.java": _OTHER_ERROR,
            "ErrorDecoy.java": _ERROR_DECOY,
            "Main.java": (
                "package com.example;\n"
                "public class Main {\n"
                "  String caught() {\n"
                "    try {\n"
                "      throw new AppError();\n"
                "    } catch (AppError e) {\n"
                "      return e.detail();\n"
                "    }\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    _assert_calls(
        _calls(mock_ingestor),
        ".Main.caught()",
        ".AppError.detail()",
        ".ErrorDecoy.detail()",
    )


def test_catch_parameter_final_and_annotated_resolves(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "jcatchmod"
    _write_pkg(
        root,
        {
            "AppError.java": _APP_ERROR,
            "OtherError.java": _OTHER_ERROR,
            "ErrorDecoy.java": _ERROR_DECOY,
            "Main.java": (
                "package com.example;\n"
                "public class Main {\n"
                "  String caughtMod() {\n"
                "    try {\n"
                "      throw new AppError();\n"
                "    } catch (final @Deprecated AppError e) {\n"
                "      return e.detail();\n"
                "    }\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    _assert_calls(
        _calls(mock_ingestor),
        ".Main.caughtMod()",
        ".AppError.detail()",
        ".ErrorDecoy.detail()",
    )


def test_multi_catch_common_supertype_resolves(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "jmulticatch"
    _write_pkg(
        root,
        {
            "BaseError.java": (
                "package com.example;\n"
                "public class BaseError extends RuntimeException {\n"
                '  public String detail() { return "base"; }\n'
                "}\n"
            ),
            "AppError.java": (
                "package com.example;\npublic class AppError extends BaseError {}\n"
            ),
            "OtherError.java": (
                "package com.example;\npublic class OtherError extends BaseError {}\n"
            ),
            "ErrorDecoy.java": _ERROR_DECOY,
            "Main.java": (
                "package com.example;\n"
                "public class Main {\n"
                "  String multi() {\n"
                "    try {\n"
                "      throw new AppError();\n"
                "    } catch (AppError | OtherError e) {\n"
                "      return e.detail();\n"
                "    }\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    _assert_calls(
        _calls(mock_ingestor),
        ".Main.multi()",
        ".BaseError.detail()",
        ".ErrorDecoy.detail()",
    )


def test_multi_catch_unrelated_types_stay_untyped(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # AppError and OtherError share no first-party supertype that defines
    # detail(), so e must stay untyped: no edge to either class's detail().
    root = temp_repo / "jmulticatchneg"
    _write_pkg(
        root,
        {
            "AppError.java": _APP_ERROR,
            "OtherError.java": _OTHER_ERROR,
            "ErrorDecoy.java": _ERROR_DECOY,
            "Main.java": (
                "package com.example;\n"
                "public class Main {\n"
                "  String multiUnrelated() {\n"
                "    try {\n"
                "      throw new AppError();\n"
                "    } catch (AppError | OtherError e) {\n"
                "      return e.detail();\n"
                "    }\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    calls = _calls(mock_ingestor)
    targets = {
        t
        for f, t in calls
        if f.endswith(".Main.multiUnrelated()") and t.endswith(".detail()")
    }
    assert targets == set(), sorted(targets)


def test_java9_bare_resource_keeps_outer_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # try (r) declares nothing: the outer local's type must survive it.
    root = temp_repo / "jbare"
    _write_pkg(
        root,
        {
            "Res.java": _RES,
            "Other.java": _OTHER,
            "Main.java": (
                "package com.example;\n"
                "public class Main {\n"
                "  int bare() throws Exception {\n"
                "    Res r = new Res();\n"
                "    try (r) {\n"
                "      return r.read();\n"
                "    }\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    _assert_calls(_calls(mock_ingestor), ".Main.bare()", ".Res.read()", ".Other.read()")


def test_resource_shadows_field_inside_try_block(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "jshadow"
    _write_pkg(
        root,
        {
            "Res.java": _RES,
            "Other.java": _OTHER,
            "Main.java": (
                "package com.example;\n"
                "public class Main {\n"
                "  Other r = new Other();\n"
                "  int shadowed() throws Exception {\n"
                "    try (Res r = new Res()) {\n"
                "      return r.read();\n"
                "    }\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    _assert_calls(
        _calls(mock_ingestor), ".Main.shadowed()", ".Res.read()", ".Other.read()"
    )


def test_resource_factory_initializer_resolves(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A resource typed by a factory call's return type, not a constructor.
    root = temp_repo / "jtwrfactory"
    _write_pkg(
        root,
        {
            "Res.java": _RES,
            "Factory.java": (
                "package com.example;\n"
                "public class Factory {\n"
                "  public Res make() { return new Res(); }\n"
                "}\n"
            ),
            "Other.java": _OTHER,
            "Main.java": (
                "package com.example;\n"
                "public class Main {\n"
                "  int factoryMade() throws Exception {\n"
                "    Factory f = new Factory();\n"
                "    try (Res r = f.make()) {\n"
                "      return r.read();\n"
                "    }\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    _assert_calls(
        _calls(mock_ingestor), ".Main.factoryMade()", ".Res.read()", ".Other.read()"
    )


def test_catch_type_identity_follows_import(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Two first-party classes named AppError: the imported one wins over the
    # same-package one, so the catch parameter binds the imported class.
    root = temp_repo / "jimp"
    _write_pkg(
        root,
        {
            "AppError.java": _APP_ERROR,
            "OtherError.java": _OTHER_ERROR,
            "Main.java": (
                "package com.example;\n"
                "import other.AppError;\n"
                "public class Main {\n"
                "  String imported() {\n"
                "    try {\n"
                "      throw new AppError();\n"
                "    } catch (AppError e) {\n"
                "      return e.detail();\n"
                "    }\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    other_pkg = root / "other"
    other_pkg.mkdir(parents=True)
    (other_pkg / "AppError.java").write_text(
        "package other;\n"
        "public class AppError extends RuntimeException {\n"
        '  public String detail() { return "ext"; }\n'
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    calls = _calls(mock_ingestor)
    targets = {
        t
        for f, t in calls
        if f.endswith(".Main.imported()") and t.endswith(".detail()")
    }
    assert any(".other.AppError." in t for t in targets), sorted(targets)
    assert not any(".example.AppError." in t for t in targets), sorted(targets)


def test_nested_try_resources_both_typed(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "jtwrnested"
    _write_pkg(
        root,
        {
            "Res.java": _RES,
            "Other.java": _OTHER,
            "Main.java": (
                "package com.example;\n"
                "public class Main {\n"
                "  int nested() throws Exception {\n"
                "    try (Res a = new Res()) {\n"
                "      try (Res b = new Res()) {\n"
                "        return a.read() + b.read();\n"
                "      }\n"
                "    }\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    calls = _calls(mock_ingestor)
    targets = {
        t for f, t in calls if f.endswith(".Main.nested()") and t.endswith(".read()")
    }
    assert len(targets) == 1 and next(iter(targets)).endswith(".Res.read()"), sorted(
        targets
    )


def test_two_catch_blocks_each_typed(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    root = temp_repo / "jtwocatch"
    _write_pkg(
        root,
        {
            "AppError.java": _APP_ERROR,
            "OtherError.java": _OTHER_ERROR,
            "ErrorDecoy.java": _ERROR_DECOY,
            "Main.java": (
                "package com.example;\n"
                "public class Main {\n"
                "  String first() {\n"
                "    try {\n"
                "      throw new AppError();\n"
                "    } catch (AppError e) {\n"
                "      return e.detail();\n"
                "    }\n"
                "  }\n"
                "  String second() {\n"
                "    try {\n"
                "      throw new OtherError();\n"
                "    } catch (OtherError e) {\n"
                "      return e.detail();\n"
                "    }\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    calls = _calls(mock_ingestor)
    _assert_calls(calls, ".Main.first()", ".AppError.detail()", ".ErrorDecoy.detail()")
    _assert_calls(
        calls, ".Main.second()", ".OtherError.detail()", ".ErrorDecoy.detail()"
    )


def test_qualified_jdk_multi_catch_never_binds_first_party(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The Greptile trap (case 12): qualified JDK alternatives must not be
    # re-identified as the same-named first-party classes, even though those
    # classes uniquely share the simple names and a common supertype.
    root = temp_repo / "jgdktrap"
    _write_pkg(
        root,
        {
            "BaseError.java": (
                "package com.example;\n"
                "public class BaseError extends RuntimeException {\n"
                '  public String detail() { return "base"; }\n'
                "}\n"
            ),
            "IOException.java": (
                "package com.example;\n"
                "public class IOException extends BaseError {\n"
                '  public String detail() { return "io"; }\n'
                "}\n"
            ),
            "SQLException.java": (
                "package com.example;\n"
                "public class SQLException extends BaseError {\n"
                '  public String detail() { return "sql"; }\n'
                "}\n"
            ),
            "Main.java": (
                "package com.example;\n"
                "public class Main {\n"
                "  String jdkPair() {\n"
                "    try {\n"
                "      throw new RuntimeException();\n"
                "    } catch (java.io.IOException | java.sql.SQLException e) {\n"
                "      return e.detail();\n"
                "    }\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    calls = _calls(mock_ingestor)
    targets = {
        t for f, t in calls if f.endswith(".Main.jdkPair()") and t.endswith(".detail()")
    }
    assert targets == set(), sorted(targets)


def test_catch_parameter_shadows_field_inside_catch_block(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Case 23: a typed catch parameter overwrites the field binding in the
    # flat map, so the call inside the catch block binds the catch type.
    root = temp_repo / "jcatchshadow"
    _write_pkg(
        root,
        {
            "AppError.java": _APP_ERROR,
            "OtherError.java": _OTHER_ERROR,
            "Other.java": (
                "package com.example;\n"
                "public class Other {\n"
                '  public String detail() { return "other"; }\n'
                "}\n"
            ),
            "Main.java": (
                "package com.example;\n"
                "public class Main {\n"
                "  Other e = new Other();\n"
                "  String shadowedCatch() {\n"
                "    try {\n"
                "      throw new AppError();\n"
                "    } catch (AppError e) {\n"
                "      return e.detail();\n"
                "    }\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    _assert_calls(
        _calls(mock_ingestor),
        ".Main.shadowedCatch()",
        ".AppError.detail()",
        ".Other.detail()",
    )


def test_nested_resource_shadows_field(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Nested shadowing in a legal form: the inner try-resource shadows a
    # FIELD of the same name (locals cannot shadow enclosing locals in
    # compilable Java). The call binds the inner resource's type.
    root = temp_repo / "jnestedshadow"
    _write_pkg(
        root,
        {
            "Res.java": _RES,
            "Other.java": _OTHER,
            "Main.java": (
                "package com.example;\n"
                "public class Main {\n"
                "  Other r = new Other();\n"
                "  int nestedShadowed() throws Exception {\n"
                "    try (Res a = new Res()) {\n"
                "      try (Res r = new Res()) {\n"
                "        return r.read();\n"
                "      }\n"
                "    }\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    _assert_calls(
        _calls(mock_ingestor),
        ".Main.nestedShadowed()",
        ".Res.read()",
        ".Other.read()",
    )


def test_inner_catch_parameter_shadows_field(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Nested shadowing in a legal form: an inner catch parameter shadows a
    # FIELD of the same name, nested inside an outer try-with-resources.
    root = temp_repo / "jnestedcatchshadow"
    _write_pkg(
        root,
        {
            "Res.java": _RES,
            "AppError.java": _APP_ERROR,
            "OtherError.java": _OTHER_ERROR,
            "Other.java": (
                "package com.example;\n"
                "public class Other {\n"
                '  public String detail() { return "other"; }\n'
                "}\n"
            ),
            "Main.java": (
                "package com.example;\n"
                "public class Main {\n"
                "  Other e = new Other();\n"
                "  String nestedCatchShadowed() throws Exception {\n"
                "    try (Res a = new Res()) {\n"
                "      try {\n"
                "        throw new AppError();\n"
                "      } catch (AppError e) {\n"
                "        return e.detail();\n"
                "      }\n"
                "    }\n"
                "  }\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="java")
    _assert_calls(
        _calls(mock_ingestor),
        ".Main.nestedCatchShadowed()",
        ".AppError.detail()",
        ".Other.detail()",
    )
