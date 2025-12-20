from pydantic import BaseModel, ConfigDict, field_validator

from .types_defs import ResultRow


class QueryGraphData(BaseModel):
    query_used: str
    results: list[ResultRow]
    summary: str

    @field_validator("results", mode="before")
    @classmethod
    def _format_results(cls, v: list[ResultRow] | None) -> list[ResultRow]:
        if not isinstance(v, list):
            return []

        clean_results: list[ResultRow] = []
        for row in v:
            clean_row: ResultRow = {
                k: (
                    val
                    if isinstance(
                        val, str | int | float | bool | list | dict | type(None)
                    )
                    else str(val)
                )
                for k, val in row.items()
            }
            clean_results.append(clean_row)
        return clean_results

    model_config = ConfigDict(extra="forbid")


class CodeSnippet(BaseModel):
    qualified_name: str
    source_code: str
    file_path: str
    line_start: int
    line_end: int
    docstring: str | None = None
    found: bool = True
    error_message: str | None = None


class ShellCommandResult(BaseModel):
    return_code: int
    stdout: str
    stderr: str
