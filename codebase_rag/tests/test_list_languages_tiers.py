"""`cgr language list-languages` names every language in every tier (issue #2421).

The old five-column table squeezed the Language column to zero width at 80
columns (every pipe and non-TTY), truncated it at 120, and listed only the
tree-sitter languages, so "is Ruby supported?" got the wrong answer. These
read the rendered table back cell by cell, the way a user reads it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.language_spec import LANGUAGE_SPECS, LanguageSpec
from codebase_rag.parser_loader import grammar_installed
from codebase_rag.parsers.ast_grep_tier import load_pattern_configs
from codebase_rag.parsers.document_tier import DOCUMENT_EXTENSIONS
from codebase_rag.tools.language import list_languages

_HEADER_BAR = "┃"
_BODY_BAR = "│"
_HEADER_RULE = "┡"
_BOTTOM_EDGE = "└"
_ELLIPSIS = "…"

_LANGUAGE_HEADERS = ("Language", "Extensions", "Tier", "Support", "Installed")
_NODE_HEADERS = ("Language", "Kind", "Node Types")
_FRONTEND_HEADERS = ("Frontend", "Languages", "Toolchain", "Setting", "Active")

# 80 is what every pipe and non-TTY gets; 120 is where the old table still
# truncated the names; None leaves COLUMNS unset, the real piped case; 50 is
# a narrow split pane, where the other columns must give way instead.
_WIDTHS = (50, 80, 120, None)


def _invoke(*args: str, columns: int | None = 80) -> Result:
    env = {"COLUMNS": None if columns is None else str(columns)}
    result = CliRunner().invoke(list_languages, list(args), env=env)
    assert result.exit_code == 0, result.output
    return result


def _cells(line: str, bar: str) -> list[str]:
    return [cell.strip() for cell in line.split(bar)[1:-1]]


def _squashed(text: str) -> str:
    return "".join(text.split())


def _table(output: str, headers: tuple[str, ...]) -> list[dict[str, str]]:
    """The rows of the table whose header starts with `headers`.

    A header that wraps or folds over several lines is joined back first. A
    row that wraps continues on lines whose first cell is blank; those are
    joined onto the row, so a wrapped cell reads back whole.
    """
    header_lines: list[list[str]] = []
    columns: list[str] | None = None
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        if columns is None:
            if _HEADER_BAR in line:
                header_lines.append(_cells(line, _HEADER_BAR))
            elif line.lstrip().startswith(_HEADER_RULE) and header_lines:
                joined = [
                    _squashed("".join(parts))
                    for parts in zip(*header_lines, strict=True)
                ]
                wanted = [_squashed(header) for header in headers]
                if joined[: len(headers)] == wanted:
                    # keyed by the real names, spaces and all
                    columns = [*headers, *joined[len(headers) :]]
                header_lines = []
            continue
        if line.lstrip().startswith(_BOTTOM_EDGE):
            break
        if _BODY_BAR not in line:
            continue
        cells = _cells(line, _BODY_BAR)
        if cells[0]:
            rows.append(dict(zip(columns, cells, strict=True)))
        elif rows:
            for column, cell in zip(columns, cells, strict=True):
                if cell:
                    rows[-1][column] = f"{rows[-1][column]} {cell}".strip()
    assert columns is not None, f"no table with headers {headers}:\n{output}"
    return rows


def _by_name(rows: list[dict[str, str]], column: str) -> dict[str, dict[str, str]]:
    return {row[column]: row for row in rows}


def _tokens(cell: str) -> list[str]:
    return cell.replace(",", " ").split()


def _display_name(key: str) -> str:
    meta = cs.LANGUAGE_METADATA.get(key)
    return meta.display_name if meta is not None else key


def _first_cells(output: str) -> set[str]:
    return {
        _cells(line, _BODY_BAR)[0]
        for line in output.splitlines()
        if line.count(_BODY_BAR) >= 2
    }


# --- red: the reported defects ---------------------------------------------


@pytest.mark.parametrize("columns", _WIDTHS)
def test_first_column_names_every_tree_sitter_language(columns: int | None) -> None:
    # Layout-agnostic: whatever the columns are, the first one must name the
    # language (by key or display name), in full.
    names = _first_cells(_invoke(columns=columns).output)

    for key in LANGUAGE_SPECS:
        assert names & {key, _display_name(key)}, (
            f"{key} not named at {columns} columns: {sorted(names)}"
        )


def test_ruby_and_markdown_are_mentioned_at_all() -> None:
    # The issue's reproduction: `list-languages | grep -ci ruby` printed 0.
    output = _invoke(columns=250).output.lower()

    for needle in ("ruby", ".rb", "markdown", ".md"):
        assert needle in output, needle


@pytest.mark.parametrize("columns", _WIDTHS)
def test_every_tree_sitter_language_is_named_with_all_its_extensions(
    columns: int | None,
) -> None:
    rows = _by_name(
        _table(_invoke(columns=columns).output, _LANGUAGE_HEADERS), "Language"
    )

    for key, spec in LANGUAGE_SPECS.items():
        name = _display_name(key)
        assert name in rows, f"{name} missing at {columns} columns: {sorted(rows)}"
        assert _tokens(rows[name]["Extensions"]) == list(spec.file_extensions)


@pytest.mark.parametrize("columns", _WIDTHS)
def test_no_cell_is_truncated(columns: int | None) -> None:
    assert _ELLIPSIS not in _invoke(columns=columns).output


def test_ast_grep_languages_are_listed_with_their_extensions() -> None:
    rows = _table(_invoke().output, _LANGUAGE_HEADERS)
    structural = [row for row in rows if row["Tier"] == "ast-grep"]
    listed = {ext for row in structural for ext in _tokens(row["Extensions"])}

    assert listed == set(load_pattern_configs())
    assert "Ruby" in {row["Language"] for row in structural}
    assert {row["Support"] for row in structural} == {"structural"}


def test_markdown_is_listed_as_the_document_tier() -> None:
    rows = _by_name(_table(_invoke().output, _LANGUAGE_HEADERS), "Language")

    assert rows["Markdown"]["Tier"] == "document"
    assert set(_tokens(rows["Markdown"]["Extensions"])) == DOCUMENT_EXTENSIONS


def test_default_table_leaves_node_types_out() -> None:
    # The node-type columns are what squeezed the names out; they belong
    # behind --verbose, not in the table most users run this for. Wide, so a
    # node type is not merely truncated out of sight.
    output = _invoke(columns=250).output

    assert "function_definition" not in output


@pytest.mark.parametrize("columns", (80, 120))
def test_verbose_lists_every_node_mapping_untruncated(columns: int) -> None:
    output = _invoke("--verbose", columns=columns).output
    rows = _by_name(_table(output, _NODE_HEADERS), "Language")

    assert _ELLIPSIS not in output
    for key, spec in LANGUAGE_SPECS.items():
        listed = _tokens(rows[_display_name(key)]["Node Types"])
        for node_type in (
            *spec.function_node_types,
            *spec.class_node_types,
            *spec.module_node_types,
            *spec.call_node_types,
        ):
            assert node_type in listed, f"{key}: {node_type}"


def test_optional_frontends_are_listed() -> None:
    rows = _by_name(_table(_invoke().output, _FRONTEND_HEADERS), "Frontend")

    assert set(rows) == {"libclang", "go/types", "Roslyn", "javac", "Jedi"}
    assert rows["libclang"]["Languages"] == "C, C++"


# --- negative: what must not change, and what must not be overclaimed -----


def test_tree_sitter_languages_keep_their_tier_and_documented_support() -> None:
    # Scala and SQL are "In Development" in the README and the docs matrix;
    # the rest are fully supported. The listing must not promote or drop any.
    rows = _by_name(_table(_invoke(columns=200).output, _LANGUAGE_HEADERS), "Language")

    for key, spec in LANGUAGE_SPECS.items():
        row = rows[_display_name(key)]
        assert row["Tier"] == "tree-sitter"
        assert _tokens(row["Extensions"]) == list(spec.file_extensions)
        expected = (
            "full"
            if cs.LANGUAGE_METADATA[key].status == cs.LanguageStatus.FULL
            else "in development"
        )
        assert row["Support"] == expected, key


def test_verbose_leaves_the_language_table_as_it_was() -> None:
    plain = _table(_invoke().output, _LANGUAGE_HEADERS)
    verbose = _table(_invoke("--verbose").output, _LANGUAGE_HEADERS)

    assert verbose == plain


def test_missing_grammar_is_not_claimed_installed() -> None:
    with patch(
        "codebase_rag.tools.language_catalog.grammar_installed",
        side_effect=lambda lang: lang != cs.SupportedLanguage.RUST,
    ):
        result = _invoke()
    rows = _by_name(_table(result.output, _LANGUAGE_HEADERS), "Language")

    assert rows["Rust"]["Installed"] == "no"
    assert rows["Python"]["Installed"] == "yes"
    assert "code-graph-rag[treesitter-full]" in result.output


def test_ast_grep_tier_without_its_extra_is_not_claimed_installed() -> None:
    with patch("codebase_rag.tools.language_catalog.has_ast_grep", return_value=False):
        result = _invoke()
    rows = _table(result.output, _LANGUAGE_HEADERS)

    structural = [row for row in rows if row["Tier"] == "ast-grep"]
    assert structural, "the tier's languages are still listed"
    assert {row["Installed"] for row in structural} == {"no"}
    assert {row["Installed"] for row in rows if row["Tier"] == "tree-sitter"} == {"yes"}
    assert "code-graph-rag[ast-grep]" in result.output


def test_markdown_without_its_grammar_is_not_claimed_installed() -> None:
    with patch(
        "codebase_rag.tools.language_catalog.document_tier_available",
        return_value=False,
    ):
        rows = _by_name(_table(_invoke().output, _LANGUAGE_HEADERS), "Language")

    assert rows["Markdown"]["Installed"] == "no"


def test_no_install_hint_when_everything_is_installed() -> None:
    with (
        patch(
            "codebase_rag.tools.language_catalog.grammar_installed", return_value=True
        ),
        patch("codebase_rag.tools.language_catalog.has_ast_grep", return_value=True),
        patch(
            "codebase_rag.tools.language_catalog.document_tier_available",
            return_value=True,
        ),
    ):
        output = _invoke().output

    assert "pip install" not in output


def test_unreadable_ast_grep_configs_leave_the_other_tiers_listed() -> None:
    with patch(
        "codebase_rag.tools.language_catalog.structural_tier_languages",
        side_effect=ValueError(
            "ruby.yaml: 'extensions' and 'ast_grep_id' are required"
        ),
    ):
        rows = _table(_invoke().output, _LANGUAGE_HEADERS)

    names = {row["Language"] for row in rows}
    assert "Python" in names
    assert "Markdown" in names
    assert not [row for row in rows if row["Tier"] == "ast-grep"]


def test_added_grammar_is_listed_by_key_without_claiming_full_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A spec written by `add-grammar` has no metadata and, with no grammar
    # submodule under the working directory, nothing to parse with.
    monkeypatch.chdir(tmp_path)
    spec = LanguageSpec(
        language="mylang",
        file_extensions=(".ml",),
        function_node_types=("function_definition",),
        class_node_types=(),
        module_node_types=("source_file",),
    )
    with patch.dict(LANGUAGE_SPECS, {"mylang": spec}):
        rows = _by_name(_table(_invoke().output, _LANGUAGE_HEADERS), "Language")

    assert rows["mylang"]["Extensions"] == ".ml"
    assert rows["mylang"]["Tier"] == "tree-sitter"
    assert rows["mylang"]["Support"] == "in development"
    assert rows["mylang"]["Installed"] == "no"


def test_enabled_frontend_without_its_toolchain_is_not_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The resolver indexing uses sees no `go`, so the listing must not claim
    # go/types even though the setting asks for it.
    monkeypatch.setattr(settings, "GO_FRONTEND", cs.GoFrontend.GOTYPES)
    monkeypatch.setattr(
        "codebase_rag.parsers.go_frontend.frontend.go_frontend_available",
        lambda: False,
    )
    rows = _by_name(_table(_invoke().output, _FRONTEND_HEADERS), "Frontend")

    assert rows["go/types"]["Toolchain"] == "missing"
    assert rows["go/types"]["Active"] == "no"
    assert rows["go/types"]["Setting"] == "GO_FRONTEND=gotypes"


def test_disabled_frontend_is_not_active_even_with_its_toolchain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "JAVA_FRONTEND", cs.JavaFrontend.HEURISTIC)
    monkeypatch.setattr(
        "codebase_rag.parsers.java_frontend.java_frontend_available", lambda: True
    )
    rows = _by_name(_table(_invoke().output, _FRONTEND_HEADERS), "Frontend")

    assert rows["javac"]["Toolchain"] == "found"
    assert rows["javac"]["Active"] == "no"
    assert rows["javac"]["Setting"] == "JAVA_FRONTEND=heuristic"


def test_enabled_frontend_probes_its_toolchain_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The javac probe starts two JVMs; the listing must not pay for it twice.
    probe = MagicMock(return_value=True)
    monkeypatch.setattr(settings, "JAVA_FRONTEND", cs.JavaFrontend.JAVAC)
    monkeypatch.setattr(
        "codebase_rag.parsers.java_frontend.frontend.java_frontend_available", probe
    )
    monkeypatch.setattr(
        "codebase_rag.parsers.java_frontend.java_frontend_available", probe
    )
    rows = _by_name(_table(_invoke().output, _FRONTEND_HEADERS), "Frontend")

    assert rows["javac"]["Active"] == "yes"
    assert rows["javac"]["Toolchain"] == "found"
    assert probe.call_count == 1


def test_grammar_installed_finds_the_core_python_wheel() -> None:
    assert grammar_installed(cs.SupportedLanguage.PYTHON) is True


def test_grammar_installed_reports_a_missing_grammar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with patch("codebase_rag.parser_loader._import_pip_grammar", return_value=None):
        assert grammar_installed(cs.SupportedLanguage.RUST) is False


def test_grammar_installed_never_builds_a_submodule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The loader runs the submodule's setup.py on first use; answering
    # "installed?" must not start that build.
    monkeypatch.chdir(tmp_path)
    submodule = tmp_path / "grammars" / "tree-sitter-rust"
    (submodule / "bindings" / "python").mkdir(parents=True)
    (submodule / "setup.py").write_text("", encoding="utf-8")
    # an empty loader cache, so a route through the real loader would build
    with (
        patch.dict("codebase_rag.parser_loader._loader_cache", clear=True),
        patch("codebase_rag.parser_loader._import_pip_grammar", return_value=None),
        patch("codebase_rag.parser_loader.subprocess.run") as run,
    ):
        assert grammar_installed(cs.SupportedLanguage.RUST) is True
    run.assert_not_called()
