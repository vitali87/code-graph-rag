from __future__ import annotations

from enum import StrEnum


class FlowKind(StrEnum):
    ARG = "arg"
    RETURN = "return"
    RESOURCE = "resource"


KEY_VIA = "via"
KEY_KIND = "kind"
# The function (or module) whose body produced a resource-to-resource flow.
# Both endpoints are shared Resource nodes, so without it the edge has no
# owner a re-parse or a project delete can remove it through (issue #2746).
KEY_SCOPE = "scope"

VIA_ARG_FORMAT = "arg:{index}"
VIA_KW_FORMAT = "kw:{name}"
VIA_RETURN = "return"
