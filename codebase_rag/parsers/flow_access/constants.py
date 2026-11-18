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
# The functions, outside the scope's own body, whose summaries a composed
# resource flow was derived through: a re-parse of the scope alone cannot
# derive it again while one of them is not walked (review of PR #2759).
KEY_THROUGH = "through"

VIA_ARG_FORMAT = "arg:{index}"
VIA_KW_FORMAT = "kw:{name}"
VIA_RETURN = "return"
