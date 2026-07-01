from __future__ import annotations

import mgclient  # ty: ignore[unresolved-import]
import pytest

from codebase_rag.tools.health_checker import HealthChecker


def test_check_memgraph_connection_returns_failure_when_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_operational_error(**_: object) -> object:
        raise mgclient.OperationalError("connection refused")

    monkeypatch.setattr(mgclient, "connect", raise_operational_error)

    result = HealthChecker().check_memgraph_connection()

    assert result.passed is False
