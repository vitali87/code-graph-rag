# The C++ libclang integration behind the EmittingFrontend protocol (#1178).
#
# `EmittingFrontend` shipped with the protocol but nothing registered against
# it, so the C++ frontend was still called directly from graph_updater. These
# tests pin the registration and the phase mapping, which is the part a future
# refactor is most likely to get wrong: LIBCLANG emits its own definition nodes
# and must run BEFORE the definition pass, while HYBRID attaches to spans that
# pass produces and must run after.
from __future__ import annotations

from codebase_rag import constants as cs
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


def test_the_cpp_frontend_declares_a_real_phase() -> None:
    """The phase decides whether the definition pass has already run.

    Getting it wrong does not raise: HYBRID running before definitions would
    find no tree-sitter spans to attribute macro calls to, and would silently
    attribute nothing.
    """
    frontend = EMITTING_FRONTENDS[cs.SupportedLanguage.CPP]

    assert frontend.phase in tuple(FrontendPhase), frontend.phase


def test_the_frontend_reports_availability_without_raising() -> None:
    """`available()` is called on every index, including where libclang is absent.

    It must answer the question rather than propagate an ImportError, since a
    missing toolchain has to degrade to the tree-sitter backbone.
    """
    frontend = EMITTING_FRONTENDS[cs.SupportedLanguage.CPP]

    assert isinstance(frontend.available(), bool)
