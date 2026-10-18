"""Every standalone eval must REPORT an unusable oracle, not crash or misname it.

The L1 driver learned both halves of issue #1639: prefer the probe's own
reason over the fixed "not found on PATH" (false when the binary is present
but too old), and turn the post-install `NodeOracleUnavailable` into a clean
exit. The commands that kept a private copy of that guard predated it, so an
unusable toolchain printed a misdiagnosis or a traceback there.

Each test drives a command's real `main` with the ORACLE module's seams
patched, so it exercises whatever the command actually calls rather than a
reimplementation of its guard.
"""

from __future__ import annotations

import importlib
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest
import typer
from loguru import logger

from evals import constants as ec
from evals.oracles import (
    NodeOracleUnavailable,
    csharp_oracle,
    java_oracle,
    lua_oracle,
    php_oracle,
    typescript_oracle,
)

_REASON = "this node cannot require(stub): ERR_REQUIRE_ESM"
_OLD_SDK = "8.0.130 [/usr/share/dotnet/sdk]"
_FIXED_MESSAGE = "not found on PATH"

_NODE_RETRIEVAL = [
    ("evals.ts_retrieval", typescript_oracle),
    ("evals.js_retrieval", typescript_oracle),
    ("evals.lua_retrieval", lua_oracle),
    ("evals.php_retrieval", php_oracle),
]


@pytest.fixture(autouse=True)
def _fresh_csharp_probe() -> Iterator[None]:
    # The C# probe is cached with no arguments, so a verdict cached under one
    # test's fake toolchain would answer for the next test and for production
    # code in the same process.
    csharp_oracle.csharp_oracle_skip_reason.cache_clear()
    yield
    csharp_oracle.csharp_oracle_skip_reason.cache_clear()


def _run_main(module_name: str, tmp_path: Path) -> Callable[[], None]:
    module = importlib.import_module(module_name)
    return lambda: module.main(
        target=tmp_path, project_name="", out_dir=tmp_path / "out"
    )


def _exit_and_errors(run: Callable[[], None]) -> tuple[int, str]:
    messages: list[str] = []
    sink = logger.add(messages.append, level="ERROR")
    try:
        # Caught explicitly: `conftest.pytest_runtest_call` turns a leaked
        # NodeOracleUnavailable into a SKIP, so a command that stopped
        # catching it would make the test vanish rather than fail.
        with pytest.raises(typer.Exit) as exit_info:
            run()
    except NodeOracleUnavailable as leaked:
        raise AssertionError(
            f"the command let the exception escape: {leaked}"
        ) from leaked
    finally:
        logger.remove(sink)
    return int(exit_info.value.exit_code), "\n".join(messages)


def _node_unusable(monkeypatch: pytest.MonkeyPatch, oracle: ModuleType) -> None:
    monkeypatch.setattr(oracle, "node_oracle_available", lambda *_: False)
    monkeypatch.setattr(oracle, "node_oracle_skip_reason", lambda *_: _REASON)


def _node_fails_after_install(
    monkeypatch: pytest.MonkeyPatch, oracle: ModuleType
) -> None:
    """The clean-checkout path: the guard cannot tell yet, the run can."""

    def _unavailable(*_args: object) -> None:
        raise NodeOracleUnavailable(_REASON)

    monkeypatch.setattr(oracle, "node_oracle_available", lambda *_: True)
    monkeypatch.setattr(oracle, "run_node_oracle_payload", _unavailable)


def _dotnet_too_old(monkeypatch: pytest.MonkeyPatch) -> None:
    """dotnet IS on PATH, but its only SDK is older than the csproj needs."""

    def _list_sdks(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, 0, stdout=f"{_OLD_SDK}\n", stderr="")

    monkeypatch.setattr(csharp_oracle.shutil, "which", lambda name: f"/stub/{name}")
    monkeypatch.setattr(csharp_oracle.subprocess, "run", _list_sdks)


