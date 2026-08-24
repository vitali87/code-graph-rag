"""Editor link resolution: CGR_EDITOR, auto-detection, URL and diff templates."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.editor_links import (
    EditorTemplateError,
    diff_command,
    diff_link,
    editor_url,
    resolve_editor,
    url_template_problem,
)


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


class TestDiffLink:
    def test_pair_link_encodes_both_locations(self) -> None:
        url = diff_link(Path("/repo/a.py"), 5, Path("/repo/b.py"), 8)
        assert url == "diff://open?left=%2Frepo%2Fa.py%3A5&right=%2Frepo%2Fb.py%3A8"

    def test_spaces_are_encoded(self) -> None:
        url = diff_link(Path("/My Repo/a.py"), 1, Path("/My Repo/b.py"), 2)
        assert url is not None
        assert "%2FMy%20Repo%2Fa.py%3A1" in url

    def test_none_editor_disables_pair_links(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "CGR_EDITOR", cs.EDITOR_NONE)
        assert diff_link(Path("/r/a"), 1, Path("/r/b"), 2) is None


class TestBlankTemplates:
    """Whitespace-only settings read as unset, never as an empty command."""

    def test_blank_diff_command_falls_back_to_editor_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "CGR_DIFF_COMMAND", "   ")
        argv = diff_command(Path("/r/a.py"), Path("/r/b.py"))
        assert argv == ["code", "--diff", "/r/a.py", "/r/b.py"]

    def test_blank_url_template_falls_back_to_editor_scheme(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "CGR_EDITOR_URL_TEMPLATE", "  ")
        url = editor_url(Path("/r/a.py"), 3)
        assert url == "vscode://file//r/a.py:3"


class TestMalformedTemplates:
    """User template typos degrade or error cleanly, never traceback."""

    def test_unknown_url_placeholder_disables_links_with_problem(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "CGR_EDITOR_URL_TEMPLATE", "ed://{unknown}")
        assert editor_url(Path("/r/a.py"), 1) is None
        problem = url_template_problem()
        assert problem is not None
        assert "{path}" in problem and "{line}" in problem

    def test_unmatched_url_brace_disables_links_with_problem(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "CGR_EDITOR_URL_TEMPLATE", "ed://{path")
        assert editor_url(Path("/r/a.py"), 1) is None
        assert url_template_problem() is not None

    def test_valid_builtin_template_reports_no_problem(self) -> None:
        assert url_template_problem() is None

    def test_unknown_diff_placeholder_raises_actionable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "CGR_DIFF_COMMAND", "tool {oops}")
        with pytest.raises(EditorTemplateError, match=r"\{left\}"):
            diff_command(Path("/r/a"), Path("/r/b"))

    def test_unbalanced_diff_quote_raises_actionable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "CGR_DIFF_COMMAND", "tool '{left} {right}")
        with pytest.raises(EditorTemplateError, match=r"\{right\}"):
            diff_command(Path("/r/a"), Path("/r/b"))

    def test_attribute_placeholder_in_url_template_degrades(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # str.format attribute access ({path.foo}) raises AttributeError,
        # not KeyError; it must degrade like every other bad template.
        monkeypatch.setattr(settings, "CGR_EDITOR_URL_TEMPLATE", "ed://{path.foo}")
        assert editor_url(Path("/r/a.py"), 1) is None
        assert url_template_problem() is not None

    def test_index_placeholder_on_int_in_diff_raises_actionable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "CGR_DIFF_COMMAND", "tool {left.foo} {right}")
        with pytest.raises(EditorTemplateError, match=r"\{left\}"):
            diff_command(Path("/r/a"), Path("/r/b"))

    def test_command_splitting_to_nothing_raises_actionable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Unreachable via settings (blank reads as unset), so force a
        # degenerate built-in: the guard must refuse an empty argv rather
        # than let it reach Popen.
        monkeypatch.setitem(cs.EDITOR_DIFF_COMMANDS, "vscode", "   ")
        with pytest.raises(EditorTemplateError, match="empty"):
            diff_command(Path("/r/a"), Path("/r/b"))
