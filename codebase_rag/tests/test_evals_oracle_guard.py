"""`evals.oracle_guard` is how every standalone eval ends when its oracle
cannot run: one stated reason on the error log and exit code 1, never a
traceback and never a message that misnames the obstacle (issue #1639)."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
import typer
from loguru import logger

from evals.oracle_guard import oracle_unavailable_exits, require_oracle
from evals.oracles import NodeOracleUnavailable

_MISSING = "{binary} not found on PATH"


@pytest.fixture
def errors() -> Iterator[list[str]]:
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="ERROR")
    yield lines
    logger.remove(sink)


def _exit(run: Callable[[], None]) -> typer.Exit:
    # Caught explicitly: `conftest.pytest_runtest_call` turns a leaked
    # NodeOracleUnavailable into a SKIP, which would hide the regression.
    try:
        with pytest.raises(typer.Exit) as exit_info:
            run()
    except NodeOracleUnavailable as leaked:
        raise AssertionError(
            f"the guard let the exception escape: {leaked}"
        ) from leaked
    return exit_info.value


def test_an_available_oracle_passes_without_asking_why(errors: list[str]) -> None:
    def never() -> str | None:
        raise AssertionError("the reason probe ran for an available oracle")

    require_oracle(lambda: True, _MISSING, "node", skip_reason=never)
    assert errors == []


def test_a_missing_oracle_names_its_binary(errors: list[str]) -> None:
    assert _exit(lambda: require_oracle(lambda: False, _MISSING, "node")).exit_code == 1
    assert errors == ["node not found on PATH"]


def test_a_probe_reason_replaces_the_fixed_message(errors: list[str]) -> None:
    # The binary is present but cannot load the parser: "not found on PATH"
    # would be false.
    reason = "this node cannot require(stub): ERR_REQUIRE_ESM"
    exited = _exit(
        lambda: require_oracle(
            lambda: False, _MISSING, "node", skip_reason=lambda: reason
        )
    )
    assert exited.exit_code == 1
    assert errors == [f"Oracle unavailable: {reason}"]


def test_a_probe_with_no_reason_falls_back_to_the_fixed_message(
    errors: list[str],
) -> None:
    exited = _exit(
        lambda: require_oracle(
            lambda: False, _MISSING, "dotnet", skip_reason=lambda: None
        )
    )
    assert exited.exit_code == 1
    assert errors == ["dotnet not found on PATH"]


def test_a_message_with_no_binary_field_is_logged_as_written(
    errors: list[str],
) -> None:
    # libclang loads in-process, so its message names no binary.
    exited = _exit(lambda: require_oracle(lambda: False, "libclang is unavailable"))
    assert exited.exit_code == 1
    assert errors == ["libclang is unavailable"]


def test_an_oracle_failing_after_install_exits_with_its_reason(
    errors: list[str],
) -> None:
    unavailable = NodeOracleUnavailable("tree-sitter-php did not build")

    def run() -> None:
        with oracle_unavailable_exits():
            raise unavailable

    exited = _exit(run)
    assert exited.exit_code == 1
    assert exited.__cause__ is unavailable
    assert errors == ["Oracle unavailable: tree-sitter-php did not build"]


def test_other_failures_pass_through_and_a_clean_run_is_untouched(
    errors: list[str],
) -> None:
    with pytest.raises(ValueError, match="a real bug"):
        with oracle_unavailable_exits():
            raise ValueError("a real bug")
    with oracle_unavailable_exits():
        pass
    assert errors == []
