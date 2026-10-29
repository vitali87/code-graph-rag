"""One request to `CallResolver.resolve_function_call`, as its stages read it.

The resolver builds one per call it resolves and hands it whole to each
stage, its own and those of the language resolvers it builds (`RustResolver`
in `parsers/rust/resolution.py`), rather than eight loose arguments."""

from typing import NamedTuple

from .. import constants as cs


class CallSite(NamedTuple):
    """One `_resolve_function_call` request, handed whole to its phases."""

    call_name: str
    module_qn: str
    local_var_types: dict[str, str] | None
    class_context: str | None
    caller_qn: str | None
    language: cs.SupportedLanguage | None
    call_point: int | None
    constructing: bool
