"""Trace vocabulary read outside the trace package: the CALLS-edge
properties trace ingestion writes, and the notes crash correlation adds to a
traceback. The rest lives in trace/constants.py."""

from enum import StrEnum

# Properties stored on CALLS edges by trace ingestion. A `dynamic` edge that
# `static_missed` is a relationship the static passes could not see (dynamic
# dispatch, reflection, registries); one without the flag confirms a static
# edge at runtime.
TRACE_PROP_DYNAMIC = "dynamic"
TRACE_PROP_CALL_COUNT = "dynamic_call_count"


class TraceUnresolvedReason(StrEnum):
    """Why a traced frame could not be mapped to a graph node."""

    OUTSIDE_REPO = "outside_repo"
    SYNTHETIC = "synthetic"
    UNKNOWN_PATH = "unknown_path"
    NO_MATCH = "no_match"
    AMBIGUOUS = "ambiguous"


# Said instead of handing back an empty result that reads as "the graph does
# not know this code" when only the paths failed to line up (issue #2587).
TRACEBACK_NOTE_NOTHING_RESOLVED = (
    "0 of {total} frames resolved: {outside} lie outside the indexed checkout "
    "{root}, and no checkout root they share matches a file the graph indexes. "
    "If the traceback comes from another machine, a container or a CI runner, "
    "pass {param} to map its checkout root onto the repository, "
    'e.g. {{"/app": "."}}.'
)

# An ExceptionGroup's report covers the group and, under `members`, each
# sub-exception it printed (issue #3235).
TRACEBACK_NOTE_EXCEPTION_GROUP = (
    "{type} with {listed} sub-exception{plural}: frames is the stack that "
    "raised the group, and each sub-exception is analysed on its own under "
    "members."
)
TRACEBACK_NOTE_MEMBERS_OMITTED = (
    " The traceback itself left out {omitted} more, past its "
    "max_group_width or max_group_depth."
)
