"""Editor link resolution: CGR_EDITOR, auto-detection, URL and diff templates."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.editor_links import diff_command, editor_url, resolve_editor


@pytest.fixture(autouse=True)
def _neutral_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(cs.ENV_CF_BUNDLE_ID, raising=False)
    monkeypatch.setattr(settings, "CGR_EDITOR", cs.EDITOR_AUTO)
    monkeypatch.setattr(settings, "CGR_EDITOR_URL_TEMPLATE", None)
    monkeypatch.setattr(settings, "CGR_DIFF_COMMAND", None)


class TestResolveEditor:
    def test_explicit_setting_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "CGR_EDITOR", "zed")
        monkeypatch.setenv(cs.ENV_CF_BUNDLE_ID, "com.todesktop.cursor")
        assert resolve_editor() == "zed"

    def test_auto_detects_host_bundle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(cs.ENV_CF_BUNDLE_ID, "com.todesktop.Cursor")
        assert resolve_editor() == "cursor"

    def test_auto_falls_back_to_vscode(self) -> None:
        assert resolve_editor() == cs.EDITOR_VSCODE


class TestEditorUrl:
    def test_default_vscode_url_carries_path_and_line(self) -> None:
        url = editor_url(Path("/repo/pkg/mod.py"), 42)
        assert url == "vscode://file//repo/pkg/mod.py:42"

    def test_spaces_are_percent_encoded(self) -> None:
        url = editor_url(Path("/My Repo/mod.py"), 7)
        assert url == "vscode://file//My%20Repo/mod.py:7"

    def test_none_editor_disables_links(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "CGR_EDITOR", cs.EDITOR_NONE)
        assert editor_url(Path("/repo/mod.py"), 1) is None

    def test_template_overrides_editor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "CGR_EDITOR_URL_TEMPLATE", "myed://{path}#{line}")
        monkeypatch.setattr(settings, "CGR_EDITOR", cs.EDITOR_NONE)
        assert editor_url(Path("/repo/mod.py"), 3) == "myed:///repo/mod.py#3"


class TestDiffCommand:
    def test_default_vscode_diff(self) -> None:
        argv = diff_command(Path("/r/a.py"), Path("/r/b.py"))
        assert argv == ["code", "--diff", "/r/a.py", "/r/b.py"]

    def test_paths_with_spaces_stay_single_arguments(self) -> None:
        argv = diff_command(Path("/My Repo/a.py"), Path("/My Repo/b.py"))
        assert argv == ["code", "--diff", "/My Repo/a.py", "/My Repo/b.py"]

    def test_custom_command_template(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "CGR_DIFF_COMMAND", "meld {left} {right}")
        assert diff_command(Path("/r/a"), Path("/r/b")) == ["meld", "/r/a", "/r/b"]

    def test_unknown_editor_has_no_diff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "CGR_EDITOR", "textmate")
        assert diff_command(Path("/r/a"), Path("/r/b")) is None
