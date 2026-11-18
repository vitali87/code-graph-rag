#!/usr/bin/env python3
"""Write, or check, the public surface snapshot (issue #3629).

Everything users and agents reach, as data: every command and its options
(with the values they accept), the exit codes, every MCP tool and its input
schema, the settings and the environment variables they are read from, the
graph labels and relationships, the JSON export's records, the protobuf
index's messages and enum numbers, and the Python SDK with its classes'
public methods. Prose (help text, descriptions) is left out: it
may change freely, the shape may not. The Rust rewrite is held to this file,
and `test_public_surface.py` fails when the code drifts from it.

    uv run python scripts/public_surface.py           # rewrite the snapshot
    uv run python scripts/public_surface.py --check   # exit 1 if it is stale
"""

from __future__ import annotations

import argparse
import dataclasses
import inspect
import json
import sys
import tempfile
import types
import typing
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from contextlib import contextmanager
from enum import Enum
from pathlib import Path, PurePath
from unittest.mock import MagicMock, patch

import click
import typer
import typer.core
from google.protobuf.descriptor import Descriptor, EnumDescriptor, FieldDescriptor
from pydantic import AliasChoices
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined, PydanticUndefinedType

import cgr
from codebase_rag import cli as cli_module
from codebase_rag import constants as cs
from codebase_rag import exceptions as ex
from codebase_rag import types_defs as td
from codebase_rag.cli import app
from codebase_rag.cli_help import CLICommandName
from codebase_rag.config import AppConfig, settings
from codebase_rag.mcp import tools as mcp_tools
from codebase_rag.tools import semantic_search
from codec import schema_pb2

PROJECT_ROOT = Path(__file__).parent.parent
SNAPSHOT = PROJECT_ROOT / cs.SURFACE_SNAPSHOT_PATH

_PROTO_TYPES = {
    getattr(FieldDescriptor, name): name.removeprefix(cs.SURFACE_PROTO_TYPE_PREFIX)
    for name in dir(FieldDescriptor)
    if name.startswith(cs.SURFACE_PROTO_TYPE_PREFIX)
}
_EXPORT_RECORDS = (td.GraphData, td.GraphMetadata, td.NodeData, td.RelationshipData)

# What an option or setting default can hold.
type DefaultValue = (
    str
    | int
    | float
    | bool
    | Enum
    | PurePath
    | PydanticUndefinedType
    | None
    | Callable[[], DefaultValue]
    | list[DefaultValue]
    | tuple[DefaultValue, ...]
    | set[DefaultValue]
    | frozenset[DefaultValue]
)
type Annotation = type | str | typing.ForwardRef | types.UnionType | types.GenericAlias
type CliParam = (
    typer.core.TyperOption | typer.core.TyperArgument | click.Option | click.Argument
)

# The click group each pass-through command runs (`_run_delegated_group`).
_DELEGATED_GROUPS: dict[str, click.Group] = {
    CLICommandName.LANGUAGE: cli_module.language_cli,
    CLICommandName.DAEMON: cli_module.daemon_cli,
    CLICommandName.WORKSPACE: cli_module.workspace_cli,
    CLICommandName.TRACE: cli_module.trace_cli,
    CLICommandName.EDITS: cli_module.edits_cli,
    CLICommandName.GRAPH: cli_module.graph_cli,
    CLICommandName.PORT: cli_module.port_cli,
}


def _json_value(value: DefaultValue) -> td.JsonValue:
    """A default as JSON, the same on every machine."""
    if value is None or value is PydanticUndefined or callable(value):
        return None if value is None else cs.SURFACE_COMPUTED_DEFAULT
    if isinstance(value, Enum):
        # click 8.2+ marks "no default" with an enum sentinel whose value is a
        # bare object(); its repr is a memory address, new on every run.
        if type(value.value) is object:
            return None
        return _json_value(value.value)
    if isinstance(value, PurePath):
        return value.as_posix()
    if isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple | set | frozenset):
        items = [_json_value(item) for item in value]
        return sorted(items, key=str) if isinstance(value, set | frozenset) else items
    return str(value)


