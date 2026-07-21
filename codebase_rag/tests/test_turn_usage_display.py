# Unit tests for the per-turn token/cost display helpers (issue #80). No DB or
# LLM call: they drive the accumulator and formatting directly.
from __future__ import annotations

import io
from decimal import Decimal

from rich.console import Console

from codebase_rag import main
from codebase_rag.models import SessionState


def _fresh_session() -> tuple[SessionState, io.StringIO]:
    session = SessionState()
    main.app_context.session = session
    buffer = io.StringIO()
    main.app_context.console = Console(file=buffer, force_terminal=False, no_color=True)
    return session, buffer


def test_accumulates_tokens_and_cost_across_turns() -> None:
    session, _ = _fresh_session()
    main._record_and_print_turn_usage(1000, 500, Decimal("0.01"), turn_priced=True)
    main._record_and_print_turn_usage(200, 100, Decimal("0.002"), turn_priced=True)
    assert session.total_input_tokens == 1200
    assert session.total_output_tokens == 600
    assert session.total_cost_usd == Decimal("0.012")
    assert session.cost_known is True


def test_cost_stays_hidden_until_a_turn_is_priced() -> None:
    _, buffer = _fresh_session()
    main._record_and_print_turn_usage(10, 5, Decimal(0), turn_priced=False)
    out = buffer.getvalue()
    assert "tokens" in out
    assert "$" not in out


def test_priced_turn_shows_cost_segment() -> None:
    _, buffer = _fresh_session()
    main._record_and_print_turn_usage(1000, 500, Decimal("0.0105"), turn_priced=True)
    assert "$0.0105" in buffer.getvalue()


def test_price_current_run_none_when_no_config(monkeypatch) -> None:
    class _Boom:
        @property
        def active_orchestrator_config(self):  # noqa: ANN202
            raise RuntimeError("no config")

    monkeypatch.setattr(main, "settings", _Boom())
    assert main._price_current_run(object()) is None
