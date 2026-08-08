"""Regression tests for the local pre-commit hooks (issue #1097).

A bare ``uv run`` re-syncs the environment to the default dependency set and
re-resolves the lockfile before executing, so a commit silently uninstalls every
extra from the developer's virtualenv and rewrites ``uv.lock`` underneath the
commit. The local hooks must therefore run with ``--no-sync`` and ``--frozen``.
"""

import ast
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / ".pre-commit-config.yaml"

REQUIRED_UV_RUN_FLAGS = ("--no-sync", "--frozen")


def _local_hook_entries() -> list[tuple[str, list[str]]]:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    return [
        (hook["id"], hook["entry"].split())
        for repo in config["repos"]
        if repo["repo"] == "local"
        for hook in repo["hooks"]
        if "entry" in hook
    ]


def _uv_run_hooks() -> list[tuple[str, list[str]]]:
    return [
        (hook_id, tokens)
        for hook_id, tokens in _local_hook_entries()
        if tokens[:2] == ["uv", "run"]
    ]


def _uv_run_flags(tokens: list[str]) -> list[str]:
    flags = []
    for token in tokens[2:]:
        if not token.startswith("-"):
            break
        flags.append(token)
    return flags


def test_local_hooks_exercise_uv_run() -> None:
    assert _uv_run_hooks(), (
        "expected at least one local hook launched through `uv run`; "
        "these tests guard flags that would otherwise silently disappear"
    )


def test_uv_run_hooks_do_not_mutate_the_developer_environment() -> None:
    offenders = {
        hook_id: [
            flag for flag in REQUIRED_UV_RUN_FLAGS if flag not in _uv_run_flags(tokens)
        ]
        for hook_id, tokens in _uv_run_hooks()
    }
    missing = {hook_id: flags for hook_id, flags in offenders.items() if flags}
    assert missing == {}, (
        f"local hooks are missing required `uv run` flags: {missing}. "
        "Without --no-sync a commit strips extras from the developer's venv; "
        "without --frozen it rewrites uv.lock mid-commit"
    )


def test_required_flags_precede_the_hook_command() -> None:
    for hook_id, tokens in _uv_run_hooks():
        flags = _uv_run_flags(tokens)
        for required in REQUIRED_UV_RUN_FLAGS:
            assert required in flags, (
                f"hook {hook_id!r} must pass {required} to `uv run` itself, not "
                f"to the command it launches: {' '.join(tokens)!r}"
            )


def uv_run_vectors(source: str) -> list[list[str]]:
    """Every static ``["uv", "run", ...]`` argument vector in a script.

    Parsed per call rather than grepped over the whole file: one guarded
    invocation must not vouch for a second bare one sitting beside it.
    """
    tree = ast.parse(source)
    modules, direct = _subprocess_aliases(tree)
    vectors: list[list[str]] = []
    for node in ast.walk(tree):
        # Only the argv of an actual subprocess launch counts. Matching every
        # list literal would flag an unrelated `EXAMPLE = ["uv", "run", ...]`
        # constant that never runs anything.
        if not isinstance(node, ast.Call) or not _is_subprocess_launch(
            node.func, modules, direct
        ):
            continue
        for argument in [*node.args, *(kw.value for kw in node.keywords)]:
            if not isinstance(argument, ast.List | ast.Tuple):
                continue
            literals = [
                element.value
                for element in argument.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            ]
            if len(literals) == len(argument.elts) and literals[:2] == ["uv", "run"]:
                vectors.append(literals)
    return vectors


LAUNCHERS = frozenset({"run", "call", "check_call", "check_output", "Popen"})