def _type_name(annotation: Annotation | None) -> str:
    """A type as written, by bare class names: `str()` qualifies a class by
    its module, which moves between Python versions (`pathlib.Path` is
    `pathlib._local.Path` on 3.13)."""
    if annotation is None or annotation is type(None):
        return cs.SURFACE_NONE_TYPE
    if isinstance(annotation, typing.ForwardRef):
        return annotation.__forward_arg__
    if isinstance(annotation, str):
        return annotation
    args = typing.get_args(annotation)
    origin = typing.get_origin(annotation)
    if not args:
        if isinstance(annotation, type):
            return annotation.__name__
        return str(annotation).replace(cs.SURFACE_TYPING_PREFIX, "")
    if origin is types.UnionType or origin is typing.Union:
        return cs.SURFACE_UNION_SEPARATOR.join(_type_name(arg) for arg in args)
    if origin is typing.Literal:
        inner = cs.SURFACE_ARG_SEPARATOR.join(repr(arg) for arg in args)
    else:
        inner = cs.SURFACE_ARG_SEPARATOR.join(_type_name(arg) for arg in args)
    return f"{_type_name(origin)}[{inner}]"


def _envvars(envvar: str | Sequence[str] | None) -> list[str]:
    if envvar is None:
        return []
    return [envvar] if isinstance(envvar, str) else list(envvar)


def _choices(param: CliParam) -> list[str] | None:
    # Read by attribute: typer bundles its own click, so its Choice is not
    # click.Choice.
    choices = getattr(param.type, cs.SURFACE_ATTR_CHOICES, None)
    if choices is None:
        return None
    return [str(_json_value(choice)) for choice in choices]


def _bounds(param: CliParam) -> td.SurfaceBounds | None:
    kind = param.type
    if not hasattr(kind, cs.SURFACE_ATTR_MIN_OPEN):
        return None
    return td.SurfaceBounds(
        min=_json_value(getattr(kind, cs.SURFACE_ATTR_MIN)),
        max=_json_value(getattr(kind, cs.SURFACE_ATTR_MAX)),
        min_open=bool(getattr(kind, cs.SURFACE_ATTR_MIN_OPEN)),
        max_open=bool(getattr(kind, cs.SURFACE_ATTR_MAX_OPEN)),
        clamp=bool(getattr(kind, cs.SURFACE_ATTR_CLAMP, False)),
    )


def _path_rule(param: CliParam) -> td.SurfacePathRule | None:
    """What a path option checks before the command runs (`exists`, file
    or directory): the type's name alone says only that it is a path."""
    kind = param.type
    if not hasattr(kind, cs.SURFACE_ATTR_DIR_OKAY):
        return None
    return td.SurfacePathRule(
        exists=bool(getattr(kind, cs.SURFACE_ATTR_EXISTS, False)),
        file_okay=bool(getattr(kind, cs.SURFACE_ATTR_FILE_OKAY, True)),
        dir_okay=bool(getattr(kind, cs.SURFACE_ATTR_DIR_OKAY, True)),
        readable=bool(getattr(kind, cs.SURFACE_ATTR_READABLE, True)),
        writable=bool(getattr(kind, cs.SURFACE_ATTR_WRITABLE, False)),
        resolve_path=bool(getattr(kind, cs.SURFACE_ATTR_RESOLVE_PATH, False)),
        allow_dash=bool(getattr(kind, cs.SURFACE_ATTR_ALLOW_DASH, False)),
    )


def _param(param: CliParam) -> td.SurfaceParam:
    kind = (
        cs.SurfaceParamKind.OPTION
        if param.param_type_name == cs.SurfaceParamKind.OPTION
        else cs.SurfaceParamKind.ARGUMENT
    )
    return td.SurfaceParam(
        name=param.name or "",
        kind=kind,
        flags=[*param.opts, *param.secondary_opts],
        type=param.type.name,
        nargs=param.nargs,
        choices=_choices(param),
        bounds=_bounds(param),
        path=_path_rule(param),
        required=param.required,
        multiple=param.multiple,
        is_flag=bool(getattr(param, cs.SURFACE_ATTR_IS_FLAG, False)),
        hidden=bool(getattr(param, cs.SURFACE_ATTR_HIDDEN, False)),
        default=_json_value(param.default),
        envvar=_envvars(param.envvar),
    )


def _delegated(
    command: typer.core.TyperCommand | typer.core.TyperGroup, path: list[str]
) -> click.Group | None:
    """The click group a pass-through command hands its arguments to.

    A command that takes unknown options and extra arguments runs a group of
    its own (`_run_delegated_group` in cli.py); its subcommands are only
    reachable through that group, so an unmapped one fails loudly rather than
    dropping out of the snapshot.
    """
    if not command.context_settings.get(cs.SURFACE_DELEGATE_SETTING):
        return None
    group = _DELEGATED_GROUPS.get(path[-1])
    if group is None:
        raise LookupError(ex.SURFACE_UNMAPPED_DELEGATE.format(command=" ".join(path)))
    return group


