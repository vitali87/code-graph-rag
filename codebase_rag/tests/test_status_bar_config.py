from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import main as main_mod


@pytest.fixture(autouse=True)
def reset_session(monkeypatch: pytest.MonkeyPatch):
    main_mod.app_context.session.confirm_edits = True
    main_mod.app_context.session.load_cgr_instructions = True
    main_mod.app_context.session.target_repo = None
    yield


@patch("codebase_rag.main.settings")
def test_config_segments_always_shows_both_models(
    mock_settings: MagicMock,
) -> None:
    mock_settings.active_orchestrator_config.model_id = "anthropic:claude-opus-4-7"
    mock_settings.active_cypher_config.model_id = "anthropic:claude-opus-4-7"
    main_mod.app_context.session.target_repo = Path("/tmp/myrepo")

    segments = dict(main_mod._config_segments())

    assert segments[cs.STATUS_BAR_CONFIG_LABEL_O] == "claude-opus-4-7"
    assert segments[cs.STATUS_BAR_CONFIG_LABEL_C] == "claude-opus-4-7"
    assert segments[cs.STATUS_BAR_CONFIG_LABEL_EDIT] == cs.STATUS_BAR_EDIT_ON
    assert segments[cs.STATUS_BAR_CONFIG_LABEL_INSTRUCTIONS] == cs.STATUS_BAR_EDIT_ON
    assert segments[cs.STATUS_BAR_CONFIG_LABEL_REPO] == "/tmp/myrepo"


@patch("codebase_rag.main.settings")
def test_config_segments_shows_distinct_models(
    mock_settings: MagicMock,
) -> None:
    mock_settings.active_orchestrator_config.model_id = "anthropic:claude-opus-4-7"
    mock_settings.active_cypher_config.model_id = "anthropic:claude-haiku-4-5"

    segments = dict(main_mod._config_segments())

    assert segments[cs.STATUS_BAR_CONFIG_LABEL_O] == "claude-opus-4-7"
    assert segments[cs.STATUS_BAR_CONFIG_LABEL_C] == "claude-haiku-4-5"


@patch("codebase_rag.main.settings")
def test_config_segments_reflects_session_flags(
    mock_settings: MagicMock,
) -> None:
    mock_settings.active_orchestrator_config.model_id = "anthropic:claude-opus-4-7"
    mock_settings.active_cypher_config.model_id = "anthropic:claude-opus-4-7"
    main_mod.app_context.session.confirm_edits = False
    main_mod.app_context.session.load_cgr_instructions = False

    segments = dict(main_mod._config_segments())

    assert segments[cs.STATUS_BAR_CONFIG_LABEL_EDIT] == cs.STATUS_BAR_EDIT_OFF
    assert segments[cs.STATUS_BAR_CONFIG_LABEL_INSTRUCTIONS] == cs.STATUS_BAR_EDIT_OFF


@patch("codebase_rag.main.settings")
def test_abbreviated_repo_uses_tilde_for_home_paths(
    mock_settings: MagicMock,
) -> None:
    inside_home = Path.home() / "Documents" / "platform"

    assert main_mod._abbreviated_repo(inside_home) == "~/Documents/platform"


def test_abbreviated_repo_keeps_absolute_for_outside_paths() -> None:
    assert main_mod._abbreviated_repo(Path("/etc/hosts")) == "/etc/hosts"


def test_abbreviated_repo_survives_unresolvable_home() -> None:
    # Path.home() raises RuntimeError when no home env vars are set (Windows
    # without USERPROFILE); the status bar must fall back to the absolute path.
    with patch.object(main_mod.Path, "home", side_effect=RuntimeError):
        assert main_mod._abbreviated_repo(Path("/etc/hosts")) == "/etc/hosts"


def test_abbreviated_repo_handles_none() -> None:
    assert main_mod._abbreviated_repo(None) == ""


@patch("codebase_rag.main.settings")
def test_config_status_html_includes_model_and_repo(
    mock_settings: MagicMock,
) -> None:
    mock_settings.active_orchestrator_config.model_id = "anthropic:claude-opus-4-7"
    mock_settings.active_cypher_config.model_id = "anthropic:claude-opus-4-7"
    main_mod.app_context.session.target_repo = Path("/tmp/showme")

    html = main_mod._config_status_html()

    assert "claude-opus-4-7" in html
    assert "/tmp/showme" in html
    assert cs.STATUS_BAR_CONFIG_LABEL_O in html
    assert cs.STATUS_BAR_CONFIG_LABEL_REPO in html


@patch("codebase_rag.main._git_state", return_value=None)
@patch("codebase_rag.main._terminal_columns", return_value=200)
@patch("codebase_rag.main.settings")
def test_status_bar_html_inlines_config_when_wide(
    mock_settings: MagicMock,
    _columns: MagicMock,
    _git: MagicMock,
) -> None:
    mock_settings.active_orchestrator_config.model_id = "anthropic:claude-opus-4-7"
    mock_settings.active_cypher_config.model_id = "anthropic:claude-opus-4-7"
    main_mod.app_context.session.target_repo = Path("/tmp/x")

    html = main_mod._status_bar_label()

    rendered = str(html.value) if hasattr(html, "value") else str(html)
    body_marker = main_mod._permission_mode_label()
    body_idx = rendered.index(body_marker)
    config_idx = rendered.index(cs.STATUS_BAR_CONFIG_LABEL_O + ":")
    assert config_idx > body_idx, "config should appear after body when wide"


