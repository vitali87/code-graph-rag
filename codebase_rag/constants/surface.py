# The public surface snapshot (issue #3629): everything users and agents
# reach, recorded so a change to it is a reviewed change, and so the Rust
# rewrite can be held to it.
from __future__ import annotations

from enum import StrEnum

SURFACE_SNAPSHOT_PATH = "docs/reference/public-surface.json"
SURFACE_SNAPSHOT_VERSION = 1
SURFACE_REGENERATE = "uv run python scripts/public_surface.py"
SURFACE_ROOT_COMMAND = "cgr"
# A default worked out when the program runs (a home directory, a factory):
# recording its value would tie the snapshot to the machine that wrote it.
SURFACE_COMPUTED_DEFAULT = "<computed>"
# JSON Schema keys whose values are prose, left out of the MCP input schemas.
SURFACE_PROSE_SCHEMA_KEYS = frozenset({"description", "title"})
# Integer constants whose name holds this are process exit codes.
SURFACE_EXIT_CODE_MARKER = "EXIT"
SURFACE_SDK_PACKAGE = "cgr"
SURFACE_PROTO_TYPE_PREFIX = "TYPE_"
# Dropped from type names so a union reads as it is written.
SURFACE_TYPING_PREFIX = "typing."
SURFACE_NONE_TYPE = "None"
SURFACE_UNION_SEPARATOR = " | "
SURFACE_ARG_SEPARATOR = ", "
SURFACE_JSON_INDENT = 2
SURFACE_NOT_REQUIRED_PREFIX = "NotRequired["
# A command whose context ignores unknown options hands its arguments to a
# click group of its own (cli.py, `_DELEGATED_GROUP_CONTEXT`).
SURFACE_DELEGATE_SETTING = "ignore_unknown_options"
SURFACE_ATTR_IS_FLAG = "is_flag"
SURFACE_ATTR_HIDDEN = "hidden"
# What a click parameter type carries when it limits the accepted values.
SURFACE_ATTR_CHOICES = "choices"
SURFACE_ATTR_MIN = "min"
SURFACE_ATTR_MAX = "max"
SURFACE_ATTR_MIN_OPEN = "min_open"
SURFACE_ATTR_MAX_OPEN = "max_open"
SURFACE_ATTR_CLAMP = "clamp"
# An SDK class's members are recorded when a first-party class defines them;
# what pydantic or object supply is theirs, not cgr's.
SURFACE_FIRST_PARTY_MODULES = ("codebase_rag", "cgr")
SURFACE_PRIVATE_PREFIX = "_"
# pydantic reserves this prefix for its own hooks (`model_post_init`).
SURFACE_PYDANTIC_PREFIX = "model_"
SURFACE_PROPERTY_SIGNATURE = "<property>"


class SurfaceParamKind(StrEnum):
    OPTION = "option"
    ARGUMENT = "argument"


class SurfaceExtra(StrEnum):
    # The optional dependency group a tool needs before the server offers it.
    SEMANTIC = "semantic"
    AST_GREP = "ast-grep"