def _surface_command(
    path: list[str], hidden: bool, params: list[CliParam]
) -> td.SurfaceCommand:
    return td.SurfaceCommand(
        command=" ".join(path),
        hidden=hidden,
        params=[_param(param) for param in params],
    )


def _walk_click(
    command: click.Command, path: list[str], ctx: click.Context
) -> Iterator[td.SurfaceCommand]:
    # A delegated group is the real click, not typer's bundled copy.
    params: list[CliParam] = [
        param
        for param in command.params
        if isinstance(param, click.Option | click.Argument)
    ]
    if not isinstance(command, click.Group):
        yield _surface_command(path, command.hidden, params)
        return
    # A group's own options (`cgr graph --project`) come before its
    # subcommand, so they are recorded on the group itself.
    if params:
        yield _surface_command(path, command.hidden, params)
    for name in command.list_commands(ctx):
        sub = command.get_command(ctx, name)
        if sub is not None:
            child = click.Context(sub, info_name=name, parent=ctx)
            yield from _walk_click(sub, [*path, name], child)


def _walk_typer(
    command: typer.core.TyperCommand | typer.core.TyperGroup,
    path: list[str],
    ctx: typer.Context,
) -> Iterator[td.SurfaceCommand]:
    if (group := _delegated(command, path)) is not None:
        yield from _walk_click(group, path, click.Context(group, info_name=path[-1]))
        return
    params: list[CliParam] = [
        param
        for param in command.params
        if isinstance(param, typer.core.TyperOption | typer.core.TyperArgument)
    ]
    if not isinstance(command, typer.core.TyperGroup):
        yield _surface_command(path, command.hidden, params)
        return
    # The root group owns the global options (`--version`, `--quiet`).
    if params:
        yield _surface_command(path, command.hidden, params)
    for name in command.list_commands(ctx):
        sub = command.get_command(ctx, name)
        if isinstance(sub, typer.core.TyperCommand | typer.core.TyperGroup):
            child = typer.Context(sub, info_name=str(name), parent=ctx)
            yield from _walk_typer(sub, [*path, str(name)], child)


def cli_commands() -> list[td.SurfaceCommand]:
    root = typer.main.get_command(app)
    if not isinstance(root, typer.core.TyperGroup):
        return []
    ctx = typer.Context(root, info_name=cs.SURFACE_ROOT_COMMAND)
    commands = list(_walk_typer(root, [cs.SURFACE_ROOT_COMMAND], ctx))
    return sorted(commands, key=lambda command: command["command"])


def exit_codes() -> dict[str, int]:
    return {
        name: value
        for name, value in sorted(vars(cs).items())
        if cs.SURFACE_EXIT_CODE_MARKER in name
        and isinstance(value, int)
        and not isinstance(value, bool)
    }


def _without_prose(schema: td.JsonValue) -> td.JsonValue:
    if isinstance(schema, dict):
        return {
            key: _without_prose(value)
            for key, value in sorted(schema.items())
            if key not in cs.SURFACE_PROSE_SCHEMA_KEYS
        }
    if isinstance(schema, list):
        return [_without_prose(item) for item in schema]
    return schema


@contextmanager
def _extras(semantic: bool, ast_grep: bool) -> Generator[None]:
    # Which optional extras are installed decides which tools the server
    # offers (`semantic` for semantic_search, `ast-grep` for the structural
    # tools); every answer is recorded, on any machine.
    with (
        patch.object(mcp_tools, "has_semantic_dependencies", lambda: semantic),
        patch.object(mcp_tools, "has_ast_grep", lambda: ast_grep),
        patch.object(semantic_search, "create_semantic_search_tool", MagicMock()),
        patch.object(settings, "MCP_READ_ONLY", False),
    ):
        yield


def _served_tools(*, semantic: bool, ast_grep: bool) -> dict[str, td.JsonValue]:
    with _extras(semantic, ast_grep), tempfile.TemporaryDirectory() as root:
        registry = mcp_tools.MCPToolsRegistry(
            project_root=root, ingestor=MagicMock(), cypher_gen=MagicMock()
        )
        return {
            # A JSON round trip types the schema as the JSON it is.
            str(schema.name): json.loads(json.dumps(schema.inputSchema))
            for schema in registry.get_tool_schemas()
        }


