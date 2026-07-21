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


def test_cost_stays_hidden_until_a_turn_is_priced() -> None:
    _, buffer = _fresh_session()
    main._record_and_print_turn_usage(10, 5, Decimal(0), turn_priced=False)
    out = buffer.getvalue()
    assert "tokens" in out
    assert "$" not in out


def test_unpriced_turn_after_priced_turn_hides_cost() -> None:
    # Switching to a local/unknown model mid-session must not render its
    # unpriced turn as a known $0 cost just because an earlier turn was priced.
    _, buffer = _fresh_session()
    main._record_and_print_turn_usage(1000, 500, Decimal("0.01"), turn_priced=True)
    buffer.truncate(0)
    buffer.seek(0)
    main._record_and_print_turn_usage(200, 100, Decimal(0), turn_priced=False)
    out = buffer.getvalue()
    assert "tokens" in out
    assert "$" not in out


def test_priced_turn_shows_cost_segment() -> None:
    _, buffer = _fresh_session()
    main._record_and_print_turn_usage(1000, 500, Decimal("0.0105"), turn_priced=True)
    assert "$0.0105" in buffer.getvalue()


def test_session_total_marked_partial_after_an_unpriced_turn() -> None:
    # priced -> unpriced -> priced: the final session total omits the unpriced
    # turn, so it must be shown as a partial floor, not a definitive total.
    _, buffer = _fresh_session()
    main._record_and_print_turn_usage(1000, 500, Decimal("0.01"), turn_priced=True)
    main._record_and_print_turn_usage(200, 100, Decimal(0), turn_priced=False)
    buffer.truncate(0)
    buffer.seek(0)
    main._record_and_print_turn_usage(300, 150, Decimal("0.003"), turn_priced=True)
    out = buffer.getvalue()
    assert "partial" in out
    assert "$0.0130+" in out


def test_price_current_run_none_when_no_config(monkeypatch) -> None:
    class _Boom:
        @property
        def active_orchestrator_config(self):  # noqa: ANN202
            raise RuntimeError("no config")

    monkeypatch.setattr(main, "settings", _Boom())
    assert main._price_current_run(object(), None) is None


def test_price_current_run_uses_override_config(monkeypatch) -> None:
    # After a /model switch, pricing must follow the override config, not the
    # default orchestrator config. Boom settings prove settings is not consulted.
    from pydantic_ai.usage import RunUsage

    from codebase_rag.config import ModelConfig

    class _Boom:
        @property
        def active_orchestrator_config(self):  # noqa: ANN202
            raise RuntimeError("settings must not be read when an override exists")

    monkeypatch.setattr(main, "settings", _Boom())
    override = ModelConfig(provider="anthropic", model_id="claude-sonnet-4-5")
    usage = RunUsage(input_tokens=1000, output_tokens=500)
    cost = main._price_current_run(usage, override)
    assert cost is not None
    assert cost > 0
