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
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from unittest.mock import patch

import click
import pytest
from dotenv import load_dotenv
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
# Windows holds at most this many characters in one variable, `NAME=value`
# counted together, and `os.environ` raises ValueError past it.
_WINDOWS_VARIABLE_LIMIT = 32_767
_WINDOWS_REFUSAL = (
    f"the environment variable is longer than {_WINDOWS_VARIABLE_LIMIT} characters"
)
# The value CI failed on: too long for Windows to hold in its environment.
_TOO_LONG_FOR_WINDOWS = "[" * 100_000
# Three times the limit `json.loads` has for nesting on CPython 3.12 and 3.13
# (about 10,000 levels), and shown shortened in the refusal rather than echoed
# whole. Short enough for Windows to hold in a variable, so the decoder, not
# the environment, refuses it there too.
_TOO_DEEP = "[" * 30_000
_NESTED_TOO_DEEPLY = (
    "'[[[[[[[[[[[[...[[[[[[[[[[[[[' is nested too deeply to read as a JSON "
    'list, such as ["ls", "cat"].'
)


# `cgr` with `os.environ` refusing a variable as Windows does, so the value
# that failed every command there is reproduced on every OS.
_CLI_WITH_WINDOWS_LIMIT = f"""
import os
_store = os._Environ.__setitem__
def _refuse_like_windows(environ, key, value):
    if len(key) + 1 + len(value) > {_WINDOWS_VARIABLE_LIMIT}:
        raise ValueError({_WINDOWS_REFUSAL!r})
    _store(environ, key, value)
os._Environ.__setitem__ = _refuse_like_windows
{_CLI}
"""
# Variables only the `.env` loading tests set, besides python-dotenv's switch.
_SCRATCH = (
    "CGR_TEST_DOTENV_FIRST",
    "CGR_TEST_DOTENV_REFUSED",
    "CGR_TEST_DOTENV_LAST",
    "CGR_TEST_DOTENV_BARE",
    "PYTHON_DOTENV_DISABLED",
)


