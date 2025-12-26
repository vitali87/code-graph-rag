from typing import TypedDict


class ToolSchema(TypedDict):
    name: str
    description: str
    inputSchema: dict[str, str | dict[str, str | dict[str, str]] | list[str]]