def _needed_extra(
    name: str, without_semantic: set[str], without_ast_grep: set[str]
) -> str | None:
    if name not in without_semantic:
        return cs.SurfaceExtra.SEMANTIC
    if name not in without_ast_grep:
        return cs.SurfaceExtra.AST_GREP
    return None


def mcp_tool_list() -> list[td.SurfaceMcpTool]:
    every = _served_tools(semantic=True, ast_grep=True)
    without_semantic = set(_served_tools(semantic=False, ast_grep=True))
    without_ast_grep = set(_served_tools(semantic=True, ast_grep=False))
    return [
        td.SurfaceMcpTool(
            name=name,
            input_schema=_without_prose(schema),
            annotations={
                key: value
                for key, value in mcp_tools.tool_annotations(name)
                .model_dump(exclude_none=True)
                .items()
                if isinstance(value, bool)
            },
            served_read_only=name in cs.MCP_READ_ONLY_TOOLS,
            needs_extra=_needed_extra(name, without_semantic, without_ast_grep),
        )
        for name, schema in sorted(every.items())
    ]


def _env_names(name: str, field: FieldInfo) -> list[str]:
    """The environment variables a setting is read from: its alias when it
    has one (`CGR_CAPTURE_LOCAL_DEFINITIONS`), else its own name."""
    alias = field.validation_alias
    if isinstance(alias, str):
        return [alias]
    if isinstance(alias, AliasChoices):
        return [choice for choice in alias.choices if isinstance(choice, str)]
    return [name]


def _constraints(field: FieldInfo) -> dict[str, td.JsonValue]:
    """A setting's declared limits (`ge=1`): tightening one rejects values
    that were valid, though the type and default stay the same."""
    found: dict[str, td.JsonValue] = {}
    for item in field.metadata:
        if dataclasses.is_dataclass(item) and not isinstance(item, type):
            for key, value in dataclasses.asdict(item).items():
                found[key] = _json_value(value)
        else:
            found[type(item).__name__] = str(item)
    return dict(sorted(found.items()))


def setting_list() -> list[td.SurfaceSetting]:
    # A security waiver that only an exported variable may grant: a
    # repository's own .env cannot turn it on.
    env_only = {name.upper() for name in cs.ENVIRONMENT_ONLY_SETTINGS}
    return [
        td.SurfaceSetting(
            name=name,
            env=_env_names(name, field),
            env_only=any(env.upper() in env_only for env in _env_names(name, field)),
            type=_type_name(field.annotation),
            constraints=_constraints(field),
            default=(
                cs.SURFACE_COMPUTED_DEFAULT
                if field.default_factory is not None
                else _json_value(field.default)
            ),
        )
        for name, field in sorted(AppConfig.model_fields.items())
    ]


def graph_nodes() -> list[td.SurfaceNodeSchema]:
    return [
        td.SurfaceNodeSchema(label=str(schema.label), properties=schema.properties)
        for schema in td.NODE_SCHEMAS
    ]


def graph_relationships() -> list[td.SurfaceRelationshipSchema]:
    return [
        td.SurfaceRelationshipSchema(
            type=str(schema.rel_type),
            sources=[str(label) for label in schema.sources],
            targets=[str(label) for label in schema.targets],
        )
        for schema in td.RELATIONSHIP_SCHEMAS
    ]


def _record_field(name: str, type_name: str) -> td.SurfaceRecordField:
    # Under `from __future__ import annotations` a TypedDict cannot see
    # NotRequired[...] in its string annotations, so every key reads as
    # required in __required_keys__; the annotation itself says.
    optional = type_name.startswith(cs.SURFACE_NOT_REQUIRED_PREFIX)
    if optional:
        type_name = type_name.removeprefix(cs.SURFACE_NOT_REQUIRED_PREFIX)[:-1]
    return td.SurfaceRecordField(name=name, type=type_name, required=not optional)


def export_records() -> list[td.SurfaceRecord]:
    return [
        td.SurfaceRecord(
            name=record.__name__,
            fields=[
                _record_field(name, _type_name(annotation))
                for name, annotation in record.__annotations__.items()
            ],
        )
        for record in _EXPORT_RECORDS
    ]


def _proto_name(descriptor: Descriptor | EnumDescriptor) -> str:
    # Nested types by their path inside the package (`Relationship.RelationshipType`).
    return descriptor.full_name.removeprefix(f"{schema_pb2.DESCRIPTOR.package}.")


def _proto_field_type(field: FieldDescriptor) -> str:
    if field.message_type is not None:
        return _proto_name(field.message_type)
    if field.enum_type is not None:
        return _proto_name(field.enum_type)
    return _PROTO_TYPES[field.type]


