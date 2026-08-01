# A C# declaration whose attribute is wrapped in a conditional-compilation
# block (`#if ... [Attr] ... #endif`) parses with a leading
# preproc_if_in_attribute_list child, so the raw declaration node starts on
# the `#if` line. cgr must record the declaration's start line as the real
# first token (the conditional attribute), matching Roslyn's span, not the
# `#if` directive line.
from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_nodes, run_updater

SKIP = "c_sharp"


def _start_line(mock_ingestor: MagicMock, node_type: str, leaf: str) -> int:
    for call in get_nodes(mock_ingestor, node_type):
        props = call[0][1]
        if props[cs.KEY_QUALIFIED_NAME].split("(", 1)[0].endswith(leaf):
            return props[cs.KEY_START_LINE]
    raise AssertionError(f"{node_type} {leaf} not found")


def test_class_start_line_skips_leading_if_directive(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "cs_preproc_class"
    project.mkdir()
    # Lines: 1 ns, 2 blank, 3 doc, 4 #if, 5 [attr], 6 #endif, 7 class.
    (project / "Broken.cs").write_text(
        "namespace N;\n"
        "\n"
        "/// <summary>doc</summary>\n"
        "#if !NETCOREAPP\n"
        "[System.Serializable]\n"
        "#endif\n"
        "public class Broken { }\n",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)
    # The conditional attribute is on line 5; the `#if` directive on line 4
    # must not be counted as the class's start.
    assert _start_line(mock_ingestor, cs.NodeLabel.CLASS.value, "Broken") == 5


def test_method_start_line_skips_leading_if_directive(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "cs_preproc_method"
    project.mkdir()
    # Lines: 1 ns, 2 class, 3 #if, 4 [attr], 5 #endif, 6 method.
    (project / "Host.cs").write_text(
        "namespace N;\n"
        "public class Host {\n"
        "#if DEBUG\n"
        "[System.Obsolete]\n"
        "#endif\n"
        "public void M() { }\n"
        "}\n",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)
    assert _start_line(mock_ingestor, cs.NodeLabel.METHOD.value, "M") == 4


def test_plain_attribute_start_line_unchanged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Guard the fix does not shift the normal (non-conditional) attribute case:
    # a plain `[Attr]` above a class is already the class's first token.
    project = temp_repo / "cs_plain_attr"
    project.mkdir()
    # Lines: 1 ns, 2 [attr], 3 class.
    (project / "Tagged.cs").write_text(
        "namespace N;\n[System.Serializable]\npublic class Tagged { }\n",
        encoding="utf-8",
    )
    run_updater(project, mock_ingestor, skip_if_missing=SKIP)
    assert _start_line(mock_ingestor, cs.NodeLabel.CLASS.value, "Tagged") == 2
