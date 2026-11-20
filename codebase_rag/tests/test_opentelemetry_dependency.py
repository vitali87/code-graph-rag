# GHSA-xcq7-p3pf-p88h (CVE-2026-114603): opentelemetry-api <= 1.45.0 split the
# W3C tracestate header with a regex that costs O(N^2) on a run of spaces or
# tabs not followed by a comma, and never length-checked the header first, so
# one request through propagate.extract() could burn CPU. 1.45.1 parses in
# linear time and drops combined tracestate headers over 8192 characters.
#
# GHSA-mf5v-w3w3-f6cg (CVE-2026-126360): before 1.45.2 the traceparent regex
# backtracks in O(N^2) on a trace-context prefix, a long run of whitespace and
# an embedded newline. HTTP headers cannot carry the newline, but JSON can, and
# the mcp server extracts trace context from every request's `_meta` by
# default. 1.45.2 drops a traceparent over 512 characters before matching.
#
# pydantic-ai-slim and mcp pull the API into the runtime closure; pyproject
# declares the floor too, because a pip upgrade over an older install keeps
# whatever version still satisfies them.
from __future__ import annotations

import tomllib
from importlib.metadata import version
from pathlib import Path

from opentelemetry import propagate, trace
from opentelemetry.trace import SpanContext
from packaging.requirements import Requirement
from packaging.version import Version

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
_PACKAGE = "opentelemetry-api"
_PATCHED = "1.45.2"
_LAST_VULNERABLE = "1.45.1"
_TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
_TRACE_ID = 0x0AF7651916CD43DD8448EB211C80319C
_MEMBER = "vendor=value"
_MAXIMUM_TRACESTATE_LENGTH = 8192
_MAXIMUM_TRACEPARENT_LENGTH = 512


def _extract(tracestate: str) -> SpanContext:
    context = propagate.extract({"traceparent": _TRACEPARENT, "tracestate": tracestate})
    return trace.get_current_span(context).get_span_context()


def _extract_traceparent(traceparent: str) -> SpanContext:
    context = propagate.extract({"traceparent": traceparent})
    return trace.get_current_span(context).get_span_context()


def _declared_requirement() -> Requirement:
    config = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    requirements = [Requirement(r) for r in config["project"]["dependencies"]]
    matches = [r for r in requirements if r.name == _PACKAGE]
    assert len(matches) == 1, matches
    return matches[0]


def test_opentelemetry_api_dependency_is_patched() -> None:
    assert Version(version(_PACKAGE)) >= Version(_PATCHED), version(_PACKAGE)


def test_the_declared_range_excludes_the_vulnerable_release() -> None:
    # The lock alone protects only lock-file installs; a pip upgrade keeps an
    # installed 1.45.1 unless the package metadata itself refuses it.
    specifier = _declared_requirement().specifier
    assert not specifier.contains(_LAST_VULNERABLE), specifier
    assert specifier.contains(_PATCHED), specifier


def test_traceparent_at_the_length_cap_is_kept() -> None:
    # Known positive: a value the cap allows still yields its span, so the
    # invalid context below is the cap firing, not a value that never parses.
    padded = _TRACEPARENT + " " * (_MAXIMUM_TRACEPARENT_LENGTH - len(_TRACEPARENT))
    span_context = _extract_traceparent(padded)
    assert span_context.is_valid
    assert span_context.trace_id == _TRACE_ID


def test_oversized_traceparent_is_dropped_before_matching() -> None:
    padded = _TRACEPARENT + " " * (_MAXIMUM_TRACEPARENT_LENGTH + 1 - len(_TRACEPARENT))
    assert not _extract_traceparent(padded).is_valid


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
