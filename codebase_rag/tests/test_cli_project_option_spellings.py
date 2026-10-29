"""Every command that names one project accepts `--project`/`-p`.

`cgr start`, `watch`, `trace ingest` and `workspace add-repo` took only
`--project-name` while the read commands take `--project`/`-p`, and `-p`
meant the project on some commands and nothing on others. The old spellings
stay; these are aliases of the same option.
"""

from __future__ import annotations

import pytest
import typer

from codebase_rag import cli as cgr_cli
from codebase_rag.trace import cli as trace_cli
from codebase_rag.workspaces import cli as workspace_cli


def _top(name: str) -> object:
    return typer.main.get_group(cgr_cli.app).commands[name]


def _flags(command: object, name: str) -> tuple[list[str], bool]:
    # Duck-typed: a typer that vendors click builds options that are not
    # `click.Option` (#1409).
    for param in command.params:  # type: ignore[attr-defined]
        if param.param_type_name == "option" and name in param.opts:
            return [*param.opts, *param.secondary_opts], param.multiple
    raise AssertionError(name)


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(lambda: _top("start"), id="start"),
        pytest.param(lambda: _top("watch"), id="watch"),
        pytest.param(lambda: trace_cli.ingest_cmd, id="trace-ingest"),
        pytest.param(lambda: workspace_cli.add_repo_cmd, id="workspace-add-repo"),
    ],
)
def test_the_project_option_has_one_spelling_everywhere(command) -> None:
    flags, _ = _flags(command(), "--project-name")
    assert {"--project-name", "--project", "-p"} <= set(flags), flags


def test_start_takes_format_as_its_output_format() -> None:
    flags, _ = _flags(_top("start"), "--output-format")
    assert "--format" in flags


def test_start_projects_repeats() -> None:
    _, multiple = _flags(_top("start"), "--projects")
    assert multiple


def test_diff_index_takes_output_as_its_json_file() -> None:
    flags, _ = _flags(_top("diff-index"), "--json-out")
    assert {"--output", "-o"} <= set(flags)
