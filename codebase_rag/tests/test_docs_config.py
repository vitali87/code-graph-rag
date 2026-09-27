"""The docs site configuration (`mkdocs.yml`) and what builds it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from codebase_rag import constants as cs

REPO_ROOT = Path(__file__).resolve().parents[2]


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


def test_the_credits_page_is_in_the_docs_nav_with_its_hook() -> None:
    config = _mkdocs_config()
    assert {"Credits": "credits.md"} in config["nav"]
    assert "scripts/mkdocs_credits_hook.py" in config["hooks"]


def test_the_docs_use_only_the_builtin_search_plugin() -> None:
    # minify's three dependencies are abandoned (csscompressor, jsmin and
    # htmlmin2 last released in 2017, 2022 and 2023), and Zensical, the MkDocs
    # successor, accepts the plugin name but silently skips it.
    assert _plugin_names(_mkdocs_config()) == ["search"]
