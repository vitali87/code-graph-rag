"""The docs Credits page and the `--version` credits line (issue #2175)."""

from __future__ import annotations

import importlib.util
import re
import sys
from email.message import Message
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from codebase_rag import constants as cs
from codebase_rag.cli import credits_lines

REPO_ROOT = Path(__file__).resolve().parents[2]
PAGE_SCRIPT = REPO_ROOT / "scripts" / "generate_credits_page.py"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def page_module() -> ModuleType:
    return _load(PAGE_SCRIPT, "generate_credits_page")


@pytest.fixture(scope="module")
def page(page_module: ModuleType) -> str:
    return page_module.build_page()


class _FakeDist:
    def __init__(self, name: str, **fields: list[str] | str) -> None:
        self.metadata = Message()
        self.metadata["Name"] = name
        for key, value in fields.items():
            for item in [value] if isinstance(value, str) else value:
                self.metadata[key.replace("_", "-")] = item


def test_the_page_credits_exactly_the_notices_components(
    page_module: ModuleType, page: str
) -> None:
    """Same list as the binary's notices file, so the two cannot drift."""
    notices = page_module._load_notices()
    expected = {
        n.name for n in notices.collect_notices(notices.runtime_closure().values())
    }
    listed = set(re.findall(r"^## (.+)$", page, flags=re.MULTILINE))
    assert expected, "the runtime closure is empty, so the comparison proves nothing"
    assert listed == expected


def test_every_entry_links_its_project(page: str) -> None:
    entries = re.split(r"^## ", page, flags=re.MULTILINE)[1:]
    unlinked = [
        entry.splitlines()[0]
        for entry in entries
        if not re.search(r"\[Project page\]\(https?://[^)]+\)", entry)
    ]
    assert not unlinked, unlinked


def test_licence_text_is_escaped_inside_its_block(page_module: ModuleType) -> None:
    class _Notice:
        name, version, texts = "x", "1", ("<b>&</b>",)

        @staticmethod
        def license_line() -> str:
            return "MIT"

    text = page_module.render([(_Notice(), "https://x.example")])
    assert "<pre>&lt;b&gt;&amp;&lt;/b&gt;</pre>" in text


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            {
                "Project_URL": [
                    "Changelog, https://c.example",
                    "Source, https://s.example",
                    "Homepage, https://h.example",
                ]
            },
            "https://h.example",
        ),
        ({"Project_URL": ["Issues, https://i.example"]}, "https://i.example"),
        ({"Home_page": "https://home.example"}, "https://home.example"),
        ({"Home_page": "UNKNOWN"}, "https://pypi.org/project/some-pkg/"),
        ({}, "https://pypi.org/project/some-pkg/"),
    ],
    ids=["homepage-label", "any-project-url", "home-page", "unknown", "pypi"],
)
def test_the_project_link_falls_back_in_order(
    page_module: ModuleType, fields: dict[str, list[str] | str], expected: str
) -> None:
    assert page_module.project_url(_FakeDist("Some_Pkg", **fields)) == expected


class _MkdocsLoader(yaml.SafeLoader):
    """mkdocs.yml names Python objects (`!!python/name:...`); they are not
    what this test reads, so they load as None."""


_MkdocsLoader.add_multi_constructor(
    "tag:yaml.org,2002:python/", lambda _loader, _suffix, _node: None
)


def test_the_page_is_in_the_docs_nav_with_its_hook() -> None:
    config = yaml.load(  # noqa: S506 - SafeLoader subclass; python tags load as None
        (REPO_ROOT / "mkdocs.yml").read_text(encoding=cs.ENCODING_UTF8),
        Loader=_MkdocsLoader,
    )
    assert {"Credits": "credits.md"} in config["nav"]
    assert "scripts/mkdocs_credits_hook.py" in config["hooks"]


def test_the_readme_links_the_page() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding=cs.ENCODING_UTF8)
    assert cs.CREDITS_URL in readme


def test_version_names_the_credits_page() -> None:
    assert credits_lines(frozen=False) == [
        cs.CLI_MSG_CREDITS.format(url=cs.CREDITS_URL)
    ]


@pytest.mark.parametrize(
    "executable",
    ["/opt/code-graph-rag-linux-x86_64", r"C:\bin\code-graph-rag-windows-x86_64.exe"],
)
def test_a_release_binary_also_names_its_notices_file(executable: str) -> None:
    stem = re.split(r"[\\/]", executable)[-1].removesuffix(".exe")
    lines = credits_lines(frozen=True, executable=executable.replace("\\", "/"))
    assert lines[1] == cs.CLI_MSG_CREDITS_NOTICES.format(
        name=f"{stem}{cs.THIRD_PARTY_NOTICES_SUFFIX}"
    )
