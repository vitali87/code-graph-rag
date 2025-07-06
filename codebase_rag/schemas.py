from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class GraphData(BaseModel):
    """Data model for results returned from the knowledge graph tool."""

    query_used: str
    results: list[dict[str, Any]]
    summary: str = Field(description="A brief summary of the operation's outcome.")

    @field_validator("results", mode="before")
    @classmethod
    def _format_results(cls, v: Any) -> list[dict[str, Any]]:
        if not isinstance(v, list):
            return []  # Return empty list instead of v

        clean_results = []
        for row in v:
            clean_row = {}
            for k, val in row.items():
                if not isinstance(
                    val, str | int | float | bool | list | dict | type(None)
                ):
                    clean_row[k] = str(val)
                else:
                    clean_row[k] = val  # type: ignore
            clean_results.append(clean_row)
        return clean_results

    model_config = ConfigDict(extra="forbid")


class CodeSnippet(BaseModel):
    """Data model for code snippet results."""

    qualified_name: str
    source_code: str
    file_path: str
    line_start: int
    line_end: int
    docstring: str | None = None
    found: bool = True
    error_message: str | None = None


class ShellCommandResult(BaseModel):
    """Data model for shell command results."""

    return_code: int
    stdout: str
    stderr: str
