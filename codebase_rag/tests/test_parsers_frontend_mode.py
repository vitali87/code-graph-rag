"""`resolve_frontend_mode`: the explicitly disabled mode stays as set without
probing the toolchain, every backed mode degrades to the fallback without
one, and AUTO (or, for a two-mode language, any backed mode) resolves to
the preferred backend with one."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.frontend_mode import resolve_frontend_mode


def _toolchain(present: bool, probes: list[bool]) -> Callable[[], bool]:
    def available() -> bool:
        probes.append(present)
        return present

    return available


def _go(mode: cs.GoFrontend, present: bool, probes: list[bool]) -> cs.GoFrontend:
    return resolve_frontend_mode(
        mode,
        cs.GoFrontend.TREESITTER,
        _toolchain(present, probes),
        auto=cs.GoFrontend.AUTO,
        auto_resolves_to=cs.GoFrontend.GOTYPES,
    )


@pytest.mark.parametrize("present", [True, False])
def test_the_fallback_mode_stays_and_never_probes_the_toolchain(
    present: bool,
) -> None:
    probes: list[bool] = []
    assert _go(cs.GoFrontend.TREESITTER, present, probes) is cs.GoFrontend.TREESITTER
    assert probes == []


@pytest.mark.parametrize("mode", [cs.GoFrontend.AUTO, cs.GoFrontend.GOTYPES])
def test_every_backed_mode_degrades_to_the_fallback_without_a_toolchain(
    mode: cs.GoFrontend,
) -> None:
    probes: list[bool] = []
    assert _go(mode, False, probes) is cs.GoFrontend.TREESITTER
    assert probes == [False]


def test_auto_means_the_preferred_backend_with_a_toolchain() -> None:
    assert _go(cs.GoFrontend.AUTO, True, []) is cs.GoFrontend.GOTYPES


def test_an_explicit_backed_mode_is_kept_rather_than_read_as_auto() -> None:
    # C# has two toolchain-backed modes; asking for ROSLYN alone must not be
    # widened to HYBRID, which is only what AUTO means.
    resolved = resolve_frontend_mode(
        cs.CSharpFrontend.ROSLYN,
        cs.CSharpFrontend.TREESITTER,
        _toolchain(True, []),
        auto=cs.CSharpFrontend.AUTO,
        auto_resolves_to=cs.CSharpFrontend.HYBRID,
    )
    assert resolved is cs.CSharpFrontend.ROSLYN


@pytest.mark.parametrize(
    ("present", "expected"),
    [(True, cs.JavaFrontend.JAVAC), (False, cs.JavaFrontend.HEURISTIC)],
)
def test_a_two_mode_language_resolves_its_backed_mode_without_an_auto(
    present: bool, expected: cs.JavaFrontend
) -> None:
    resolved = resolve_frontend_mode(
        cs.JavaFrontend.JAVAC,
        cs.JavaFrontend.HEURISTIC,
        _toolchain(present, []),
        auto_resolves_to=cs.JavaFrontend.JAVAC,
    )
    assert resolved is expected


def test_without_a_preferred_backend_auto_stays_auto() -> None:
    resolved = resolve_frontend_mode(
        cs.GoFrontend.AUTO,
        cs.GoFrontend.TREESITTER,
        _toolchain(True, []),
        auto=cs.GoFrontend.AUTO,
    )
    assert resolved is cs.GoFrontend.AUTO