def _cgr(
    cwd: Path,
    *args: str,
    env: dict[str, str] | None = None,
    dotenv: str | None = None,
    script: str = _CLI,
) -> subprocess.CompletedProcess[str]:
    if dotenv is not None:
        (cwd / ".env").write_text(f"{dotenv}\n", encoding="utf-8")
    child_env = {k: v for k, v in os.environ.items() if k.upper() not in _VARIABLES}
    child_env.update(env or {})
    return subprocess.run(
        [sys.executable, "-c", script, *args],
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


@pytest.fixture
def scratch_environ(clean_env: Path) -> Iterator[Path]:
    # Loading `.env` writes the real environment; it is restored whole.
    with patch.dict(os.environ):
        for name in _SCRATCH:
            os.environ.pop(name, None)
        yield clean_env


@pytest.fixture
def windows_sized_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only writes are refused, and only past the limit, as on Windows.
    store = type(os.environ).__setitem__

    def refuse_like_windows(
        environ: MutableMapping[str, str], key: str, value: str
    ) -> None:
        if len(key) + 1 + len(value) > _WINDOWS_VARIABLE_LIMIT:
            raise ValueError(_WINDOWS_REFUSAL)
        store(environ, key, value)

    monkeypatch.setattr(type(os.environ), "__setitem__", refuse_like_windows)


def _write_dotenv(directory: Path, *lines: str) -> Path:
    path = directory / ".env"
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


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
        # (Greptile, PR #2556). Read where the environment holds no more than
        # Windows does, so it is the decoder that refuses the value on every
        # OS: one past that limit is refused by the environment on Windows
        # first (CI on PR #2556).
        dotenv = f"SHELL_COMMAND_ALLOWLIST={_TOO_DEEP}"
        windows = _CLI_WITH_WINDOWS_LIMIT

        version = _cgr(tmp_path, "--version", dotenv=dotenv, script=windows)
        usage = _cgr(tmp_path, "--help", dotenv=dotenv, script=windows)
        command = _cgr(tmp_path, *_A_COMMAND, dotenv=dotenv, script=windows)

        assert version.returncode == 0, version.stderr
        assert usage.returncode == 0, usage.stderr
        assert command.returncode == 2, command.stderr
        assert command.stderr.strip() == (
            "Error: Invalid value for SHELL_COMMAND_ALLOWLIST in ./.env: "
            f"{_NESTED_TOO_DEEPLY}"
        )
        for result in (version, usage, command):
            assert "Traceback" not in result.stdout + result.stderr

    def test_a_value_too_long_for_the_environment_is_reported_not_raised(
        self, tmp_path: Path
    ) -> None:
        # Windows refuses a variable past 32,767 characters, and the ValueError
        # escaped `load_dotenv` while the settings were imported, so every
        # command failed there, `--version` too (CI on PR #2556). The value is
        # also nested too deeply, yet is reported once, for the environment.
        dotenv = f"SHELL_COMMAND_ALLOWLIST={_TOO_LONG_FOR_WINDOWS}\nMEMGRAPH_PORT=7688"
        windows = _CLI_WITH_WINDOWS_LIMIT

        version = _cgr(tmp_path, "--version", dotenv=dotenv, script=windows)
        usage = _cgr(tmp_path, "--help", dotenv=dotenv, script=windows)
        command = _cgr(tmp_path, *_A_COMMAND, dotenv=dotenv, script=windows)

        assert version.returncode == 0, version.stderr
        assert "code-graph-rag version" in version.stdout
        assert usage.returncode == 0, usage.stderr
        assert command.returncode == 2, command.stderr
        assert command.stderr.strip() == (
            "Error: Invalid value for SHELL_COMMAND_ALLOWLIST in ./.env: "
            f"the operating system refused it ({_WINDOWS_REFUSAL})."
        )
        for result in (version, usage, command):
            assert "Traceback" not in result.stdout + result.stderr


class TestDotenvIntoTheEnvironment:
    """`.env` is merged into the environment as `load_dotenv` merged it, but a
    value the operating system will not hold is reported, not raised (#2474)."""

    def test_every_variable_in_a_normal_file_is_set(
        self, scratch_environ: Path
    ) -> None:
        dotenv = _write_dotenv(
            scratch_environ, "CGR_TEST_DOTENV_FIRST=one", "CGR_TEST_DOTENV_LAST=two"
        )

        refused = config.merge_dotenv(dotenv)

        assert refused == {}
        assert os.environ["CGR_TEST_DOTENV_FIRST"] == "one"
        assert os.environ["CGR_TEST_DOTENV_LAST"] == "two"

    def test_a_variable_already_set_is_not_overridden(
        self, scratch_environ: Path
    ) -> None:
        os.environ["CGR_TEST_DOTENV_FIRST"] = "exported"
        dotenv = _write_dotenv(
            scratch_environ,
            "CGR_TEST_DOTENV_FIRST=from-file",
            "CGR_TEST_DOTENV_LAST=${CGR_TEST_DOTENV_FIRST}",
        )

        config.merge_dotenv(dotenv)

        assert os.environ["CGR_TEST_DOTENV_FIRST"] == "exported"
        # A reference to it reads the exported value too.
        assert os.environ["CGR_TEST_DOTENV_LAST"] == "exported"

    @pytest.mark.parametrize("disabled", [None, "true"])
    def test_the_environment_ends_as_load_dotenv_leaves_it(
        self, scratch_environ: Path, disabled: str | None
    ) -> None:
        # Quoting, interpolation, a key with no value, UTF-8 and python-dotenv's
        # own off switch all behave as they did before.
        if disabled is not None:
            os.environ["PYTHON_DOTENV_DISABLED"] = disabled
        os.environ["CGR_TEST_DOTENV_FIRST"] = "exported"
        dotenv = _write_dotenv(
            scratch_environ,
            "CGR_TEST_DOTENV_FIRST=from-file",
            'CGR_TEST_DOTENV_LAST="quoted ${CGR_TEST_DOTENV_FIRST} caf\u00e9"',
            "CGR_TEST_DOTENV_BARE",
        )
        with patch.dict(os.environ):
            load_dotenv(dotenv)
            expected = dict(os.environ)

        config.merge_dotenv(dotenv)

        assert dict(os.environ) == expected

    def test_a_value_too_long_for_windows_leaves_the_rest_loaded(
        self, scratch_environ: Path, windows_sized_environ: None
    ) -> None:
        dotenv = _write_dotenv(
            scratch_environ,
            "CGR_TEST_DOTENV_FIRST=one",
            f"CGR_TEST_DOTENV_REFUSED={'x' * _WINDOWS_VARIABLE_LIMIT}",
            "CGR_TEST_DOTENV_LAST=two",
        )

        refused = config.merge_dotenv(dotenv)

        # Named with the reason, and the value is not echoed.
        assert refused == {
            "CGR_TEST_DOTENV_REFUSED": (
                "Invalid value for CGR_TEST_DOTENV_REFUSED in ./.env: "
                f"the operating system refused it ({_WINDOWS_REFUSAL})."
            )
        }
        assert "CGR_TEST_DOTENV_REFUSED" not in os.environ
        assert os.environ["CGR_TEST_DOTENV_FIRST"] == "one"
        assert os.environ["CGR_TEST_DOTENV_LAST"] == "two"

    def test_a_nul_byte_is_refused_by_every_os(self, scratch_environ: Path) -> None:
        # No OS holds a NUL in a variable, so this one needs no stand-in.
        dotenv = _write_dotenv(
            scratch_environ,
            "CGR_TEST_DOTENV_FIRST=one",
            "CGR_TEST_DOTENV_REFUSED=a\x00b",
            "CGR_TEST_DOTENV_LAST=two",
        )

        refused = config.merge_dotenv(dotenv)

        assert list(refused) == ["CGR_TEST_DOTENV_REFUSED"]
        assert refused["CGR_TEST_DOTENV_REFUSED"].startswith(
            "Invalid value for CGR_TEST_DOTENV_REFUSED in ./.env: "
            "the operating system refused it ("
        )
        assert "a\x00b" not in refused["CGR_TEST_DOTENV_REFUSED"]
        assert "CGR_TEST_DOTENV_REFUSED" not in os.environ
        assert os.environ["CGR_TEST_DOTENV_LAST"] == "two"


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


class TestAVariableTheEnvironmentRefused:
    """pydantic-settings reads `.env` itself, so it still sees a value the
    environment refused; the setting must not keep it, nor report it twice."""

    _MESSAGE = (
        "Invalid value for SHELL_COMMAND_ALLOWLIST in ./.env: "
        f"the operating system refused it ({_WINDOWS_REFUSAL})."
    )

    def test_it_is_reported_once_though_its_value_is_also_invalid(
        self, clean_env: Path
    ) -> None:
        _write_dotenv(
            clean_env,
            f"SHELL_COMMAND_ALLOWLIST={_TOO_LONG_FOR_WINDOWS}",
            "MEMGRAPH_HOST=db.internal",
        )

        loaded, errors = config.load_settings(
            frozenset(os.environ), {"SHELL_COMMAND_ALLOWLIST": self._MESSAGE}
        )

        assert errors == (self._MESSAGE,)
        assert loaded.MEMGRAPH_HOST == "db.internal"

    def test_a_valid_value_falls_back_to_the_default(self, clean_env: Path) -> None:
        _write_dotenv(clean_env, 'SHELL_COMMAND_ALLOWLIST=["ls"]')

        loaded, errors = config.load_settings(
            frozenset(os.environ), {"SHELL_COMMAND_ALLOWLIST": self._MESSAGE}
        )

        default = config.AppConfig.model_fields["SHELL_COMMAND_ALLOWLIST"]
        assert errors == (self._MESSAGE,)
        assert loaded.SHELL_COMMAND_ALLOWLIST == default.get_default(
            call_default_factory=True
        )

    def test_it_is_reported_alongside_a_value_that_fails_validation(
        self, clean_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEMGRAPH_PORT", "abc")

        loaded, errors = config.load_settings(
            frozenset(os.environ), {"SHELL_COMMAND_ALLOWLIST": self._MESSAGE}
        )

        assert loaded.MEMGRAPH_PORT == 7687
        assert errors == (
            self._MESSAGE,
            "Invalid value for MEMGRAPH_PORT in the environment: "
            "'abc' is not a valid integer.",
        )


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
