"""Issue #3457: no approval-free read hands a credential file to the model.

`read_file`, confined shell reads and MCP `read_file` all ran without
approval, and whatever they returned went to the model provider. An
instruction planted in the repository could ask for `.env` as easily as for
source.
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic_ai import Agent, ApprovalRequired, DeferredToolRequests
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from codebase_rag import config as cgr_config
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.sensitive_paths import is_sensitive_path
from codebase_rag.services.llm import run_to_text_answer
from codebase_rag.tools.file_reader import FileReader, create_file_reader_tool
from codebase_rag.tools.shell_command import _is_confined_read, _noninteractive_denial

SECRET = "sk-live-0123456789abcdef0123456789abcdef"
DENIAL = "denied: nobody to approve"


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "app/.env.local",
        ".env.production",
        "deploy/prod.env",
        "keys/id_rsa",
        "keys/id_ed25519",
        "certs/server.pem",
        "certs/server.key",
        "store.p12",
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".git-credentials",
        ".git/config",
        "home/.aws/credentials",
        "home/.docker/config.json",
        "home/.kube/config",
        "home/.ssh/known_hosts",
        "infra/terraform.tfstate",
        "infra/terraform.tfstate.backup",
        "CERTS/SERVER.PEM",
    ],
)
def test_credential_stores_are_sensitive(path: str) -> None:
    assert is_sensitive_path(Path(path))


@pytest.mark.parametrize(
    "path",
    [
        ".env.example",
        ".env.sample",
        ".env.template",
        "keys/id_rsa.pub",
        "src/config",
        "src/config.py",
        "settings.py",
        "key.py",
        "README.md",
        "docs/environment.md",
        "credentials",
    ],
)
def test_ordinary_files_are_not_sensitive(path: str) -> None:
    assert not is_sensitive_path(Path(path))


def test_a_symlink_is_judged_by_its_target(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"KEY={SECRET}\n", encoding="utf-8")
    os.symlink(tmp_path / ".env", tmp_path / "notes.txt")

    assert is_sensitive_path(tmp_path / "notes.txt")


def _ctx(approved: bool) -> MagicMock:
    ctx = MagicMock()
    ctx.tool_call_approved = approved
    return ctx


class TestReadFileTool:
    async def test_a_credential_file_waits_for_approval(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text(f"KEY={SECRET}\n", encoding="utf-8")
        tool = create_file_reader_tool(FileReader(str(tmp_path)))
        ctx = _ctx(approved=False)

        with pytest.raises(ApprovalRequired):
            await tool.function(ctx, file_path=".env")

    async def test_an_approved_read_returns_the_file(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text(f"KEY={SECRET}\n", encoding="utf-8")
        tool = create_file_reader_tool(FileReader(str(tmp_path)))

        assert SECRET in await tool.function(_ctx(approved=True), file_path=".env")

    async def test_source_needs_no_approval(self, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
        tool = create_file_reader_tool(FileReader(str(tmp_path)))

        assert "print" in await tool.function(_ctx(approved=False), file_path="app.py")


def _reading_agent(root: Path) -> Agent[None, str | DeferredToolRequests]:
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [
            str(part.content)
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        if returns:
            return ModelResponse(parts=[TextPart(f"Tool said: {returns[-1]}")])
        return ModelResponse(
            parts=[ToolCallPart("read_file", {"file_path": ".env"}, tool_call_id="c1")]
        )

    return Agent[None, str | DeferredToolRequests](
        FunctionModel(model),
        tools=[create_file_reader_tool(FileReader(str(root)))],
        output_type=[str, DeferredToolRequests],
    )


class TestRunsWithNobodyToAsk:
    """`cgr ask` and MCP `ask_agent` settle approvals without an operator."""

    async def test_a_denied_run_never_sees_the_secret(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text(f"KEY={SECRET}\n", encoding="utf-8")

        answer = await run_to_text_answer(
            _reading_agent(tmp_path), "show .env", approve=False, denial=DENIAL
        )

        assert SECRET not in answer
        assert DENIAL in answer

    async def test_an_approving_run_reads_it(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text(f"KEY={SECRET}\n", encoding="utf-8")

        answer = await run_to_text_answer(
            _reading_agent(tmp_path), "show .env", approve=True, denial=DENIAL
        )

        assert SECRET in answer


@pytest.mark.parametrize(
    "command",
    [
        "cat .env",
        "head -n 3 .env.local",
        "tail -- keys/id_rsa",
        "sort certs/server.pem",
        "rg KEY .env",
        "rg -i -e KEY .git/config",
        "rg -f .env notes.txt",
        "rg --hidden KEY",
        "rg -uu KEY",
        "rg -. KEY",
        "rg --unrestricted KEY",
        "cat README.md | rg -n KEY .npmrc",
    ],
)
def test_a_shell_read_of_a_credential_file_is_not_confined(
    command: str, tmp_path: Path
) -> None:
    assert _noninteractive_denial(command, tmp_path) is not None
    assert not _is_confined_read(command, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "cat README.md",
        "head -n 5 .env.example",
        "rg .env src",
        "rg -e KEY src",
        "ls .env",
        "find . -name .env",
        "wc -l .env",
    ],
)
def test_reads_that_print_no_credential_file_stay_confined(
    command: str, tmp_path: Path
) -> None:
    assert _noninteractive_denial(command, tmp_path) is None
    assert _is_confined_read(command, tmp_path)


@pytest.fixture
def registry(tmp_path: Path) -> MCPToolsRegistry:
    (tmp_path / ".env").write_text(f"KEY={SECRET}\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    return MCPToolsRegistry(
        project_root=str(tmp_path), ingestor=MagicMock(), cypher_gen=MagicMock()
    )


class TestMcpReadFile:
    def test_a_credential_file_is_refused(self, registry: MCPToolsRegistry) -> None:
        for kwargs in ({}, {"offset": 0, "limit": 5}):
            result = asyncio.run(registry.read_file(".env", **kwargs))

            assert SECRET not in result
            assert "CGR_ALLOW_SENSITIVE_READS" in result

    def test_the_setting_serves_it(
        self, registry: MCPToolsRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cgr_config.settings, "ALLOW_SENSITIVE_READS", True)

        assert SECRET in asyncio.run(registry.read_file(".env"))

    def test_source_is_served(self, registry: MCPToolsRegistry) -> None:
        assert "print" in asyncio.run(registry.read_file("app.py"))


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
class TestRecursiveRipgrep:
    """A ripgrep given no path, or a directory, searches every file below it
    that is neither hidden nor ignored (Codex review on PR #3471)."""

    @pytest.fixture
    def tree(self, tmp_path: Path) -> Path:
        (tmp_path / "certs").mkdir()
        (tmp_path / "certs" / "server.pem").write_text(f"KEY={SECRET}\n")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("print('hi')\n")
        return tmp_path

    @pytest.mark.parametrize(
        "command",
        [
            "rg KEY",
            "rg KEY certs",
            "rg -n '' certs/",
            "rg -i KEY .",
            "rg -g='*.pem' KEY",
            "rg -ig='*.pem' KEY",
            "rg --glob='*.pem' KEY",
        ],
    )
    def test_a_search_that_reaches_a_credential_file_is_not_confined(
        self, tree: Path, command: str
    ) -> None:
        assert _noninteractive_denial(command, tree) is not None

    @pytest.mark.parametrize(
        "command",
        [
            "rg KEY src",
            "rg -g '*.py' KEY",
            "rg -t py KEY",
            "rg --files",
            "rg -g '=*.pem' KEY",
            "rg --no-heading -n KEY src",
        ],
    )
    def test_a_search_that_leaves_it_out_stays_confined(
        self, tree: Path, command: str
    ) -> None:
        assert _noninteractive_denial(command, tree) is None

    def test_an_ignored_credential_file_is_not_searched(self, tree: Path) -> None:
        (tree / ".ignore").write_text("*.pem\n")

        assert _noninteractive_denial("rg KEY", tree) is None

    @pytest.mark.parametrize(
        "command",
        [
            "rg --no-ignore KEY .",
            "rg --no-ignore-dot KEY",
            "rg --no-ignore-vcs KEY",
            "rg --no-ignore-files KEY src",
        ],
    )
    def test_a_switch_that_unignores_files_is_not_confined(
        self, tree: Path, command: str
    ) -> None:
        """The file list cannot vouch for files an ignore rule hid from it
        (CodeRabbit review on PR #3471)."""
        (tree / ".ignore").write_text("*.pem\n")

        assert _noninteractive_denial(command, tree) is not None

    @pytest.mark.parametrize(
        ("config", "command"),
        [
            ("--glob=!*.pem", "rg --no-config KEY"),
            (
                "--glob-case-insensitive\n--glob=!*.PEM",
                "rg --no-glob-case-insensitive KEY",
            ),
        ],
    )
    def test_a_switch_that_undoes_the_ripgrep_config_reaches_the_file_list(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch, config: str, command: str
    ) -> None:
        """The file list reads the same ripgrep config as the search, so a
        switch that overrides the config must reach it too (CodeRabbit review
        on PR #3471)."""
        (tree / "ripgreprc").write_text(f"{config}\n")
        monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(tree / "ripgreprc"))

        assert _noninteractive_denial("rg KEY", tree) is None
        assert _noninteractive_denial(command, tree) is not None

    def test_without_ripgrep_nothing_is_read(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_which = shutil.which
        monkeypatch.setattr(
            shutil,
            "which",
            lambda name, *a, **k: None if name == "rg" else real_which(name, *a, **k),
        )

        assert _noninteractive_denial("rg KEY", tree) is None


def test_a_credential_directory_named_itself_is_sensitive() -> None:
    assert is_sensitive_path(Path(".ssh"))
    assert is_sensitive_path(Path("home/.gnupg"))


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_a_search_of_a_named_hidden_credential_directory_is_not_confined(
    tmp_path: Path,
) -> None:
    """ripgrep searches a hidden directory it is given by name (Greptile
    review on PR #3471)."""
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_rsa").write_text(f"{SECRET}\n")

    assert _noninteractive_denial("rg -e . .ssh", tmp_path) is not None


def test_the_approval_prompt_shows_the_path_as_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model chooses the path, so markup in it must not hide part of what
    the operator approves (CodeRabbit review on PR #3471)."""
    from rich.console import Console

    from codebase_rag import main
    from codebase_rag.types_defs import ConfirmationToolNames

    buffer = io.StringIO()
    monkeypatch.setattr(
        main.app_context,
        "console",
        Console(file=buffer, force_terminal=False, no_color=True, width=200),
    )
    names = ConfirmationToolNames("replace_code", "create_file", "shell", "sg")
    path = "notes[/][conceal]/../.env"

    main._display_tool_call_diff("read_file", {"file_path": path}, names)

    assert path in buffer.getvalue()
