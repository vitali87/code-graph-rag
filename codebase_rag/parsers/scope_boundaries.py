"""The nested scopes a scan of one scope's own syntax stops at.

A walk over a scope for the functions it stores, returns or holds in a
literal must not descend into a nested function or class: the returns and
local bindings there belong to that scope, which gets a pass of its own.
The call pass, its reference walkers and the languages' call collaborators
all read the boundaries here; it imports no language module, so a
language's collaborator can import it too."""

from .. import constants as cs
from ..language_spec import LanguageSpec

# Python's nested scopes. A decorated definition wraps a function or class
# and is no function or class node type of its own in Python's spec.
PY_SCOPE_BOUNDARY_TYPES = frozenset(
    {
        cs.TS_PY_FUNCTION_DEFINITION,
        cs.TS_PY_CLASS_DEFINITION,
        cs.TS_PY_DECORATED_DEFINITION,
    }
)


def flow_scope_boundaries(lang_config: LanguageSpec) -> frozenset[str]:
    """The node types of a nested function or class scope a scan of a scope's
    own syntax stops at."""
    # The Python set is unioned in for every language so Python keeps its
    # exact prior behaviour (its decorated_definition wrapper is not a
    # config type).
    return (
        PY_SCOPE_BOUNDARY_TYPES
        | frozenset(lang_config.function_node_types)
        | frozenset(lang_config.class_node_types)
    )
