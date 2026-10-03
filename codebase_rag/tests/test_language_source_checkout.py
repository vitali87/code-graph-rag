"""`cgr language` grammar commands only ever touch a code-graph-rag checkout.

Issue #2422: `add-grammar` and `remove-language` assumed the cwd was a source
checkout. An installed cgr run inside someone's own project staged a grammar
submodule in that project, then failed on a cwd-relative
`codebase_rag/language_spec.py` and exited 0. These tests drive the commands
against throwaway git repos. Only the file transport is enabled, so no test
can reach the network, and grammar URLs point at a local repository.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner
from loguru import logger
from typer.testing import CliRunner as TyperCliRunner

from codebase_rag import constants as cs
from codebase_rag.cli import app
from codebase_rag.language_spec import LanguageSpec
from codebase_rag.tools import language
from codebase_rag.tools.language import cli

LANG = "mylang"
GRAMMAR_PATH = f"grammars/tree-sitter-{LANG}"
EMPTY_SPECS = "LANGUAGE_SPECS = {\n}\n"
# Valid Python with no dict literal to extend: the config step fails after the
# submodule step has already succeeded.
UNPATCHABLE_SPECS = "LANGUAGE_SPECS = dict()\n"
REFUSAL_MARKER = "source checkout"


def _git(cwd: Path, *args: str) -> str:
    # The test's own git calls must address `cwd` even while a test exports
    # another repository's GIT_DIR, as a hook or wrapper script would.
    env = {k: v for k, v in os.environ.items() if k not in cs.GIT_LOCATION_ENV_VARS}
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
    ).stdout


def _commit_all(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")


@pytest.fixture(autouse=True)
def _offline_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # GIT_ALLOW_PROTOCOL=file permits the local grammar clone and makes any
    # https clone fail at once, so a regression cannot reach the network.
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    empty_config = tmp_path / "gitconfig"
    empty_config.touch()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "cgr-test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "cgr-test@example.com")


@pytest.fixture
def error_logs() -> Iterator[list[str]]:
    records: list[str] = []
    sink_id = logger.add(
        lambda message: records.append(message.record["message"]), level="ERROR"
    )
    yield records
    logger.remove(sink_id)


@pytest.fixture
def grammar_url(tmp_path: Path) -> str:
    # The URL carries the tree-sitter organisation marker, so the command
    # treats it as an official grammar and does not prompt. A file URI keeps
    # forward slashes on Windows too.
    repo = tmp_path / "remote" / "github.com" / "tree-sitter" / f"tree-sitter-{LANG}"
    (repo / "src").mkdir(parents=True)
    (repo / "tree-sitter.json").write_text(
        json.dumps({"grammars": [{"name": LANG, "file-types": ["ml"]}]}),
        encoding="utf-8",
    )
    (repo / "src" / "node-types.json").write_text(
        json.dumps(
            [
                {
                    "type": "declaration",
                    "subtypes": [
                        {"type": "function_declaration"},
                        {"type": "class_declaration"},
                        {"type": "call_expression"},
                    ],
                },
                {"type": "source_file", "root": True},
            ]
        ),
        encoding="utf-8",
    )
    _commit_all(repo)
    return repo.as_uri()


def _run_from_package(monkeypatch: pytest.MonkeyPatch, package: Path) -> None:
    # raising=False keeps the fixture usable against a build without the hook,
    # so the regression tests fail on behaviour rather than on setup.
    monkeypatch.setattr(language, "_package_dir", lambda: package, raising=False)


@pytest.fixture
def installed_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # What pip, pipx and `uv tool install` leave: the package in
    # site-packages, with no checkout around it.
    package = tmp_path / "site-packages" / "codebase_rag"
    package.mkdir(parents=True)
    (package / "language_spec.py").write_text(EMPTY_SPECS, encoding="utf-8")
    _run_from_package(monkeypatch, package)
    return package


@pytest.fixture
def user_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "myproject"
    repo.mkdir()
    (repo / "app.py").write_text("print(1)\n", encoding="utf-8")
    _commit_all(repo)
    monkeypatch.chdir(repo)
    return repo


def _make_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    specs: str = EMPTY_SPECS,
    *,
    project_name: str = "code-graph-rag",
    tracked_gitmodules: bool = True,
) -> Path:
    root = tmp_path / "code-graph-rag"
    (root / "codebase_rag").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "{project_name}"\n', encoding="utf-8"
    )
    (root / "codebase_rag" / "language_spec.py").write_text(specs, encoding="utf-8")
    if tracked_gitmodules:
        # The real repository tracks an empty .gitmodules.
        (root / ".gitmodules").write_text("", encoding="utf-8")
    _commit_all(root)
    _run_from_package(monkeypatch, root / "codebase_rag")
    return root


def _spec(name: str) -> LanguageSpec:
    return LanguageSpec(
        language=name,
        file_extensions=(f".{name}",),
        function_node_types=("function_definition",),
        class_node_types=("class_definition",),
        module_node_types=("module",),
    )


def _invoke(args: list[str]) -> tuple[int, str, BaseException | None]:
    result = CliRunner().invoke(cli, args)
    return result.exit_code, result.output, result.exception


def _invoke_cgr(args: list[str]) -> tuple[int, str]:
    # Through the top-level app: the language group runs non-standalone there,
    # so this is the exit status a shell actually sees.
    result = TyperCliRunner().invoke(app, ["language", *args], prog_name="cgr")
    return result.exit_code, result.output


class TestInstalledCgrRefuses:
    def test_add_grammar_in_unrelated_repo_leaves_it_untouched(
        self, installed_package: Path, user_repo: Path, grammar_url: str
    ) -> None:
        exit_code, output = _invoke_cgr(
            ["add-grammar", LANG, "--grammar-url", grammar_url]
        )

        assert _git(user_repo, "status", "--porcelain") == ""
        assert not (user_repo / "grammars").exists()
        assert not (user_repo / ".gitmodules").exists()
        assert exit_code == 1
        assert REFUSAL_MARKER in output

    def test_add_grammar_outside_any_repo_refuses_without_traceback(
        self,
        installed_package: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        grammar_url: str,
    ) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.chdir(plain)

        exit_code, output, exception = _invoke(
            ["add-grammar", LANG, "--grammar-url", grammar_url]
        )

        assert not isinstance(exception, subprocess.CalledProcessError)
        assert list(plain.iterdir()) == []
        assert exit_code == 1
        assert isinstance(exception, SystemExit)
        assert REFUSAL_MARKER in output

    def test_remove_language_from_installed_cgr_refuses(
        self, installed_package: Path, user_repo: Path
    ) -> None:
        exit_code, output = _invoke_cgr(["remove-language", "python"])

        assert exit_code == 1
        assert REFUSAL_MARKER in output
        assert (installed_package / "language_spec.py").read_text(
            encoding="utf-8"
        ) == EMPTY_SPECS
        assert _git(user_repo, "status", "--porcelain") == ""

    def test_cleanup_orphaned_modules_from_installed_cgr_refuses(
        self, installed_package: Path, user_repo: Path
    ) -> None:
        orphan = user_repo / ".git" / "modules" / "grammars" / "tree-sitter-x"
        orphan.mkdir(parents=True)

        exit_code, output, _ = _invoke(["cleanup-orphaned-modules"])

        assert exit_code == 1
        assert REFUSAL_MARKER in output
        assert orphan.is_dir()

    def test_refusal_is_printed_once(
        self,
        installed_package: Path,
        user_repo: Path,
        grammar_url: str,
        error_logs: list[str],
    ) -> None:
        _, output, _ = _invoke(["add-grammar", LANG, "--grammar-url", grammar_url])

        assert output.count(REFUSAL_MARKER) == 1
        assert error_logs == []


class TestSourceCheckoutFailuresRollBack:
    @pytest.mark.parametrize("tracked_gitmodules", [True, False])
    def test_config_failure_removes_submodule_and_exits_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        grammar_url: str,
        error_logs: list[str],
        tracked_gitmodules: bool,
    ) -> None:
        root = _make_checkout(
            tmp_path,
            monkeypatch,
            UNPATCHABLE_SPECS,
            tracked_gitmodules=tracked_gitmodules,
        )
        monkeypatch.chdir(root)

        exit_code, output, _ = _invoke(
            ["add-grammar", LANG, "--grammar-url", grammar_url]
        )

        assert _git(root, "status", "--porcelain") == ""
        assert not (root / "grammars").exists()
        assert not (root / ".git" / "modules" / GRAMMAR_PATH).exists()
        assert (root / "codebase_rag" / "language_spec.py").read_text(
            encoding="utf-8"
        ) == UNPATCHABLE_SPECS
        assert exit_code == 1
        assert output.count("Could not find LANGUAGE_SPECS") == 1
        assert error_logs == []

    def test_failed_clone_exits_nonzero_without_traceback_or_grammars_dir(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        error_logs: list[str],
    ) -> None:
        root = _make_checkout(tmp_path, monkeypatch)
        monkeypatch.chdir(root)

        # The default https URL: the offline git environment refuses it, which
        # stands in for any clone failure.
        exit_code, output, exception = _invoke(["add-grammar", LANG])

        assert not isinstance(exception, subprocess.CalledProcessError)
        assert _git(root, "status", "--porcelain") == ""
        assert not (root / "grammars").exists()
        assert exit_code == 1
        assert isinstance(exception, SystemExit)
        assert output.count("transport 'https' not allowed") == 1
        assert error_logs == []

    def test_unexpected_git_error_exits_cleanly_and_keeps_existing_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, grammar_url: str
    ) -> None:
        root = _make_checkout(tmp_path, monkeypatch)
        monkeypatch.chdir(root)
        squatter = root / GRAMMAR_PATH / "notes.txt"
        squatter.parent.mkdir(parents=True)
        squatter.write_text("mine\n", encoding="utf-8")

        exit_code, output, exception = _invoke(
            ["add-grammar", LANG, "--grammar-url", grammar_url]
        )

        assert not isinstance(exception, subprocess.CalledProcessError)
        assert exit_code == 1
        assert isinstance(exception, SystemExit)
        assert "already exists and is not a valid git repo" in output
        assert squatter.read_text(encoding="utf-8") == "mine\n"

    def test_checkout_is_found_from_the_package_not_the_cwd(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        user_repo: Path,
        grammar_url: str,
    ) -> None:
        root = _make_checkout(tmp_path, monkeypatch)

        exit_code, output, _ = _invoke(
            ["add-grammar", LANG, "--grammar-url", grammar_url]
        )

        assert exit_code == 0, output
        assert _git(user_repo, "status", "--porcelain") == ""
        assert f'"{LANG}": LanguageSpec(' in (
            root / "codebase_rag" / "language_spec.py"
        ).read_text(encoding="utf-8")
        assert (root / GRAMMAR_PATH / "tree-sitter.json").is_file()


class TestSourceCheckoutStillWorks:
    def test_add_grammar_in_checkout_adds_submodule_and_registers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, grammar_url: str
    ) -> None:
        root = _make_checkout(tmp_path, monkeypatch)
        monkeypatch.chdir(root)

        exit_code, output, _ = _invoke(
            ["add-grammar", LANG, "--grammar-url", grammar_url]
        )

        assert exit_code == 0, output
        status = _git(root, "status", "--porcelain").splitlines()
        assert f"A  {GRAMMAR_PATH}" in status
        assert "M  .gitmodules" in status
        content = (root / "codebase_rag" / "language_spec.py").read_text(
            encoding="utf-8"
        )
        assert f'"{LANG}": LanguageSpec(' in content
        assert "'.ml'" in content

    def test_config_failure_keeps_a_submodule_that_was_already_there(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, grammar_url: str
    ) -> None:
        root = _make_checkout(tmp_path, monkeypatch, UNPATCHABLE_SPECS)
        monkeypatch.chdir(root)
        _git(root, "submodule", "add", "-q", grammar_url, GRAMMAR_PATH)
        _git(root, "commit", "-q", "-m", "grammar")

        exit_code, _, _ = _invoke(["add-grammar", LANG, "--grammar-url", grammar_url])

        assert exit_code == 1
        assert (root / GRAMMAR_PATH / "tree-sitter.json").is_file()
        assert GRAMMAR_PATH in (root / ".gitmodules").read_text(encoding="utf-8")
        assert _git(root, "status", "--porcelain") == ""

    def test_remove_language_in_checkout_removes_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        specs = (
            f'LANGUAGE_SPECS = {{\n    "{LANG}": LanguageSpec(language="{LANG}"),\n}}\n'
        )
        root = _make_checkout(tmp_path, monkeypatch, specs)
        monkeypatch.chdir(root)
        monkeypatch.setattr(language, "LANGUAGE_SPECS", {LANG: _spec(LANG)})

        exit_code, output, _ = _invoke(["remove-language", LANG, "--keep-submodule"])

        assert exit_code == 0, output
        content = (root / "codebase_rag" / "language_spec.py").read_text(
            encoding="utf-8"
        )
        assert f'"{LANG}"' not in content

    def test_vendored_package_in_another_repo_is_not_a_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, grammar_url: str
    ) -> None:
        root = _make_checkout(tmp_path, monkeypatch, project_name="someone-else")
        monkeypatch.chdir(root)

        exit_code, output, _ = _invoke(
            ["add-grammar", LANG, "--grammar-url", grammar_url]
        )

        assert exit_code == 1
        assert REFUSAL_MARKER in output
        assert _git(root, "status", "--porcelain") == ""

    def test_list_languages_still_works_from_installed_cgr(
        self, installed_package: Path, user_repo: Path
    ) -> None:
        exit_code, output, _ = _invoke(["list-languages"])

        assert exit_code == 0
        assert "Configured Languages" in output


_THIS_CHECKOUT = Path(language.__file__).resolve().parents[2]


@pytest.mark.skipif(
    not (_THIS_CHECKOUT / ".git").exists(), reason="suite not run from a git checkout"
)
def test_the_checkout_running_these_tests_is_recognised() -> None:
    assert language._source_checkout_root() == _THIS_CHECKOUT


class TestGitRepositoryVariablesAreIgnored:
    # A git hook or wrapper script can export GIT_DIR / GIT_WORK_TREE for
    # another repository, and git lets them outrank the working directory.
    def test_add_grammar_changes_the_checkout_not_the_exported_repo(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        grammar_url: str,
        user_repo: Path,
    ) -> None:
        root = _make_checkout(tmp_path, monkeypatch)
        monkeypatch.chdir(root)
        monkeypatch.setenv("GIT_DIR", str(user_repo / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(user_repo))
        monkeypatch.setenv("GIT_INDEX_FILE", str(user_repo / ".git" / "index"))

        exit_code, output, _ = _invoke(
            ["add-grammar", LANG, "--grammar-url", grammar_url]
        )

        assert exit_code == 0, output
        assert _git(user_repo, "status", "--porcelain") == ""
        assert f"A  {GRAMMAR_PATH}" in _git(root, "status", "--porcelain").splitlines()

    def test_other_git_variables_still_reach_git(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _make_checkout(tmp_path, monkeypatch)
        monkeypatch.setenv("GIT_AUTHOR_NAME", "Hook Author")

        result = language._run_git(root, "var", "GIT_AUTHOR_IDENT")

        assert result.stdout.startswith("Hook Author ")


def _linked_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, specs: str = EMPTY_SPECS
) -> tuple[Path, Path]:
    # `git worktree add` leaves a `.git` file in the new checkout that points
    # at its own git dir under the main checkout's `.git/worktrees/`.
    main = _make_checkout(tmp_path, monkeypatch, specs)
    worktree = tmp_path / "linked"
    _git(main, "worktree", "add", "-q", "-b", "linked", str(worktree))
    _run_from_package(monkeypatch, worktree / "codebase_rag")
    return main, worktree


def _modules(repo: Path, path: str) -> Path:
    # Where git itself keeps the repository of the submodule at `path`.
    return repo / _git(repo, "rev-parse", "--git-path", f"modules/{path}").strip()


class TestLinkedWorktree:
    # Review of PR #2522: in a linked worktree `.git` is a file, and the
    # grammar repositories live in the worktree's own git dir, not beneath it.
    def test_cleanup_finds_and_removes_an_orphan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        main, worktree = _linked_worktree(tmp_path, monkeypatch)
        orphan = _modules(worktree, "grammars/tree-sitter-orphan")
        assert orphan.resolve().is_relative_to((main / ".git" / "worktrees").resolve())
        orphan.mkdir(parents=True)
        (orphan / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

        result = CliRunner().invoke(cli, ["cleanup-orphaned-modules"], input="y\n")

        assert result.exit_code == 0, result.output
        assert "tree-sitter-orphan" in result.output
        assert not orphan.exists()

    def test_add_grammar_rollback_removes_the_grammar_repository(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, grammar_url: str
    ) -> None:
        _main, worktree = _linked_worktree(tmp_path, monkeypatch, UNPATCHABLE_SPECS)
        monkeypatch.chdir(worktree)

        exit_code, output, _ = _invoke(
            ["add-grammar", LANG, "--grammar-url", grammar_url]
        )

        assert exit_code == 1, output
        assert _git(worktree, "status", "--porcelain") == ""
        assert not (worktree / "grammars").exists()
        assert not _modules(worktree, GRAMMAR_PATH).exists()

    def test_remove_language_removes_the_grammar_repository(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, grammar_url: str
    ) -> None:
        specs = (
            f'LANGUAGE_SPECS = {{\n    "{LANG}": LanguageSpec(language="{LANG}"),\n}}\n'
        )
        _main, worktree = _linked_worktree(tmp_path, monkeypatch, specs)
        _git(worktree, "submodule", "add", "-q", grammar_url, GRAMMAR_PATH)
        _git(worktree, "commit", "-q", "-m", "grammar")
        grammar_repository = _modules(worktree, GRAMMAR_PATH)
        assert grammar_repository.is_dir()
        monkeypatch.chdir(worktree)
        monkeypatch.setattr(language, "LANGUAGE_SPECS", {LANG: _spec(LANG)})

        exit_code, output, _ = _invoke(["remove-language", LANG])

        assert exit_code == 0, output
        assert not grammar_repository.exists()
        assert not (worktree / GRAMMAR_PATH).exists()

    def test_cleanup_leaves_the_main_checkouts_grammars_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, grammar_url: str
    ) -> None:
        # The main checkout's grammar is not on the linked worktree's branch,
        # so its `.gitmodules` does not list it; it is still not an orphan of
        # this worktree, whose own modules directory does not hold it.
        main, worktree = _linked_worktree(tmp_path, monkeypatch)
        _git(main, "submodule", "add", "-q", grammar_url, GRAMMAR_PATH)
        _git(main, "commit", "-q", "-m", "grammar")
        main_repository = _modules(main, GRAMMAR_PATH)
        assert main_repository.is_dir()

        result = CliRunner().invoke(cli, ["cleanup-orphaned-modules"], input="y\n")

        assert result.exit_code == 0, result.output
        assert "tree-sitter-mylang" not in result.output
        assert main_repository.is_dir()
        assert _git(main, "status", "--porcelain") == ""


def test_cleanup_in_a_plain_checkout_removes_only_the_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, grammar_url: str
) -> None:
    root = _make_checkout(tmp_path, monkeypatch)
    _git(root, "submodule", "add", "-q", grammar_url, GRAMMAR_PATH)
    _git(root, "commit", "-q", "-m", "grammar")
    tracked = root / ".git" / "modules" / GRAMMAR_PATH
    orphan = root / ".git" / "modules" / "grammars" / "tree-sitter-orphan"
    orphan.mkdir(parents=True)

    result = CliRunner().invoke(cli, ["cleanup-orphaned-modules"], input="y\n")

    assert result.exit_code == 0, result.output
    assert not orphan.exists()
    assert tracked.is_dir()


def test_cleanup_in_a_checkout_that_is_a_submodule_finds_its_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A checkout cloned as another repository's submodule has a `.git` file
    # with a relative `gitdir: ../.git/modules/...`.
    source = _make_checkout(tmp_path, monkeypatch)
    parent = tmp_path / "parent"
    parent.mkdir()
    _git(parent, "init", "-q")
    _git(parent, "submodule", "add", "-q", source.as_uri(), "cgr")
    checkout = parent / "cgr"
    assert (checkout / ".git").is_file()
    _run_from_package(monkeypatch, checkout / "codebase_rag")
    orphan = _modules(checkout, "grammars/tree-sitter-orphan")
    assert orphan.resolve().is_relative_to((parent / ".git" / "modules").resolve())
    orphan.mkdir(parents=True)

    result = CliRunner().invoke(cli, ["cleanup-orphaned-modules"], input="y\n")

    assert result.exit_code == 0, result.output
    assert not orphan.exists()
