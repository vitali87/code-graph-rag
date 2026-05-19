from __future__ import annotations

from codebase_rag.main import _autowrap_diff_blocks


class TestNoDiff:
    def test_plain_text_unchanged(self) -> None:
        text = "Here is some explanation without any diff."
        assert _autowrap_diff_blocks(text) == text

    def test_text_without_diff_marker_unchanged(self) -> None:
        text = "Lines starting with - or + but no diff --git header\n- not a diff\n+ also not"
        assert _autowrap_diff_blocks(text) == text


class TestWrappingUnfencedDiff:
    def test_full_git_diff_gets_fenced_as_diff(self) -> None:
        text = (
            "diff --git a/file.py b/file.py\n"
            "index abc..def 100644\n"
            "--- a/file.py\n"
            "+++ b/file.py\n"
            "@@ -1,3 +1,3 @@\n"
            " context\n"
            "-old\n"
            "+new\n"
        )
        out = _autowrap_diff_blocks(text)
        assert out.startswith("```diff\n")
        assert out.rstrip().endswith("```")
        assert "diff --git a/file.py b/file.py" in out
        assert "+new" in out

    def test_diff_followed_by_explanation_text(self) -> None:
        text = (
            "diff --git a/x b/x\n"
            "--- a/x\n"
            "+++ b/x\n"
            "@@ -1 +1 @@\n"
            "-a\n"
            "+b\n"
            "\n"
            "This adds the new feature.\n"
        )
        out = _autowrap_diff_blocks(text)
        assert "```diff\n" in out
        explanation_pos = out.index("This adds the new feature.")
        fence_close_pos = out.rindex("```", 0, explanation_pos)
        assert fence_close_pos < explanation_pos, (
            "explanation text must appear after the closing fence"
        )
        assert "diff --git" in out[:fence_close_pos]

    def test_preamble_before_diff_preserved(self) -> None:
        text = (
            "Here are the changes I made:\n"
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "+y\n"
        )
        out = _autowrap_diff_blocks(text)
        assert "Here are the changes I made:" in out
        assert "```diff" in out


class TestAlreadyFenced:
    def test_already_fenced_diff_not_double_wrapped(self) -> None:
        text = (
            "Here is a diff:\n"
            "```diff\n"
            "diff --git a/x b/x\n"
            "--- a/x\n"
            "+++ b/x\n"
            "@@ -1 +1 @@\n"
            "-a\n"
            "+b\n"
            "```\n"
        )
        out = _autowrap_diff_blocks(text)
        assert out.count("```diff") == 1
        assert out.count("```") == 2

    def test_fenced_with_other_language_not_rewrapped(self) -> None:
        text = "```bash\ngit diff\ndiff --git a/x b/x\n```\n"
        out = _autowrap_diff_blocks(text)
        assert "```bash" in out
        assert "```diff" not in out
