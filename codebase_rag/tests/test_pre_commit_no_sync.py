"""Regression tests for the local pre-commit hooks (issue #1097).

A bare ``uv run`` re-syncs the environment to the default dependency set and
re-resolves the lockfile before executing, so a commit silently uninstalls every
extra from the developer's virtualenv and rewrites ``uv.lock`` underneath the
commit. The local hooks must therefore run with ``--no-sync`` and ``--frozen``.
"""

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


def test_hook_scripts_do_not_shell_out_to_a_bare_uv_run() -> None:
    hook_scripts = sorted((REPO_ROOT / "scripts" / "hooks").glob("*.py"))
    assert hook_scripts, "expected hook scripts under scripts/hooks/"

    offenders = []
    for script in hook_scripts:
        source = script.read_text(encoding="utf-8")
        if '"uv", "run"' not in source:
            continue
        for required in REQUIRED_UV_RUN_FLAGS:
            if (
                f'"uv", "run", "{required}"' not in source
                and f'"{required}"' not in source
            ):
                offenders.append((script.name, required))

    assert offenders == [], (
        f"hook scripts shell out to `uv run` without required flags: {offenders}. "
        "A nested bare `uv run` re-syncs the venv and rewrites uv.lock even when "
        "the pre-commit entry itself is guarded"
    )