@pytest.mark.parametrize(("module_name", "oracle"), _NODE_RETRIEVAL)
def test_retrieval_names_the_probe_reason(
    module_name: str,
    oracle: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _node_unusable(monkeypatch, oracle)

    code, errors = _exit_and_errors(_run_main(module_name, tmp_path))

    assert code == 1
    assert _REASON in errors
    # Absent as well as the reason present: a guard that logged both would
    # still tell the reader the binary is missing.
    assert _FIXED_MESSAGE not in errors, errors


@pytest.mark.parametrize(("module_name", "oracle"), _NODE_RETRIEVAL)
def test_retrieval_exits_on_a_post_install_failure(
    module_name: str,
    oracle: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _node_fails_after_install(monkeypatch, oracle)

    code, errors = _exit_and_errors(_run_main(module_name, tmp_path))

    assert code == 1
    assert _REASON in errors
    assert not (tmp_path / "out").exists()


def test_csharp_retrieval_names_the_old_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dotnet_too_old(monkeypatch)

    code, errors = _exit_and_errors(_run_main("evals.csharp_retrieval", tmp_path))

    assert code == 1
    assert _OLD_SDK in errors
    assert _FIXED_MESSAGE not in errors, errors


def test_js_l1_names_the_probe_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ts_l1 passed the reason probe; js_l1 shares the same tsc oracle and
    # guard but did not, so the same toolchain read as "not found" there.
    _node_unusable(monkeypatch, typescript_oracle)

    code, errors = _exit_and_errors(_run_main("evals.js_l1", tmp_path))

    assert code == 1
    assert _REASON in errors
    assert _FIXED_MESSAGE not in errors, errors


def test_csharp_l1_names_the_old_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dotnet_too_old(monkeypatch)

    code, errors = _exit_and_errors(_run_main("evals.csharp_l1", tmp_path))

    assert code == 1
    assert _OLD_SDK in errors
    assert _FIXED_MESSAGE not in errors, errors


def _run_inheritance(tmp_path: Path, language: str) -> Callable[[], None]:
    from evals import inheritance

    return lambda: inheritance.main(
        target=tmp_path,
        project_name="",
        out_dir=tmp_path / "out",
        language=ec.InheritanceLanguage(language),
    )


def _java_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(java_oracle, "_toolchain_runs", lambda _binary: False)


def _node_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(typescript_oracle, "node_oracle_available", lambda *_: False)
    monkeypatch.setattr(typescript_oracle, "node_oracle_skip_reason", lambda *_: None)


@pytest.mark.parametrize(
    ("language", "make_missing", "binary"),
    [
        (ec.InheritanceLanguage.JAVA, _java_missing, ec.JAVAC_BIN),
        (ec.InheritanceLanguage.TYPESCRIPT, _node_missing, ec.NODE_BIN),
    ],
)
def test_inheritance_names_the_missing_binary(
    language: str,
    make_missing: Callable[[pytest.MonkeyPatch], None],
    binary: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The arm that reports a missing toolchain is the one nobody who tested
    # the language ever sees, which is how a raw `{binary}` survived here
    # after the L1 driver was fixed for it (issue #1518).
    make_missing(monkeypatch)

    code, errors = _exit_and_errors(_run_inheritance(tmp_path, language))

    assert code == 1
    assert "{binary}" not in errors, errors
    assert f"'{binary}'" in errors, errors


def test_typescript_inheritance_names_the_probe_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _node_unusable(monkeypatch, typescript_oracle)

    code, errors = _exit_and_errors(
        _run_inheritance(tmp_path, ec.InheritanceLanguage.TYPESCRIPT)
    )

    assert code == 1
    assert _REASON in errors
    assert _FIXED_MESSAGE not in errors, errors


def test_typescript_inheritance_exits_on_a_post_install_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _node_fails_after_install(monkeypatch, typescript_oracle)

    code, errors = _exit_and_errors(
        _run_inheritance(tmp_path, ec.InheritanceLanguage.TYPESCRIPT)
    )

    assert code == 1
    assert _REASON in errors
    assert not (tmp_path / "out").exists()


def test_csharp_inheritance_names_the_old_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dotnet_too_old(monkeypatch)

    code, errors = _exit_and_errors(
        _run_inheritance(tmp_path, ec.InheritanceLanguage.CSHARP)
    )

    assert code == 1
    assert _OLD_SDK in errors
    assert _FIXED_MESSAGE not in errors, errors
