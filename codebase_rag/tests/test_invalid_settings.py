"""One invalid setting must not take every `cgr` command down with it (#2474).

`codebase_rag.config` builds `settings` when it is imported, and every `cgr`
invocation imports it before parsing its arguments. A single malformed value
in `.env` or the environment used to fail `--version` and `--help` with a raw
pydantic traceback, and a value that parsed but broke later (a zero batch size,
an empty host) failed deep inside a command instead of naming the variable.
"""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import click
import pytest
from pydantic import ValidationError
from pydantic_settings import EnvSettingsSource
from typer.testing import CliRunner

from codebase_rag import cli, config

# The `cgr` entry point is `codebase_rag.cli:app`.
_CLI = "from codebase_rag.cli import app; app()"
# Deadline for the child interpreter, not a performance assertion.
_TIMEOUT_SECONDS = 120
# Every setting read as a JSON list.
_LIST_SETTINGS = (
    "SHELL_COMMAND_ALLOWLIST",
    "SHELL_READ_ONLY_COMMANDS",
    "SHELL_SAFE_GIT_SUBCOMMANDS",
    "SHELL_NONINTERACTIVE_READ_COMMANDS",
)
# The variables these tests set; the child inherits none of the developer's.
_VARIABLES = (
    *_LIST_SETTINGS,
    "MEMGRAPH_HOST",
    "MEMGRAPH_PORT",
    "MEMGRAPH_BATCH_SIZE",
    "CGR_SKIP_EMBEDDINGS",
    "CPP_FRONTEND",
    "GRAPH_BACKEND",
    "QDRANT_BATCH_SIZE",
)
# Resolves the batch size before it connects, so a bad value is reached
# without a database.
_A_COMMAND = ("delete-project", "--name", "demo")
_NOT_JSON = '\'ls,cat\' is not a JSON list, such as ["ls", "cat"].'
# Far past any interpreter's recursion limit for `json.loads`, and shown
# shortened in the refusal rather than echoed whole.
_TOO_DEEP = "[" * 100_000
_NESTED_TOO_DEEPLY = (
    "'[[[[[[[[[[[[...[[[[[[[[[[[[[' is nested too deeply to read as a JSON "
    'list, such as ["ls", "cat"].'
)


