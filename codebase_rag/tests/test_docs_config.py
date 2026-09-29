"""The docs site configuration (`mkdocs.yml`) and what builds it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from codebase_rag import constants as cs

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docs.yml"
CREDITS_PAGE = "docs/credits.md"

# Settings Zensical documents as unsupported. It skips them without a warning,
# even under --strict, so each one is a feature that silently stops working.
_ZENSICAL_IGNORED_SETTINGS = frozenset(
    {
        "hooks",
        "not_in_nav",
        "exclude_docs",
        "draft_docs",
        "remote_branch",
        "remote_name",
    }
)


class _MkdocsLoader(yaml.SafeLoader):
    """mkdocs.yml names Python objects (`!!python/name:...`); they are not
    what these tests read, so they load as None."""


_MkdocsLoader.add_multi_constructor(
    "tag:yaml.org,2002:python/", lambda _loader, _suffix, _node: None
)


def _mkdocs_config() -> dict[str, Any]:
    return yaml.load(  # noqa: S506 - SafeLoader subclass; python tags load as None
        (REPO_ROOT / "mkdocs.yml").read_text(encoding=cs.ENCODING_UTF8),
        Loader=_MkdocsLoader,
    )


def _plugin_names(config: dict[str, Any]) -> list[str]:
    return [
        plugin if isinstance(plugin, str) else next(iter(plugin))
        for plugin in config.get("plugins", [])
    ]


def _docs_build_step_commands() -> list[str]:
    workflow = yaml.safe_load(DOCS_WORKFLOW.read_text(encoding=cs.ENCODING_UTF8))
    return [step.get("run", "") for step in workflow["jobs"]["build"]["steps"]]


def test_the_credits_page_is_in_the_docs_nav() -> None:
    assert {"Credits": "credits.md"} in _mkdocs_config()["nav"]


def test_the_docs_config_has_no_setting_zensical_ignores() -> None:
    assert not _ZENSICAL_IGNORED_SETTINGS & _mkdocs_config().keys()


def test_the_docs_workflow_writes_the_credits_page_before_building() -> None:
    # A hook used to add the page mid-build. Zensical never runs hooks, so the
    # page would vanish from the site with the build still reporting success.
    commands = _docs_build_step_commands()
    generate = [
        i
        for i, command in enumerate(commands)
        if f"scripts/generate_credits_page.py --output {CREDITS_PAGE}" in command
    ]
    build = [
        i for i, command in enumerate(commands) if "zensical build --strict" in command
    ]
    assert generate, commands
    assert build, commands
    assert generate[0] < build[0], commands


def test_the_generated_credits_page_is_never_committed() -> None:
    # Negative test. A committed copy would be served in place of the fresh one
    # and fall behind the dependencies, the drift the generator exists to stop.
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding=cs.ENCODING_UTF8)
    assert CREDITS_PAGE in ignored.splitlines()


def test_the_docs_use_only_the_builtin_search_plugin() -> None:
    # minify's three dependencies are abandoned (csscompressor, jsmin and
    # htmlmin2 last released in 2017, 2022 and 2023), and Zensical, the MkDocs
    # successor, accepts the plugin name but silently skips it.
    assert _plugin_names(_mkdocs_config()) == ["search"]
