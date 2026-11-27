"""Shared time bounds sized for loaded coverage runs (issue #3674)."""

# Thread hand-off allowance (`Event.wait`, `Lock.acquire`, `Future.result`):
# large enough to ride out scheduler stalls under a full `-n auto --cov` run
# while still failing a real deadlock.
HANDOFF_TIMEOUT_S = 30.0

_COVERAGE_CPU_MULTIPLIER = 3


def _coverage_active() -> bool:
    try:
        import coverage
    except ImportError:
        return False
    cls = getattr(coverage, "Coverage", None)
    current = getattr(cls, "current", None)
    return callable(current) and current() is not None


def cpu_ceiling(nominal_seconds: float) -> float:
    """Return a CPU-time ceiling, widened while a coverage session is active."""
    if _coverage_active():
        return nominal_seconds * _COVERAGE_CPU_MULTIPLIER
    return nominal_seconds
