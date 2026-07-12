from __future__ import annotations

from enum import StrEnum


class FlowKind(StrEnum):
    ARG = "arg"
    RETURN = "return"
    RESOURCE = "resource"


KEY_VIA = "via"
KEY_KIND = "kind"

VIA_ARG_FORMAT = "arg:{index}"
VIA_KW_FORMAT = "kw:{name}"
VIA_RETURN = "return"
