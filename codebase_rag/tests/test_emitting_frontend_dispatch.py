# Generic dispatch of EMITTING_FRONTENDS by declared phase (issue #1460).
#
# The registry is keyed by language and reads as a general mechanism, but the
# graph builder looked up one hardcoded key, so a frontend registered for any
# other language was silently never called -- registration succeeded, emit()
# never ran, and its nodes were simply absent with nothing to diagnose.
#
# These tests register a dummy frontend and assert its marker node REACHES the
# graph. Asserting only that indexing does not crash passes against the broken
# behaviour, which is the whole failure mode: nothing errors.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.frontends import (
    EMITTING_FRONTENDS,
    FrontendEmitContext,
    FrontendEmitResult,
    FrontendPhase,
)

_MARKER_LABEL = cs.NodeLabel.MODULE.value


class _RecordingFrontend:
    """A minimal emitting frontend that writes one identifiable node."""

    def __init__(self, language: cs.SupportedLanguage, phase: FrontendPhase) -> None:
        self.language = language
        self.phase = phase
        self.emitted = False

    def available(self) -> bool:
        return True

    def applies(self, repo_path: Path) -> bool:
        return True

    def emit(self, ctx: FrontendEmitContext) -> FrontendEmitResult:
        self.emitted = True
        ctx.ingestor.ensure_node_batch(
            _MARKER_LABEL, {cs.KEY_QUALIFIED_NAME: "marker.from.frontend"}
        )
        return FrontendEmitResult()


def _index(tmp_path: Path) -> MagicMock:
    (tmp_path / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    parsers, queries = load_parsers()
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock, repo_path=tmp_path, parsers=parsers, queries=queries
    ).run()
    return mock


def _emitted_qns(mock: MagicMock) -> set[str]:
    return {
        str(call.args[1].get(cs.KEY_QUALIFIED_NAME))
        for call in mock.ensure_node_batch.call_args_list
        if str(call.args[0]) == _MARKER_LABEL
    }


def _register(monkeypatch, frontend: _RecordingFrontend) -> None:
    patched = dict(EMITTING_FRONTENDS)
    patched[frontend.language] = frontend
    monkeypatch.setattr(
        "codebase_rag.graph_updater.EMITTING_FRONTENDS", patched, raising=False
    )


def test_a_registered_frontend_for_any_language_is_invoked(
    tmp_path: Path, monkeypatch
) -> None:
    """Registration must be sufficient, for a language other than C++.

    The previous lookup was `EMITTING_FRONTENDS.get(SupportedLanguage.CPP)`,
    so this frontend was never called and its node never written. Nothing
    raised, which is why the assertion is on the emitted node rather than on
    the absence of an exception.
    """
    frontend = _RecordingFrontend(
        cs.SupportedLanguage.RUST, FrontendPhase.AFTER_DEFINITIONS
    )
    _register(monkeypatch, frontend)

    mock = _index(tmp_path)

    assert frontend.emitted, "emit() was never called"
    assert "marker.from.frontend" in _emitted_qns(mock)


def test_a_before_definitions_frontend_is_invoked(tmp_path: Path, monkeypatch) -> None:
    """Both phases dispatch, not just the one C++ happens to use by default.

    A dispatch loop wired into only one call site would pass the test above
    and silently drop every frontend declaring the other phase.
    """
    frontend = _RecordingFrontend(
        cs.SupportedLanguage.RUST, FrontendPhase.BEFORE_DEFINITIONS
    )
    _register(monkeypatch, frontend)

    mock = _index(tmp_path)

    assert frontend.emitted, "emit() was never called"
    assert "marker.from.frontend" in _emitted_qns(mock)


def test_an_unavailable_frontend_is_not_invoked(tmp_path: Path, monkeypatch) -> None:
    """`available()` gates the call, so a missing toolchain degrades quietly.

    The paired negative: without it, a dispatch loop that ignored availability
    would pass both tests above while crashing on every machine lacking the
    tool.
    """
    frontend = _RecordingFrontend(
        cs.SupportedLanguage.RUST, FrontendPhase.AFTER_DEFINITIONS
    )
    frontend.available = lambda: False  # type: ignore[method-assign]
    _register(monkeypatch, frontend)

    mock = _index(tmp_path)

    assert not frontend.emitted
    assert "marker.from.frontend" not in _emitted_qns(mock)


def test_a_frontend_that_does_not_apply_is_not_invoked(
    tmp_path: Path, monkeypatch
) -> None:
    """`applies()` gates it too: no project file, no run."""
    frontend = _RecordingFrontend(
        cs.SupportedLanguage.RUST, FrontendPhase.AFTER_DEFINITIONS
    )
    frontend.applies = lambda repo_path: False  # type: ignore[method-assign]
    _register(monkeypatch, frontend)

    mock = _index(tmp_path)

    assert not frontend.emitted
    assert "marker.from.frontend" not in _emitted_qns(mock)


def test_a_frontend_whose_probe_raises_is_skipped(tmp_path: Path, monkeypatch) -> None:
    """A raising `available()` must not fail the whole index.

    The protocol's invariant is that a missing toolchain degrades to the
    tree-sitter backbone, never worse. A frontend whose PROBE throws -- a
    broken install, a permissions error reading a marker -- would otherwise
    take the entire run down with it, which is the opposite of degrading.

    Asserting the other frontends still run is what makes this meaningful:
    a dispatch loop that swallowed the exception and then stopped iterating
    would satisfy "no crash" while silently dropping every later frontend.
    """
    raiser = _RecordingFrontend(
        cs.SupportedLanguage.RUST, FrontendPhase.AFTER_DEFINITIONS
    )
    raiser.available = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
        RuntimeError("broken install")
    )
    survivor = _RecordingFrontend(
        cs.SupportedLanguage.JAVA, FrontendPhase.AFTER_DEFINITIONS
    )

    patched = dict(EMITTING_FRONTENDS)
    patched[raiser.language] = raiser
    patched[survivor.language] = survivor
    monkeypatch.setattr(
        "codebase_rag.graph_updater.EMITTING_FRONTENDS", patched, raising=False
    )

    mock = _index(tmp_path)

    assert not raiser.emitted, "a raising probe should skip its frontend"
    assert survivor.emitted, "one broken frontend stopped the others running"
    assert "marker.from.frontend" in _emitted_qns(mock)