@patch("codebase_rag.main._git_state", return_value=None)
@patch("codebase_rag.main._terminal_columns", return_value=40)
@patch("codebase_rag.main.settings")
def test_status_bar_html_wraps_config_when_narrow(
    mock_settings: MagicMock,
    _columns: MagicMock,
    _git: MagicMock,
) -> None:
    mock_settings.active_orchestrator_config.model_id = "anthropic:claude-opus-4-7"
    mock_settings.active_cypher_config.model_id = "anthropic:claude-opus-4-7"
    main_mod.app_context.session.target_repo = Path("/tmp/x")

    html = main_mod._status_bar_label()

    rendered = str(html.value) if hasattr(html, "value") else str(html)
    body_marker = main_mod._permission_mode_label()
    body_idx = rendered.index(body_marker)
    config_idx = rendered.index(cs.STATUS_BAR_CONFIG_LABEL_O + ":")
    assert config_idx < body_idx, "config should appear above body when narrow"


@patch("codebase_rag.main._git_state", return_value=None)
@patch("codebase_rag.main._terminal_columns", return_value=200)
@patch("codebase_rag.main.settings")
def test_rich_status_bar_inlines_config_when_wide(
    mock_settings: MagicMock,
    _columns: MagicMock,
    _git: MagicMock,
) -> None:
    mock_settings.active_orchestrator_config.model_id = "anthropic:claude-opus-4-7"
    mock_settings.active_cypher_config.model_id = "anthropic:claude-opus-4-7"
    main_mod.app_context.session.target_repo = Path("/tmp/x")

    rendered = main_mod._rich_status_bar().plain
    assert "\n" not in rendered
    assert cs.STATUS_BAR_CONFIG_LABEL_O + ":" in rendered


@patch("codebase_rag.main._git_state", return_value=None)
@patch("codebase_rag.main._terminal_columns", return_value=30)
@patch("codebase_rag.main.settings")
def test_rich_status_bar_wraps_config_when_narrow(
    mock_settings: MagicMock,
    _columns: MagicMock,
    _git: MagicMock,
) -> None:
    mock_settings.active_orchestrator_config.model_id = "anthropic:claude-opus-4-7"
    mock_settings.active_cypher_config.model_id = "anthropic:claude-opus-4-7"
    main_mod.app_context.session.target_repo = Path("/tmp/x")

    rendered = main_mod._rich_status_bar().plain
    assert "\n" in rendered


def test_git_state_returns_none_without_target_repo() -> None:
    main_mod.app_context.session.target_repo = None
    assert main_mod._git_state() is None


def test_git_state_uses_target_repo_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target-repo"
    target.mkdir()
    main_mod.app_context.session.target_repo = target

    captured: dict[str, object] = {}

    class _FakeCompleted:
        stdout = "## feature/x\n M something.py\n"

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        return _FakeCompleted()

    monkeypatch.setattr(main_mod.subprocess, "run", fake_run)

    result = main_mod._git_state()
    assert result is not None
    branch, is_dirty = result

    assert captured["cwd"] == target
    assert branch == "feature/x"
    assert is_dirty is True


def test_git_state_returns_none_when_target_missing(tmp_path: Path) -> None:
    main_mod.app_context.session.target_repo = tmp_path / "does-not-exist"
    assert main_mod._git_state() is None


@patch("codebase_rag.main._git_state", return_value=("feature/x", True))
@patch("codebase_rag.main._terminal_columns", return_value=400)
@patch("codebase_rag.main.settings")
def test_branch_appears_after_repo_when_inline(
    mock_settings: MagicMock,
    _columns: MagicMock,
    _git: MagicMock,
) -> None:
    mock_settings.active_orchestrator_config.model_id = "anthropic:claude-opus-4-7"
    mock_settings.active_cypher_config.model_id = "anthropic:claude-opus-4-7"
    main_mod.app_context.session.target_repo = Path("/tmp/target")

    rendered = main_mod._rich_status_bar().plain

    repo_label = f"{cs.STATUS_BAR_CONFIG_LABEL_REPO}:/tmp/target"
    assert repo_label in rendered
    assert "feature/x" in rendered
    assert rendered.index(repo_label) < rendered.index("feature/x")
    mode_label = main_mod._permission_mode_label()
    assert rendered.index(mode_label) < rendered.index("feature/x")


@patch("codebase_rag.main._git_state", return_value=("feature/x", False))
@patch("codebase_rag.main._terminal_columns", return_value=400)
@patch("codebase_rag.main.settings")
def test_status_bar_html_places_branch_after_repo_when_inline(
    mock_settings: MagicMock,
    _columns: MagicMock,
    _git: MagicMock,
) -> None:
    mock_settings.active_orchestrator_config.model_id = "anthropic:claude-opus-4-7"
    mock_settings.active_cypher_config.model_id = "anthropic:claude-opus-4-7"
    main_mod.app_context.session.target_repo = Path("/tmp/target")

    html = main_mod._status_bar_label()
    rendered = str(html.value) if hasattr(html, "value") else str(html)

    repo_idx = rendered.index(f"{cs.STATUS_BAR_CONFIG_LABEL_REPO}:")
    branch_idx = rendered.index("feature/x")
    assert repo_idx < branch_idx