def _cgr(
    cwd: Path,
    *args: str,
    env: dict[str, str] | None = None,
    dotenv: str | None = None,
) -> subprocess.CompletedProcess[str]:
    if dotenv is not None:
        (cwd / ".env").write_text(f"{dotenv}\n", encoding="utf-8")
    child_env = {k: v for k, v in os.environ.items() if k.upper() not in _VARIABLES}
    child_env.update(env or {})
    return subprocess.run(
        [sys.executable, "-c", _CLI, *args],
        cwd=cwd,
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    # In-process loads read `.env` from the working directory, so run them
    # from an empty one with none of the variables under test set.
    for name in _VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestTheCliStartsWithAnInvalidSetting:
    def test_version_works_with_an_invalid_value_in_dotenv(
        self, tmp_path: Path
    ) -> None:
        result = _cgr(tmp_path, "--version", dotenv="MEMGRAPH_PORT=76 87")

        assert result.returncode == 0, result.stderr
        assert "code-graph-rag version" in result.stdout

    def test_help_works_with_an_invalid_value_in_the_environment(
        self, tmp_path: Path
    ) -> None:
        result = _cgr(tmp_path, "--help", env={"MEMGRAPH_PORT": "abc"})

        assert result.returncode == 0, result.stderr
        assert "Usage" in result.stdout

    def test_a_command_names_the_variable_value_and_file_in_one_line(
        self, tmp_path: Path
    ) -> None:
        result = _cgr(tmp_path, *_A_COMMAND, dotenv="MEMGRAPH_PORT=76 87")

        assert result.returncode == 2
        assert result.stderr.strip() == (
            "Error: Invalid value for MEMGRAPH_PORT in ./.env: "
            "'76 87' is not a valid integer."
        )
        assert "Traceback" not in result.stdout + result.stderr

    def test_an_exported_value_is_traced_to_the_environment(
        self, tmp_path: Path
    ) -> None:
        # The exported value wins over `.env`, so it is the one to name.
        result = _cgr(
            tmp_path,
            *_A_COMMAND,
            env={"MEMGRAPH_PORT": "abc"},
            dotenv="MEMGRAPH_PORT=76 87",
        )

        assert result.returncode == 2
        assert result.stderr.strip() == (
            "Error: Invalid value for MEMGRAPH_PORT in the environment: "
            "'abc' is not a valid integer."
        )

    def test_every_invalid_variable_gets_its_own_line(self, tmp_path: Path) -> None:
        result = _cgr(
            tmp_path,
            *_A_COMMAND,
            env={"MEMGRAPH_PORT": "abc", "CGR_SKIP_EMBEDDINGS": "maybe"},
        )

        assert result.returncode == 2
        lines = result.stderr.strip().splitlines()
        assert len(lines) == 2, result.stderr
        assert any("MEMGRAPH_PORT" in line for line in lines)
        assert any("CGR_SKIP_EMBEDDINGS" in line for line in lines)

    def test_the_batch_size_variable_gets_the_flags_range_check(
        self, tmp_path: Path
    ) -> None:
        result = _cgr(tmp_path, *_A_COMMAND, env={"MEMGRAPH_BATCH_SIZE": "0"})

        assert result.returncode == 2
        assert result.stderr.strip() == (
            "Error: Invalid value for MEMGRAPH_BATCH_SIZE in the environment: "
            "0 is not in the range x>=1."
        )
        assert "Traceback" not in result.stdout + result.stderr

    def test_a_list_setting_that_is_not_json_is_reported_not_raised(
        self, tmp_path: Path
    ) -> None:
        env = {"SHELL_COMMAND_ALLOWLIST": "ls,cat"}

        version = _cgr(tmp_path, "--version", env=env)
        command = _cgr(tmp_path, *_A_COMMAND, env=env)

        assert version.returncode == 0, version.stderr
        assert command.returncode == 2
        assert command.stderr.strip() == (
            "Error: Invalid value for SHELL_COMMAND_ALLOWLIST in the environment: "
            f"{_NOT_JSON}"
        )

    def test_a_list_setting_nested_too_deeply_is_reported_not_raised(
        self, tmp_path: Path
    ) -> None:
        # `json.loads` raises RecursionError, not JSONDecodeError, on arrays
        # nested past the interpreter's limit, and it escaped the import of
        # the settings: `--version` and `--help` failed with a traceback
        # (Greptile, PR #2556). Written to `.env`, as no OS takes a variable
        # this long from the environment.
        dotenv = f"SHELL_COMMAND_ALLOWLIST={_TOO_DEEP}"

        version = _cgr(tmp_path, "--version", dotenv=dotenv)
        usage = _cgr(tmp_path, "--help", dotenv=dotenv)
        command = _cgr(tmp_path, *_A_COMMAND, dotenv=dotenv)

        assert version.returncode == 0, version.stderr
        assert usage.returncode == 0, usage.stderr
        assert command.returncode == 2, command.stderr
        assert command.stderr.strip() == (
            "Error: Invalid value for SHELL_COMMAND_ALLOWLIST in ./.env: "
            f"{_NESTED_TOO_DEEPLY}"
        )
        for result in (version, usage, command):
            assert "Traceback" not in result.stdout + result.stderr


class TestEmptyValues:
    def test_an_empty_host_in_the_environment_means_the_default(
        self, clean_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEMGRAPH_HOST", "")

        assert config.AppConfig().MEMGRAPH_HOST == "localhost"

    def test_an_empty_host_in_dotenv_means_the_default(self, clean_env: Path) -> None:
        # The blank line a user copies from `.env.example` and never fills in.
        (clean_env / ".env").write_text("MEMGRAPH_HOST=\n", encoding="utf-8")

        assert config.AppConfig().MEMGRAPH_HOST == "localhost"

    def test_a_host_that_is_set_is_kept(
        self, clean_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEMGRAPH_HOST", "db.internal")

        assert config.AppConfig().MEMGRAPH_HOST == "db.internal"


class TestLoadSettings:
    def test_a_valid_configuration_has_no_errors(
        self, clean_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEMGRAPH_PORT", "7688")

        loaded, errors = config.load_settings(frozenset(os.environ))

        assert errors == ()
        assert loaded.MEMGRAPH_PORT == 7688

    def test_only_the_invalid_variable_falls_back_to_its_default(
        self, clean_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The rest of the configuration is still the user's, so a library
        # caller that ignores the errors does not silently lose it all.
        monkeypatch.setenv("MEMGRAPH_PORT", "abc")
        monkeypatch.setenv("MEMGRAPH_HOST", "db.internal")

        loaded, errors = config.load_settings(frozenset(os.environ))

        assert loaded.MEMGRAPH_PORT == 7687
        assert loaded.MEMGRAPH_HOST == "db.internal"
        assert errors == (
            "Invalid value for MEMGRAPH_PORT in the environment: "
            "'abc' is not a valid integer.",
        )

    def test_a_value_only_in_dotenv_is_traced_to_the_file(
        self, clean_env: Path
    ) -> None:
        (clean_env / ".env").write_text("MEMGRAPH_PORT=76 87\n", encoding="utf-8")

        _, errors = config.load_settings(frozenset(os.environ))

        assert errors == (
            "Invalid value for MEMGRAPH_PORT in ./.env: '76 87' is not a valid integer.",
        )

    def test_a_variable_with_an_alias_is_named_and_reset(
        self, clean_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CGR_SKIP_EMBEDDINGS", "maybe")

        loaded, errors = config.load_settings(frozenset(os.environ))

        assert loaded.SKIP_EMBEDDINGS is False
        assert errors == (
            "Invalid value for CGR_SKIP_EMBEDDINGS in the environment: "
            "'maybe' is not a valid boolean.",
        )

    @pytest.mark.parametrize(
        ("name", "value", "problem"),
        [
            ("CPP_FRONTEND", "bogus", "'bogus' is not one of 'treesitter', "),
            ("QDRANT_BATCH_SIZE", "0", "0 is not in the range x>0."),
            ("MEMGRAPH_BATCH_SIZE", "-3", "-3 is not in the range x>=1."),
            ("GRAPH_BACKEND", "bogus", "GRAPH_BACKEND must be one of "),
        ],
    )
    def test_each_kind_of_refusal_reads_like_a_flag_error(
        self,
        clean_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        value: str,
        problem: str,
    ) -> None:
        monkeypatch.setenv(name, value)

        _, errors = config.load_settings(frozenset(os.environ))

        assert len(errors) == 1
        assert errors[0].startswith(f"Invalid value for {name} in the environment: ")
        assert problem in errors[0]


class TestAListSettingThatIsNotJson:
    """A list setting is read as JSON. One that is not JSON used to fail while
    pydantic-settings read the source, before validation, and the fallback
    dropped every other setting with it."""

    def test_the_other_settings_from_the_environment_are_kept(
        self, clean_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SHELL_COMMAND_ALLOWLIST", "ls,cat")
        monkeypatch.setenv("MEMGRAPH_HOST", "db.internal")

        loaded, errors = config.load_settings(frozenset(os.environ))

        assert loaded.MEMGRAPH_HOST == "db.internal"
        assert loaded.SHELL_COMMAND_ALLOWLIST == (
            config.AppConfig.model_fields["SHELL_COMMAND_ALLOWLIST"].default
        )
        assert errors == (
            f"Invalid value for SHELL_COMMAND_ALLOWLIST in the environment: "
            f"{_NOT_JSON}",
        )

    def test_the_other_settings_from_dotenv_are_kept(self, clean_env: Path) -> None:
        (clean_env / ".env").write_text(
            "SHELL_COMMAND_ALLOWLIST=ls,cat\nMEMGRAPH_HOST=db.internal\n",
            encoding="utf-8",
        )

        loaded, errors = config.load_settings(frozenset(os.environ))

        assert loaded.MEMGRAPH_HOST == "db.internal"
        assert errors == (
            f"Invalid value for SHELL_COMMAND_ALLOWLIST in ./.env: {_NOT_JSON}",
        )

    def test_it_is_reported_alongside_a_value_that_fails_validation(
        self, clean_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SHELL_COMMAND_ALLOWLIST", "ls,cat")
        monkeypatch.setenv("MEMGRAPH_PORT", "abc")
        monkeypatch.setenv("MEMGRAPH_HOST", "db.internal")

        loaded, errors = config.load_settings(frozenset(os.environ))

        assert loaded.MEMGRAPH_HOST == "db.internal"
        assert loaded.MEMGRAPH_PORT == 7687
        assert sorted(error.split(" in ")[0] for error in errors) == [
            "Invalid value for MEMGRAPH_PORT",
            "Invalid value for SHELL_COMMAND_ALLOWLIST",
        ]

    def test_one_nested_too_deeply_falls_back_to_its_default(
        self, clean_env: Path
    ) -> None:
        (clean_env / ".env").write_text(
            f"SHELL_COMMAND_ALLOWLIST={_TOO_DEEP}\nMEMGRAPH_HOST=db.internal\n",
            encoding="utf-8",
        )

        loaded, errors = config.load_settings(frozenset(os.environ))

        assert errors == (
            "Invalid value for SHELL_COMMAND_ALLOWLIST in ./.env: "
            f"{_NESTED_TOO_DEEPLY}",
        )
        default = config.AppConfig.model_fields["SHELL_COMMAND_ALLOWLIST"]
        assert loaded.SHELL_COMMAND_ALLOWLIST == default.get_default(
            call_default_factory=True
        )
        assert loaded.MEMGRAPH_HOST == "db.internal"

    def test_a_nested_list_that_decodes_is_refused_by_type_as_before(
        self, clean_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Negative: nesting the decoder can read reaches type validation,
        # which refuses it as a list of non-strings, as before.
        monkeypatch.setenv("SHELL_COMMAND_ALLOWLIST", '[["ls"]]')

        _, errors = config.load_settings(frozenset(os.environ))

        assert len(errors) == 1
        assert errors[0].startswith("Invalid value for SHELL_COMMAND_ALLOWLIST ")
        assert _NESTED_TOO_DEEPLY not in errors[0]

    @pytest.mark.parametrize("name", _LIST_SETTINGS)
    def test_a_json_list_is_still_honoured(
        self, clean_env: Path, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        monkeypatch.setenv(name, '["ls", "cat"]')

        loaded, errors = config.load_settings(frozenset(os.environ))

        assert errors == ()
        assert getattr(loaded, name) == frozenset({"ls", "cat"})

    def test_the_json_list_cases_cover_every_list_setting(self) -> None:
        # Decoding is off at the source, so a list setting the validator does
        # not name would refuse every JSON value it is given.
        source = EnvSettingsSource(config.AppConfig)
        list_settings = {
            name
            for name, field in config.AppConfig.model_fields.items()
            if source.field_is_complex(field)
        }

        assert list_settings == set(_LIST_SETTINGS)

    def test_a_list_passed_in_code_is_still_accepted(self, clean_env: Path) -> None:
        loaded = config.AppConfig(SHELL_COMMAND_ALLOWLIST=["ls"])

        assert loaded.SHELL_COMMAND_ALLOWLIST == frozenset({"ls"})

    def test_building_app_config_directly_still_raises(
        self, clean_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SHELL_COMMAND_ALLOWLIST", "ls,cat")

        with pytest.raises(ValueError, match="SHELL_COMMAND_ALLOWLIST"):
            config.AppConfig()


class TestWhatStaysTheSame:
    def test_building_app_config_directly_still_raises(
        self, clean_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Only the module-level `settings` is lenient; a caller that builds its
        # own configuration still gets the strict error.
        monkeypatch.setenv("MEMGRAPH_PORT", "abc")

        with pytest.raises(ValidationError, match="MEMGRAPH_PORT"):
            config.AppConfig()

    def test_a_batch_size_of_one_is_accepted(
        self, clean_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEMGRAPH_BATCH_SIZE", "1")

        loaded, errors = config.load_settings(frozenset(os.environ))

        assert errors == ()
        assert loaded.MEMGRAPH_BATCH_SIZE == 1

    def test_the_batch_size_flag_keeps_its_own_check(self) -> None:
        result = CliRunner().invoke(cli.app, ["start", "--batch-size", "0"])

        assert result.exit_code == 2
        assert "0 is not in the range x>=1." in result.output

    def test_a_valid_configuration_runs_the_command(self) -> None:
        # A blank name is the command's own refusal, reached only once the
        # start-up check has let the command run.
        with patch.object(cli, "settings_errors", ()):
            result = CliRunner().invoke(cli.app, ["delete-project", "--name", " "])

        assert result.exit_code == 1, result.output
        assert "--name is required" in result.output

    def test_cgr_help_still_works_while_a_setting_is_invalid(self) -> None:
        errors = ("Invalid value for MEMGRAPH_PORT in ./.env: 'x' is not valid.",)
        with patch.object(cli, "settings_errors", errors):
            result = CliRunner().invoke(cli.app, ["help", "start"])
        # CI forces colour, so rich wraps each option name in ANSI spans.
        help_text = click.unstyle(result.output)

        assert result.exit_code == 0, result.output
        assert "--batch-size" in help_text

    def test_a_command_is_refused_while_a_setting_is_invalid(self) -> None:
        errors = ("Invalid value for MEMGRAPH_PORT in ./.env: 'x' is not valid.",)
        with patch.object(cli, "settings_errors", errors):
            result = CliRunner().invoke(cli.app, list(_A_COMMAND))

        assert result.exit_code == 2
        assert f"Error: {errors[0]}" in result.output
