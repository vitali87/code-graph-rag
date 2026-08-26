# The C++ libclang integration behind the EmittingFrontend protocol (#1178).
#
# `EmittingFrontend` shipped with the protocol but nothing registered against
# it, so the C++ frontend was still called directly from graph_updater. These
# tests pin the registration and the phase mapping, which is the part a future
# refactor is most likely to get wrong: LIBCLANG emits its own definition nodes
# and must run BEFORE the definition pass, while HYBRID attaches to spans that
# pass produces and must run after.
from __future__ import annotations

from unittest.mock import patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.parsers.frontends import EMITTING_FRONTENDS, FrontendPhase


def test_cpp_registers_an_emitting_frontend() -> None:
    """C++ is the protocol's intended consumer and must actually register.

    `EmittingFrontend` exists precisely for a frontend that writes graph
    elements rather than returning facts; a protocol with no implementer is
    indistinguishable from one that does not work.
    """
    assert cs.SupportedLanguage.CPP in EMITTING_FRONTENDS, sorted(
        lang.value for lang in EMITTING_FRONTENDS
    )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        # LIBCLANG emits its own definition nodes and reports covered files, so
        # the definition pass must not have run yet.
        (cs.CppFrontend.LIBCLANG, FrontendPhase.BEFORE_DEFINITIONS),
        # HYBRID attributes macro uses to tree-sitter spans, which do not exist
        # until the definition pass has produced them.
        (cs.CppFrontend.HYBRID, FrontendPhase.AFTER_DEFINITIONS),
    ],
)
def test_each_cpp_mode_maps_to_its_own_phase(
    mode: cs.CppFrontend, expected: FrontendPhase
) -> None:
    """Each mode must map to its SPECIFIC phase, not merely to a valid one.

    Asserting `phase in FrontendPhase` is satisfied by both the correct
    mapping and an inverted one, so it cannot detect the regression it exists
    to prevent. Greptile demonstrated exactly that: swapping the two values
    left the weaker assertion green.

    The consequence of an inverted mapping is silent. HYBRID running before
    the definition pass finds no tree-sitter spans to attribute macro calls
    to and attributes nothing; nothing raises, and the graph is quietly
    missing every macro edge.
    """
    frontend = EMITTING_FRONTENDS[cs.SupportedLanguage.CPP]

    with patch.object(settings, "CPP_FRONTEND", mode):
        assert frontend.phase is expected, f"{mode.value} -> {frontend.phase}"


def test_the_frontend_reports_availability_without_raising() -> None:
    """`available()` is called on every index, including where libclang is absent.

    It must answer the question rather than propagate an ImportError, since a
    missing toolchain has to degrade to the tree-sitter backbone.
    """
    frontend = EMITTING_FRONTENDS[cs.SupportedLanguage.CPP]

    assert isinstance(frontend.available(), bool)
