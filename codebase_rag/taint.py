from __future__ import annotations

# The narrowest verbatim run (in normalized characters) that counts as
# repository content appearing in an outbound query. Below this, matches are
# dominated by identifiers and stock phrases that legitimately appear both in
# the repository and in ordinary search queries.
TAINT_SPAN_CHARS = 24


def _normalize_whitespace(text: str) -> str:
    """Collapse runs of whitespace so matching is whitespace-insensitive:
    reflowing a span across lines must not defeat the check."""
    return " ".join(text.split())


class ReadContentRecord:
    """Repository content handed to the model this session (issue #1128).

    Tools that return raw repository bytes to the agent (file reads, code
    snippets, shell output) record what they returned; outbound-egress tools
    (web search) refuse any query carrying a verbatim span of it. This is
    deliberately a cheap string check, not semantic taint tracking: web
    content shapes the model's reasoning, not just tool arguments, so no
    derived-input predicate is sound. The enforceable boundary is the
    research sub-agent, which never sees repository content at all; this
    check closes the remaining direct channel, the orchestrator pasting
    repository bytes into a research query. Paraphrased content still
    passes; that residual is accepted in the issue's design discussion.

    Two match rules, split by what the recording is. Long recordings match on
    any verbatim span of TAINT_SPAN_CHARS, since a fragment of a source file
    is still that file's content. Short standalone recordings (a file or a
    command output that is entirely a token or key) match only in full: they
    are complete values, and matching them as substrings would refuse
    ordinary queries that happen to share a few characters with source.

    Egress is gated at every hop that can carry a query off the machine: the
    research tool refuses before the sub-agent's hosted provider is called,
    and web_search refuses again before the search backend. The provider is
    itself a network egress point, so gating only at the backend would leak
    to it first.
    """

    __slots__ = ("_contents", "_short_values")

    def __init__(self) -> None:
        """Start an empty record for this session."""
        # Recorded content is already in the LLM context for the rest of the
        # session, so holding it here does not change the memory order.
        self._contents: list[str] = []
        # Recordings shorter than the span threshold, kept separately: the
        # windowed check can never match them, but a whole file (or command
        # output) that IS a short secret is exactly what must not leak.
        self._short_values: list[str] = []

    def record(self, content: str) -> None:
        """Record repository content returned to the model this session."""
        if not (normalized := _normalize_whitespace(content)):
            return
        if len(normalized) < TAINT_SPAN_CHARS:
            # Matched whole, never as a substring: a short recording is a
            # complete value (a token, a key), so requiring the full value in
            # the query keeps this free of the false positives that a
            # sub-threshold substring rule would cause on ordinary source.
            self._short_values.append(normalized)
        else:
            self._contents.append(normalized)

    def taints(self, query: str) -> bool:
        """Report whether the query carries recorded repository content, and
        so must not leave the machine.

        Long recordings match on any verbatim span; short standalone
        recordings match only in full.
        """
        normalized = _normalize_whitespace(query)
        if any(value in normalized for value in self._short_values):
            return True
        if len(normalized) < TAINT_SPAN_CHARS:
            return False
        windows = [
            normalized[i : i + TAINT_SPAN_CHARS]
            for i in range(len(normalized) - TAINT_SPAN_CHARS + 1)
        ]
        return any(
            window in content for content in self._contents for window in windows
        )
