from __future__ import annotations

import tomllib
from fnmatch import fnmatch
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_HIGHLIGHTS = _ROOT / "codebase_rag" / "queries" / "highlights"


def _package_data_patterns() -> list[str]:
    config = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return config["tool"]["setuptools"]["package-data"]["codebase_rag"]


@pytest.mark.parametrize(
    "query_file", sorted(_HIGHLIGHTS.glob("*.scm")), ids=lambda path: path.name
)
def test_the_fallback_highlight_query_ships_in_the_wheel(query_file: Path) -> None:
    # parser_loader reads these at runtime whenever a grammar package has no
    # HIGHLIGHTS_QUERY of its own, so a wheel without them silently loses
    # highlight captures for those languages.
    relative = query_file.relative_to(_ROOT / "codebase_rag").as_posix()
    assert any(fnmatch(relative, pattern) for pattern in _package_data_patterns())
