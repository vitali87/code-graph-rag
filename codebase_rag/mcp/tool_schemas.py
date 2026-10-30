"""The input schema of every MCP tool, by name: its properties, which of
them are required, and whether its result is JSON. The registry (tools.py)
decides which tools a server offers and pairs each with its handler.
"""

from typing import NamedTuple

from codebase_rag import constants as cs
from codebase_rag.tools import tool_descriptions as td
from codebase_rag.types_defs import (
    MCPInputSchema,
    MCPInputSchemaProperties,
    MCPInputSchemaProperty,
)

from . import constants as mcs


class ToolSchema(NamedTuple):
    properties: MCPInputSchemaProperties
    required: tuple[str, ...] = ()
    returns_json: bool = True


def _param(
    kind: mcs.MCPSchemaType,
    description: str,
    *,
    default: str | int | float | bool | None = None,
    minimum: int | None = None,
    items: dict[str, str] | None = None,
    value_type: mcs.MCPSchemaType | None = None,
) -> MCPInputSchemaProperty:
    """One input property of an MCP tool's JSON schema; `value_type` is the
    type of an object's values when it is used as a map."""
    prop = MCPInputSchemaProperty(type=kind, description=description)
    if default is not None:
        prop["default"] = default
    if minimum is not None:
        prop["minimum"] = minimum
    if items is not None:
        prop["items"] = items
    if value_type is not None:
        prop["additionalProperties"] = {mcs.MCPSchemaField.TYPE: value_type}
    return prop


def _graph_schema(
    params: dict[str, str],
    required: tuple[str, ...],
    integer_params: frozenset[str] = frozenset(),
    boolean_params: frozenset[str] = frozenset(),
) -> ToolSchema:
    """A deterministic graph query's schema (issue #1523): string parameters
    unless named otherwise, and the `project` every graph read is scoped by."""
    kinds = dict.fromkeys(integer_params, mcs.MCPSchemaType.INTEGER)
    kinds |= dict.fromkeys(boolean_params, mcs.MCPSchemaType.BOOLEAN)
    properties = {
        key: _param(kinds.get(key, mcs.MCPSchemaType.STRING), description)
        for key, description in params.items()
    }
    properties[cs.MCPParamName.PROJECT] = _param(
        mcs.MCPSchemaType.STRING, td.MCP_PARAM_PROJECT
    )
    return ToolSchema(properties, required)


# Both traceback tools read the same text and path map.
_TRACEBACK_SCHEMA = ToolSchema(
    {
        cs.MCPParamName.TRACEBACK_TEXT: _param(
            mcs.MCPSchemaType.STRING, td.MCP_PARAM_TRACEBACK_TEXT
        ),
        cs.MCPParamName.PATH_PREFIX_MAP: _param(
            mcs.MCPSchemaType.OBJECT,
            td.MCP_PARAM_PATH_PREFIX_MAP,
            value_type=mcs.MCPSchemaType.STRING,
        ),
    },
    (cs.MCPParamName.TRACEBACK_TEXT,),
)