def _proto_message(message: Descriptor) -> td.SurfaceProtoMessage:
    return td.SurfaceProtoMessage(
        name=_proto_name(message),
        fields=[
            td.SurfaceProtoField(
                name=field.name,
                number=field.number,
                type=_proto_field_type(field),
                repeated=field.is_repeated,
            )
            for field in message.fields
        ],
    )


def _all_messages(messages: Iterable[Descriptor]) -> Iterator[Descriptor]:
    for message in messages:
        yield message
        yield from _all_messages(message.nested_types)


def protobuf_messages() -> list[td.SurfaceProtoMessage]:
    return [
        _proto_message(message)
        for message in _all_messages(
            schema_pb2.DESCRIPTOR.message_types_by_name.values()
        )
    ]


def protobuf_enums() -> list[td.SurfaceProtoEnum]:
    """Every enum's names and numbers: a stored index holds the numbers, so
    reusing one reads an old index as something else."""
    enums = [
        *schema_pb2.DESCRIPTOR.enum_types_by_name.values(),
        *(
            enum
            for message in _all_messages(
                schema_pb2.DESCRIPTOR.message_types_by_name.values()
            )
            for enum in message.enum_types
        ),
    ]
    return [
        td.SurfaceProtoEnum(
            name=_proto_name(enum),
            values={value.name: value.number for value in enum.values},
        )
        for enum in sorted(enums, key=_proto_name)
    ]


def _signature_of(value: object) -> str | None:
    if not callable(value):
        return None
    try:
        return str(inspect.signature(value))
    except (TypeError, ValueError):
        return None


def _first_party(owner: type) -> bool:
    return owner.__module__.split(".")[0] in cs.SURFACE_FIRST_PARTY_MODULES


def _members(value: object) -> list[td.SurfaceSdkMember]:
    """The public methods and properties a first-party SDK class defines,
    including what it inherits from other first-party classes."""
    owner = value if isinstance(value, type) else type(value)
    if not _first_party(owner):
        return []
    found: dict[str, td.SurfaceSdkMember] = {}
    for klass in owner.__mro__:
        if not _first_party(klass):
            continue
        for name, member in vars(klass).items():
            if (
                name.startswith((cs.SURFACE_PRIVATE_PREFIX, cs.SURFACE_PYDANTIC_PREFIX))
                or name in found
            ):
                continue
            if isinstance(member, property):
                found[name] = td.SurfaceSdkMember(
                    name=name,
                    signature=cs.SURFACE_PROPERTY_SIGNATURE,
                    is_async=False,
                )
            elif isinstance(member, staticmethod | classmethod):
                found[name] = _sdk_member(name, member.__func__)
            elif inspect.isfunction(member):
                found[name] = _sdk_member(name, member)
    return [found[name] for name in sorted(found)]


def _sdk_member(name: str, function: Callable[..., object]) -> td.SurfaceSdkMember:
    # An awaited method turned plain keeps its signature but breaks every
    # caller that awaits it.
    return td.SurfaceSdkMember(
        name=name,
        signature=_signature_of(function),
        is_async=inspect.iscoroutinefunction(function),
    )


def python_sdk() -> list[td.SurfaceSdkName]:
    return [
        td.SurfaceSdkName(
            name=name,
            signature=_signature_of(getattr(cgr, name)),
            is_async=inspect.iscoroutinefunction(getattr(cgr, name)),
            members=_members(getattr(cgr, name)),
        )
        for name in sorted(cgr.__all__)
    ]


def build_surface() -> td.PublicSurface:
    return td.PublicSurface(
        version=cs.SURFACE_SNAPSHOT_VERSION,
        cli=cli_commands(),
        exit_codes=exit_codes(),
        mcp_tools=mcp_tool_list(),
        settings=setting_list(),
        graph_nodes=graph_nodes(),
        graph_relationships=graph_relationships(),
        export_json=export_records(),
        protobuf_index=protobuf_messages(),
        protobuf_enums=protobuf_enums(),
        python_sdk=python_sdk(),
    )


def render(surface: td.PublicSurface) -> str:
    return json.dumps(surface, indent=cs.SURFACE_JSON_INDENT, ensure_ascii=False) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    current = render(build_surface())
    if args.check:
        on_disk = SNAPSHOT.read_text(encoding=cs.ENCODING_UTF8)
        return 0 if on_disk == current else 1
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(current, encoding=cs.ENCODING_UTF8)
    return 0


if __name__ == "__main__":
    sys.exit(main())
