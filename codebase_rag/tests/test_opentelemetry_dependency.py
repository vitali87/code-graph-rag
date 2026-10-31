# GHSA-xcq7-p3pf-p88h (CVE-2026-114603): opentelemetry-api <= 1.45.0 split the
# W3C tracestate header with a regex that costs O(N^2) on a run of spaces or
# tabs not followed by a comma, and never length-checked the header first, so
# one request through propagate.extract() could burn CPU. 1.45.1 parses in
# linear time and drops combined tracestate headers over 8192 characters.
# pydantic-ai-slim pulls the API into the runtime closure; semgrep (dev group)
# pins it ~=1.37.0, which the override in pyproject.toml lifts.
from __future__ import annotations

from importlib.metadata import version

from opentelemetry import propagate, trace
from opentelemetry.trace import SpanContext
from packaging.version import Version

_TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
_TRACE_ID = 0x0AF7651916CD43DD8448EB211C80319C
_MEMBER = "vendor=value"
_MAXIMUM_TRACESTATE_LENGTH = 8192


def _extract(tracestate: str) -> SpanContext:
    context = propagate.extract({"traceparent": _TRACEPARENT, "tracestate": tracestate})
    return trace.get_current_span(context).get_span_context()


def test_opentelemetry_api_dependency_is_patched() -> None:
    assert Version(version("opentelemetry-api")) >= Version("1.45.1"), version(
        "opentelemetry-api"
    )


def test_tracestate_at_the_length_cap_is_kept() -> None:
    # Known positive: a header the cap allows still yields its member, so the
    # empty state below is the cap firing, not a header that never parses.
    padded = _MEMBER + " " * (_MAXIMUM_TRACESTATE_LENGTH - len(_MEMBER))
    span_context = _extract(padded)
    assert dict(span_context.trace_state) == {"vendor": "value"}


def test_oversized_tracestate_is_dropped_before_parsing() -> None:
    padded = _MEMBER + " " * _MAXIMUM_TRACESTATE_LENGTH
    span_context = _extract(padded)
    # The traceparent still propagates; only the oversized tracestate is shed.
    assert span_context.trace_id == _TRACE_ID
    assert dict(span_context.trace_state) == {}