_SCHEMAS: dict[cs.MCPToolName, ToolSchema] = {
    cs.MCPToolName.LIST_PROJECTS: ToolSchema({}),
    cs.MCPToolName.DELETE_PROJECT: ToolSchema(
        {
            cs.MCPParamName.PROJECT_NAME: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_PROJECT_NAME
            )
        },
        (cs.MCPParamName.PROJECT_NAME,),
    ),
    cs.MCPToolName.WIPE_DATABASE: ToolSchema(
        {
            cs.MCPParamName.CONFIRM: _param(
                mcs.MCPSchemaType.BOOLEAN, td.MCP_PARAM_CONFIRM
            )
        },
        (cs.MCPParamName.CONFIRM,),
        returns_json=False,
    ),
    cs.MCPToolName.INDEX_REPOSITORY: ToolSchema({}, returns_json=False),
    cs.MCPToolName.UPDATE_REPOSITORY: ToolSchema({}, returns_json=False),
    cs.MCPToolName.REINGEST: ToolSchema(
        {
            cs.MCPParamName.PATHS: _param(
                mcs.MCPSchemaType.ARRAY,
                td.MCP_PARAM_REINGEST_PATHS,
                items={mcs.MCPSchemaField.TYPE: mcs.MCPSchemaType.STRING},
            ),
            cs.MCPParamName.DELETED: _param(
                mcs.MCPSchemaType.ARRAY,
                td.MCP_PARAM_REINGEST_DELETED,
                items={mcs.MCPSchemaField.TYPE: mcs.MCPSchemaType.STRING},
            ),
        },
        (cs.MCPParamName.PATHS,),
    ),
    cs.MCPToolName.RESOLVE: _graph_schema(
        {cs.MCPParamName.TARGET: td.MCP_PARAM_TARGET}, (cs.MCPParamName.TARGET,)
    ),
    cs.MCPToolName.DEFINITION: _graph_schema(
        {cs.MCPParamName.QUALIFIED_NAME: td.MCP_PARAM_QUALIFIED_NAME},
        (cs.MCPParamName.QUALIFIED_NAME,),
    ),
    cs.MCPToolName.CALLERS: _graph_schema(
        {
            cs.MCPParamName.QUALIFIED_NAME: td.MCP_PARAM_QUALIFIED_NAME,
            cs.MCPParamName.DEPTH: td.MCP_PARAM_DEPTH,
        },
        (cs.MCPParamName.QUALIFIED_NAME,),
        integer_params=frozenset({cs.MCPParamName.DEPTH}),
    ),
    cs.MCPToolName.CALLEES: _graph_schema(
        {
            cs.MCPParamName.QUALIFIED_NAME: td.MCP_PARAM_QUALIFIED_NAME,
            cs.MCPParamName.DEPTH: td.MCP_PARAM_DEPTH,
        },
        (cs.MCPParamName.QUALIFIED_NAME,),
        integer_params=frozenset({cs.MCPParamName.DEPTH}),
    ),
    cs.MCPToolName.IMPLEMENTORS: _graph_schema(
        {
            cs.MCPParamName.QUALIFIED_NAME: td.MCP_PARAM_QUALIFIED_NAME,
            cs.MCPParamName.DEPTH: td.MCP_PARAM_DEPTH,
        },
        (cs.MCPParamName.QUALIFIED_NAME,),
        integer_params=frozenset({cs.MCPParamName.DEPTH}),
    ),
    cs.MCPToolName.OVERRIDES: _graph_schema(
        {
            cs.MCPParamName.QUALIFIED_NAME: td.MCP_PARAM_QUALIFIED_NAME,
            cs.MCPParamName.DEPTH: td.MCP_PARAM_DEPTH,
        },
        (cs.MCPParamName.QUALIFIED_NAME,),
        integer_params=frozenset({cs.MCPParamName.DEPTH}),
    ),
    cs.MCPToolName.ANNOTATE: _graph_schema(
        {
            cs.MCPParamName.TARGET: td.MCP_PARAM_TARGET,
            cs.MCPParamName.BODY: td.MCP_PARAM_GLOSS_BODY,
            cs.MCPParamName.KIND: td.MCP_PARAM_GLOSS_KIND,
            cs.MCPParamName.MENTIONS: td.MCP_PARAM_GLOSS_MENTIONS,
            cs.MCPParamName.AUTHOR: td.MCP_PARAM_GLOSS_AUTHOR,
        },
        (cs.MCPParamName.TARGET, cs.MCPParamName.BODY, cs.MCPParamName.KIND),
    ),
    cs.MCPToolName.GLOSSES: _graph_schema(
        {cs.MCPParamName.TARGET: td.MCP_PARAM_TARGET}, (cs.MCPParamName.TARGET,)
    ),
    cs.MCPToolName.IMPORTERS: _graph_schema(
        {
            cs.MCPParamName.MODULE_QN: td.MCP_PARAM_MODULE_QN,
            cs.MCPParamName.THROUGH_REEXPORTS: td.MCP_PARAM_THROUGH_REEXPORTS,
        },
        (cs.MCPParamName.MODULE_QN,),
        boolean_params=frozenset({cs.MCPParamName.THROUGH_REEXPORTS}),
    ),
    cs.MCPToolName.TESTS_REACHING: _graph_schema(
        {cs.MCPParamName.QUALIFIED_NAME: td.MCP_PARAM_QUALIFIED_NAME},
        (cs.MCPParamName.QUALIFIED_NAME,),
    ),
    cs.MCPToolName.ENDPOINTS: _graph_schema({}, ()),
    cs.MCPToolName.ENDPOINT_CALLERS: _graph_schema(
        {cs.MCPParamName.TARGET: td.MCP_PARAM_ENDPOINT_TARGET},
        (cs.MCPParamName.TARGET,),
    ),
    cs.MCPToolName.REMOTE_DEPENDENCIES: _graph_schema({}, ()),
    cs.MCPToolName.RENAME: ToolSchema(
        {
            cs.MCPParamName.QUALIFIED_NAME: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_QUALIFIED_NAME
            ),
            cs.MCPParamName.NEW_NAME: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_NEW_NAME
            ),
            cs.MCPParamName.ALLOW_HEURISTIC: _param(
                mcs.MCPSchemaType.BOOLEAN, td.MCP_PARAM_ALLOW_HEURISTIC
            ),
            cs.MCPParamName.DRY_RUN: _param(
                mcs.MCPSchemaType.BOOLEAN, td.MCP_PARAM_RENAME_DRY_RUN
            ),
            cs.MCPParamName.PROJECT: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_PROJECT
            ),
        },
        (cs.MCPParamName.QUALIFIED_NAME, cs.MCPParamName.NEW_NAME),
    ),
    cs.MCPToolName.CONTEXT: ToolSchema(
        {
            cs.MCPParamName.TARGET: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_CONTEXT_TARGET
            ),
            cs.MCPParamName.BUDGET_TOKENS: _param(
                mcs.MCPSchemaType.INTEGER, td.MCP_PARAM_BUDGET_TOKENS
            ),
            cs.MCPParamName.PROJECT: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_PROJECT
            ),
        },
        (cs.MCPParamName.TARGET,),
    ),
    cs.MCPToolName.QUERY_CODE_GRAPH: ToolSchema(
        {
            cs.MCPParamName.NATURAL_LANGUAGE_QUERY: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_NATURAL_LANGUAGE_QUERY
            ),
            cs.MCPParamName.PROJECT: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_PROJECT
            ),
        },
        (cs.MCPParamName.NATURAL_LANGUAGE_QUERY,),
    ),
    cs.MCPToolName.GET_CODE_SNIPPET: ToolSchema(
        {
            cs.MCPParamName.QUALIFIED_NAME: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_QUALIFIED_NAME
            )
        },
        (cs.MCPParamName.QUALIFIED_NAME,),
    ),
    cs.MCPToolName.SURGICAL_REPLACE_CODE: ToolSchema(
        {
            cs.MCPParamName.FILE_PATH: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_FILE_PATH
            ),
            cs.MCPParamName.TARGET_CODE: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_TARGET_CODE
            ),
            cs.MCPParamName.REPLACEMENT_CODE: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_REPLACEMENT_CODE
            ),
        },
        (
            cs.MCPParamName.FILE_PATH,
            cs.MCPParamName.TARGET_CODE,
            cs.MCPParamName.REPLACEMENT_CODE,
        ),
        returns_json=False,
    ),
    cs.MCPToolName.READ_FILE: ToolSchema(
        {
            cs.MCPParamName.FILE_PATH: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_FILE_PATH
            ),
            cs.MCPParamName.OFFSET: _param(
                mcs.MCPSchemaType.INTEGER,
                td.MCP_PARAM_OFFSET,
                minimum=mcs.MCP_READ_OFFSET_MINIMUM,
            ),
            cs.MCPParamName.LIMIT: _param(
                mcs.MCPSchemaType.INTEGER,
                td.MCP_PARAM_LIMIT,
                minimum=mcs.MCP_READ_LIMIT_MINIMUM,
            ),
        },
        (cs.MCPParamName.FILE_PATH,),
        returns_json=False,
    ),
    cs.MCPToolName.WRITE_FILE: ToolSchema(
        {
            cs.MCPParamName.FILE_PATH: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_FILE_PATH
            ),
            cs.MCPParamName.CONTENT: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_CONTENT
            ),
        },
        (cs.MCPParamName.FILE_PATH, cs.MCPParamName.CONTENT),
        returns_json=False,
    ),
    cs.MCPToolName.LIST_DIRECTORY: ToolSchema(
        {
            cs.MCPParamName.DIRECTORY_PATH: _param(
                mcs.MCPSchemaType.STRING,
                td.MCP_PARAM_DIRECTORY_PATH,
                default=cs.MCP_DEFAULT_DIRECTORY,
            )
        },
        returns_json=False,
    ),
    cs.MCPToolName.SEMANTIC_SEARCH: ToolSchema(
        {
            cs.MCPParamName.NATURAL_LANGUAGE_QUERY: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_NATURAL_LANGUAGE_QUERY
            ),
            cs.MCPParamName.TOP_K: _param(
                mcs.MCPSchemaType.INTEGER, td.MCP_PARAM_TOP_K, default=5
            ),
            cs.MCPParamName.PROJECT: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_PROJECT
            ),
        },
        (cs.MCPParamName.NATURAL_LANGUAGE_QUERY,),
        returns_json=False,
    ),
    cs.MCPToolName.STRUCTURAL_SEARCH: ToolSchema(
        {
            cs.MCPParamName.PATTERN: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_PATTERN
            ),
            cs.MCPParamName.LANGUAGE: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_LANGUAGE
            ),
        },
        (cs.MCPParamName.PATTERN,),
        returns_json=False,
    ),
    cs.MCPToolName.STRUCTURAL_REPLACE: ToolSchema(
        {
            cs.MCPParamName.PATTERN: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_PATTERN
            ),
            cs.MCPParamName.REWRITE: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_REWRITE
            ),
            cs.MCPParamName.LANGUAGE: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_LANGUAGE
            ),
            cs.MCPParamName.DRY_RUN: _param(
                mcs.MCPSchemaType.BOOLEAN, td.MCP_PARAM_DRY_RUN, default=True
            ),
        },
        (cs.MCPParamName.PATTERN, cs.MCPParamName.REWRITE),
        returns_json=False,
    ),
    cs.MCPToolName.FIND_DUPLICATE_CODE: ToolSchema(
        {
            cs.MCPParamName.PROJECT: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_PROJECT
            ),
            cs.MCPParamName.THRESHOLD: _param(
                mcs.MCPSchemaType.NUMBER,
                td.MCP_PARAM_THRESHOLD,
                default=cs.DUPLICATES_DEFAULT_THRESHOLD,
            ),
            cs.MCPParamName.MIN_SIZE: _param(
                mcs.MCPSchemaType.INTEGER,
                td.MCP_PARAM_MIN_SIZE,
                default=cs.DUPLICATES_DEFAULT_MIN_NODES,
            ),
            cs.MCPParamName.LIMIT: _param(
                mcs.MCPSchemaType.INTEGER,
                td.MCP_PARAM_DUPLICATES_LIMIT,
                default=cs.DUPLICATES_DEFAULT_GROUP_LIMIT,
            ),
        },
        returns_json=False,
    ),
    cs.MCPToolName.GET_FUNCTION_SOURCE: ToolSchema(
        {
            cs.MCPParamName.NODE_ID: _param(
                mcs.MCPSchemaType.INTEGER, td.MCP_PARAM_NODE_ID
            )
        },
        (cs.MCPParamName.NODE_ID,),
        returns_json=False,
    ),
    cs.MCPToolName.ASK_AGENT: ToolSchema(
        {
            cs.MCPParamName.QUESTION: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_QUESTION
            )
        },
        (cs.MCPParamName.QUESTION,),
    ),
    cs.MCPToolName.FLOW_VERDICT: ToolSchema(
        {
            cs.MCPParamName.SOURCE_QN: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_SOURCE_QN
            ),
            cs.MCPParamName.SINK_QN: _param(
                mcs.MCPSchemaType.STRING, td.MCP_PARAM_SINK_QN
            ),
        },
        (cs.MCPParamName.SOURCE_QN, cs.MCPParamName.SINK_QN),
    ),
    cs.MCPToolName.EXPLAIN_TRACEBACK: _TRACEBACK_SCHEMA,
    cs.MCPToolName.RANK_ROOT_CAUSES: _TRACEBACK_SCHEMA,
}


def input_schema(name: cs.MCPToolName) -> MCPInputSchema:
    """The object schema `name`'s arguments are checked against."""
    schema = _SCHEMAS[name]
    return MCPInputSchema(
        type=mcs.MCPSchemaType.OBJECT,
        properties=dict(schema.properties),
        required=list(schema.required),
    )


def returns_json(name: cs.MCPToolName) -> bool:
    """Whether `name`'s handler answers with JSON rather than text."""
    return _SCHEMAS[name].returns_json
