"""Gate predicates for live-orchestrator integration tests.

Kept outside the test modules so unit tests can import them without
triggering test-module side effects (e.g. Loguru sink reconfiguration).
"""

from __future__ import annotations


def orchestrator_reliably_tool_calls() -> bool:
    # Small local models emit tool calls as JSON text often enough that
    # asserting on executed tools tests the model, not our wiring.
    from codebase_rag import constants as cs
    from codebase_rag.config import settings

    return settings.active_orchestrator_config.provider != cs.Provider.OLLAMA
