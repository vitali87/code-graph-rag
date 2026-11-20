"""The docs site configuration (`mkdocs.yml`) and what builds it."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, TypedDict

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


# The website (code-graph-rag.com) theme the docs share: light by default with
# the logo's blue as the accent and Geist type, plus a matching dark scheme
# behind the toggle. Keep the light tokens in step with the site.
WEBSITE_TOKENS = {
    "--cgr-bg": "#F6F7F9",
    "--cgr-surface": "#FFFFFF",
    "--cgr-ink": "#12151A",
    "--cgr-muted": "#4A5361",
    "--cgr-line": "#E3E6EB",
    "--cgr-brand": "#2F64B5",
    "--cgr-brand-dark": "#1F4A8C",
}
DARK_TOKENS = [
    "--cgr-dark-bg",
    "--cgr-dark-surface",
    "--cgr-dark-ink",
    "--cgr-dark-muted",
    "--cgr-dark-line",
    "--cgr-dark-brand",
]
WEBSITE_URL = "https://code-graph-rag.com"
EXTRA_CSS = REPO_ROOT / "docs" / "stylesheets" / "extra.css"


def _css() -> str:
    return EXTRA_CSS.read_text(encoding=cs.ENCODING_UTF8)


def _css_tokens() -> dict[str, str]:
    return {
        m.group(1): m.group(2).upper()
        for m in re.finditer(r"(--cgr-[a-z-]+):\s*(#[0-9a-fA-F]{6})\s*;", _css())
    }


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = (
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    )
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class _PaletteToggle(TypedDict):
    icon: str
    name: str


class _PaletteScheme(TypedDict):
    scheme: str
    primary: str
    accent: str
    toggle: _PaletteToggle


def _schemes() -> list[_PaletteScheme]:
    palette = _mkdocs_config()["theme"]["palette"]
    return palette if isinstance(palette, list) else [palette]


def test_the_docs_open_in_light_with_a_dark_toggle() -> None:
    schemes = _schemes()
    assert [p["scheme"] for p in schemes] == ["default", "slate"], schemes
    assert all("toggle" in p for p in schemes), schemes


def test_the_docs_never_open_in_dark_by_default() -> None:
    # Negative test. The first palette is the one a new visitor gets.
    assert _schemes()[0]["scheme"] != "slate"


def test_the_docs_use_the_websites_fonts() -> None:
    assert _mkdocs_config()["theme"]["font"] == {"text": "Geist", "code": "Geist Mono"}


def test_the_docs_stylesheet_uses_the_websites_colour_tokens() -> None:
    tokens = _css_tokens()
    for name, value in WEBSITE_TOKENS.items():
        assert tokens.get(name) == value, (name, tokens.get(name))
    for name in DARK_TOKENS:
        assert name in tokens, name


def test_both_schemes_keep_text_readable() -> None:
    # WCAG AA for body text: 4.5:1, in the light scheme and the dark one.
    t = _css_tokens()
    pairs = [
        ("--cgr-ink", "--cgr-bg"),
        ("--cgr-muted", "--cgr-bg"),
        ("--cgr-brand", "--cgr-surface"),
        ("--cgr-dark-ink", "--cgr-dark-bg"),
        ("--cgr-dark-muted", "--cgr-dark-bg"),
        ("--cgr-dark-brand", "--cgr-dark-surface"),
    ]
    for fg, bg in pairs:
        assert fg in t, fg
        assert bg in t, bg
        assert _contrast(t[fg], t[bg]) >= 4.5, (fg, bg)


def test_each_scheme_is_driven_by_its_own_tokens() -> None:
    css = _css()
    assert re.search(
        r'\[data-md-color-scheme="default"\]\s*\{[^}]*--md-default-bg-color:\s*var\(--cgr-bg\)',
        css,
    )
    assert re.search(
        r'\[data-md-color-scheme="slate"\]\s*\{[^}]*--md-default-bg-color:\s*var\(--cgr-dark-bg\)',
        css,
    )


def test_the_docs_stylesheet_keeps_none_of_the_old_palette() -> None:
    # Negative test. The old indigo and Inter would bring back a look the
    # website no longer uses.
    css = _css()
    assert "#6366f1" not in css.lower()
    assert "#818cf8" not in css.lower()
    assert "Inter" not in css


def test_the_docs_link_back_to_the_website() -> None:
    nav = _mkdocs_config()["nav"]
    targets = [v for item in nav if isinstance(item, dict) for v in item.values()]
    assert WEBSITE_URL in targets, targets
