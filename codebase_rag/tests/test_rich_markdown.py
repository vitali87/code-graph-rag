"""How the agent's answers render their headings in the terminal.

`LeftAlignedMarkdown` replaces only rich's heading element: a top-level
heading sits left in a heavy box instead of centred, a second-level one gets
a blank line above it, and everything else renders as rich's own Markdown.
Each case is compared with rich's rendering of the same source, so the
assertions name the difference this module makes rather than rich's spacing.
"""

from __future__ import annotations

import io

from rich.console import Console
from rich.markdown import Markdown

from codebase_rag.utils.rich_markdown import LeftAlignedHeading, LeftAlignedMarkdown

_WIDTH = 32


def _render(markdown: Markdown) -> list[str]:
    console = Console(
        file=io.StringIO(), width=_WIDTH, color_system=None, force_terminal=False
    )
    console.print(markdown)
    output = console.file
    assert isinstance(output, io.StringIO)
    return [line.rstrip() for line in output.getvalue().splitlines()]


def _blank_lines_before(lines: list[str], text: str) -> int:
    index = lines.index(text)
    count = 0
    while index - count - 1 >= 0 and not lines[index - count - 1]:
        count += 1
    return count


def test_a_top_level_heading_sits_left_in_a_heavy_box() -> None:
    lines = _render(LeftAlignedMarkdown("# Title\n\nbody"))

    box = [line for line in lines if line.startswith(("┏", "┃", "┗"))]
    assert box[0] == "┏" + "━" * (_WIDTH - 2) + "┓"
    assert box[1].startswith("┃ Title ")
    assert box[1].endswith("┃")
    assert box[2] == "┗" + "━" * (_WIDTH - 2) + "┛"
    # Rich alone centres the same heading and draws no box around it.
    plain = _render(Markdown("# Title\n\nbody"))
    assert not any(line.startswith("┏") for line in plain)
    assert any(line.startswith(" ") and line.strip() == "Title" for line in plain)


def test_a_boxed_heading_keeps_its_inline_text_left_aligned() -> None:
    lines = _render(LeftAlignedMarkdown("# A **bold** heading"))

    assert any(line.startswith("┃ A bold heading ") for line in lines)


def test_a_second_level_heading_gets_one_more_blank_line_above_it() -> None:
    source = "para\n\n## Sub\n\ntext"

    ours = _render(LeftAlignedMarkdown(source))
    theirs = _render(Markdown(source))

    assert "Sub" in ours
    assert _blank_lines_before(ours, "Sub") == _blank_lines_before(theirs, "Sub") + 1


def test_deeper_headings_stay_flush_left_with_rich_spacing() -> None:
    source = "para\n\n### Deep\n\n#### Deeper\n\ntext"

    ours = _render(LeftAlignedMarkdown(source))
    theirs = _render(Markdown(source))

    for heading in ("Deep", "Deeper"):
        assert heading in ours
        assert _blank_lines_before(ours, heading) == _blank_lines_before(
            theirs, heading
        )


def test_everything_but_headings_renders_as_rich_markdown() -> None:
    source = "Some *prose*.\n\n- one\n- two\n\n```python\nx = 1\n```\n\n> quoted"

    assert _render(LeftAlignedMarkdown(source)) == _render(Markdown(source))


def test_rich_markdown_itself_keeps_its_own_heading() -> None:
    # `elements` is a class-level mapping: the subclass must copy it, or every
    # Markdown in the process would start boxing its headings.
    assert LeftAlignedMarkdown.elements["heading_open"] is LeftAlignedHeading
    assert Markdown.elements["heading_open"] is not LeftAlignedHeading
    assert set(LeftAlignedMarkdown.elements) == set(Markdown.elements)