def _subprocess_aliases(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Names bound to `subprocess`, and launcher names imported from it.

    A method merely *called* `run` proves nothing — `runner.run([...])` is not
    a child process. Only a callee that resolves back to `subprocess` counts.
    """
    modules: set[str] = set()
    direct: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in LAUNCHERS:
                    direct.add(alias.asname or alias.name)
    return modules, direct


def _is_subprocess_launch(func: ast.expr, modules: set[str], direct: set[str]) -> bool:
    """Whether a call expression launches a child process."""
    if isinstance(func, ast.Attribute):
        return (
            func.attr in LAUNCHERS
            and isinstance(func.value, ast.Name)
            and func.value.id in modules
        )
    return isinstance(func, ast.Name) and func.id in direct


def unguarded_uv_run_vectors(source: str) -> list[list[str]]:
    """The vectors missing a required flag BEFORE the launched command."""
    return [
        vector
        for vector in uv_run_vectors(source)
        if any(flag not in _uv_run_flags(vector) for flag in REQUIRED_UV_RUN_FLAGS)
    ]


def test_hook_scripts_do_not_shell_out_to_a_bare_uv_run() -> None:
    hook_scripts = sorted((REPO_ROOT / "scripts" / "hooks").glob("*.py"))
    assert hook_scripts, "expected hook scripts under scripts/hooks/"

    offenders = {
        script.name: unguarded_uv_run_vectors(script.read_text(encoding="utf-8"))
        for script in hook_scripts
    }
    unguarded = {name: vectors for name, vectors in offenders.items() if vectors}

    assert unguarded == {}, (
        f"hook scripts shell out to `uv run` without required flags: {unguarded}. "
        "A nested bare `uv run` re-syncs the venv and rewrites uv.lock even when "
        "the pre-commit entry itself is guarded"
    )


class TestNestedInvocationDetection:
    GUARDED = 'import subprocess\nsubprocess.run(["uv", "run", "--frozen", "--no-sync", "python", "x.py"])'
    BARE = 'import subprocess\nsubprocess.run(["uv", "run", "python", "y.py"])'

    def test_a_guarded_invocation_alone_passes(self) -> None:
        assert unguarded_uv_run_vectors(self.GUARDED) == []

    def test_a_bare_invocation_alone_is_caught(self) -> None:
        assert unguarded_uv_run_vectors(self.BARE)

    def test_a_guarded_invocation_does_not_vouch_for_a_bare_one(self) -> None:
        # The whole-file search this replaced passed on exactly this shape.
        source = f"{self.GUARDED}\n{self.BARE}\n"

        assert unguarded_uv_run_vectors(source) == [["uv", "run", "python", "y.py"]]

    def test_an_unrelated_literal_is_not_an_offender(self) -> None:
        # A constant that merely looks like an argv launches nothing.
        source = 'EXAMPLE = ["uv", "run", "python", "example.py"]'

        assert unguarded_uv_run_vectors(source) == []

    def test_a_bare_launch_is_caught_through_any_subprocess_helper(self) -> None:
        for call in ("subprocess.call", "subprocess.check_call", "subprocess.Popen"):
            source = f'import subprocess\n{call}(["uv", "run", "python", "y.py"])'

            assert unguarded_uv_run_vectors(source), call

    def test_an_unrelated_run_method_is_not_a_launch(self) -> None:
        # `runner.run(...)` is not a child process just because the method is
        # named `run`; the receiver has to resolve to subprocess.
        source = 'runner.run(["uv", "run", "python", "y.py"])'

        assert unguarded_uv_run_vectors(source) == []

    def test_a_module_alias_still_resolves(self) -> None:
        source = 'import subprocess as sp\nsp.run(["uv", "run", "python", "y.py"])'

        assert unguarded_uv_run_vectors(source)

    def test_a_direct_import_still_resolves(self) -> None:
        source = 'from subprocess import run\nrun(["uv", "run", "python", "y.py"])'

        assert unguarded_uv_run_vectors(source)

    def test_flags_after_the_command_do_not_count(self) -> None:
        source = (
            "import subprocess\n"
            'subprocess.run(["uv", "run", "python", "--frozen", "--no-sync"])'
        )

        assert unguarded_uv_run_vectors(source)
