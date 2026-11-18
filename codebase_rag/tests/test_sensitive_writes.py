"""MCP changes no file that looks like a credential store.

MCP `read_file` already refused them (issue #3457), but `write_file` could
overwrite one, and `surgical_replace_code` answered a guess at its contents:
"target not found" for a wrong one, success for a right one, the line of
every match for an ambiguous one. That let a client read a secret one
character at a time without ever reading the file.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import config as cgr_config
from codebase_rag.mcp.tools import MCPToolsRegistry

SECRET = "sk-live-Q7b2"
ENV_TEXT = f"KEY={SECRET}\nOTHER=a-b-c\n"
WAIVER = "CGR_ALLOW_SENSITIVE_WRITES"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / ".env").write_text(ENV_TEXT, encoding="utf-8")
    (tmp_path / ".env.example").write_text("KEY=\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def registry(root: Path) -> MCPToolsRegistry:
    return MCPToolsRegistry(
        project_root=str(root), ingestor=MagicMock(), cypher_gen=MagicMock()
    )


def _env(root: Path) -> str:
    return (root / ".env").read_text(encoding="utf-8")


class TestWriteFile:
    @pytest.mark.parametrize(
        "relative", [".env", ".env.production", "id_rsa", ".aws/credentials"]
    )
    def test_a_credential_file_is_not_written(
        self, relative: str, registry: MCPToolsRegistry, root: Path
    ) -> None:
        before = (root / relative).read_bytes() if (root / relative).exists() else None

        result = asyncio.run(registry.write_file(relative, "KEY=attacker\n"))

        assert WAIVER in result
        after = (root / relative).read_bytes() if (root / relative).exists() else None
        assert after == before

    def test_a_template_is_written(
        self, registry: MCPToolsRegistry, root: Path
    ) -> None:
        result = asyncio.run(registry.write_file(".env.example", "KEY=\nNEW=\n"))

        assert WAIVER not in result
        assert (root / ".env.example").read_text(encoding="utf-8") == "KEY=\nNEW=\n"

    @pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
    def test_a_link_to_one_is_not_written(
        self, registry: MCPToolsRegistry, root: Path
    ) -> None:
        (root / "settings.txt").symlink_to(root / ".env")

        result = asyncio.run(registry.write_file("settings.txt", "KEY=attacker\n"))

        assert WAIVER in result
        assert _env(root) == ENV_TEXT

    def test_the_waiver_allows_it(
        self,
        registry: MCPToolsRegistry,
        root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(cgr_config.settings, "ALLOW_SENSITIVE_WRITES", True)

        result = asyncio.run(registry.write_file(".env", "KEY=new\n"))

        assert WAIVER not in result
        assert _env(root) == "KEY=new\n"


class TestSurgicalReplace:
    @pytest.mark.parametrize(
        "guess",
        [
            SECRET,  # right: would have succeeded
            "sk-live-A",  # wrong: would have said "not found"
            "-",  # several matches: would have listed their lines
        ],
    )
    def test_every_guess_gets_the_same_answer(
        self, guess: str, registry: MCPToolsRegistry, root: Path
    ) -> None:
        result = asyncio.run(registry.surgical_replace_code(".env", guess, guess))

        assert result == asyncio.run(
            registry.surgical_replace_code(".env", "unrelated", "unrelated")
        )
        assert WAIVER in result
        assert _env(root) == ENV_TEXT

    def test_the_waiver_allows_it(
        self,
        registry: MCPToolsRegistry,
        root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(cgr_config.settings, "ALLOW_SENSITIVE_WRITES", True)

        result = asyncio.run(
            registry.surgical_replace_code(".env", "OTHER=a-b-c", "OTHER=d")
        )

        assert WAIVER not in result
        assert _env(root) == f"KEY={SECRET}\nOTHER=d\n"
