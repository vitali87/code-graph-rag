# (H) Parse recovery that destroys a class specifier orphans the class's
# (H) out-of-line ctors at module scope as TYPE-LESS plain-identifier
# (H) definitions. Registering them as module Functions (a) steals the class's
# (H) qualified name when the ctor precedes the class pass (the CLASS node gets
# (H) a `@line` dedup suffix, breaking type resolution), and (b) leaves the
# (H) ctor with no reachable qn: construction sites resolve to the CLASS and
# (H) emit INSTANTIATES only, so the ctor reports dead (fmt base.basic_appender
# (H) et al.). The fix: a name-matching registered class reattaches the orphan
# (H) as a METHOD under the class, and C++ construction sites redirect a CALLS
# (H) edge to the class's ctor methods exactly as Java/C# construction does.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import run_updater

ORPHANED_CTOR_CC = """
struct file {
  int fd_;
};

file(int fd) { helper(fd); }

void helper(int fd) {}

void use_file() {
  auto f = file(1);
}
"""

ORPHANED_ZERO_PARAM_CTOR_CC = """
struct pipe {
  int fd_;
};

pipe() {}
"""

IN_CLASS_CTOR_CC = """
struct widget {
  int x_;
  widget(int x) : x_(x) {}
};

void use_widget() {
  auto w = widget(1);
}
"""

NO_CLASS_NAMED_PARAM_CC = """
write2digits(char* buf) { buf[0] = '0'; }
"""


def _nodes(mock_ingestor: MagicMock, label: cs.NodeLabel) -> set[str]:
    return {
        c.args[1].get("qualified_name")
        for c in mock_ingestor.ensure_node_batch.call_args_list
        if str(c.args[0]) == label.value
    }


def _calls(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == cs.RelationshipType.CALLS.value
    }


def _defines_method(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == cs.RelationshipType.DEFINES_METHOD.value
    }


def test_named_param_orphan_ctor_registers_as_class_method(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "os.cc").write_text(ORPHANED_CTOR_CC)
    run_updater(temp_repo, mock_ingestor)

    methods = _nodes(mock_ingestor, cs.NodeLabel.METHOD)
    assert any(qn.endswith(".file.file") for qn in methods), methods
    # (H) The class must keep its plain qualified name: the old module-Function
    # (H) registration ran before the class pass and stole it, leaving the
    # (H) CLASS node with a `@line` dedup suffix.
    classes = _nodes(mock_ingestor, cs.NodeLabel.CLASS)
    assert any(qn.endswith(".file") and cs.DUP_QN_MARKER not in qn for qn in classes), (
        classes
    )
    functions = _nodes(mock_ingestor, cs.NodeLabel.FUNCTION)
    assert not any(qn.endswith(".os.file") for qn in functions), functions


def test_zero_param_orphan_ctor_registers_as_class_method(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "pipe.cc").write_text(ORPHANED_ZERO_PARAM_CTOR_CC)
    run_updater(temp_repo, mock_ingestor)

    methods = _nodes(mock_ingestor, cs.NodeLabel.METHOD)
    assert any(qn.endswith(".pipe.pipe") for qn in methods), methods


def test_defines_method_edge_links_class_to_reattached_ctor(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "os.cc").write_text(ORPHANED_CTOR_CC)
    run_updater(temp_repo, mock_ingestor)

    edges = _defines_method(mock_ingestor)
    assert any(
        src.endswith(".file") and dst.endswith(".file.file") for src, dst in edges
    ), edges


def test_construction_call_emits_calls_to_ctor(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "w.cc").write_text(IN_CLASS_CTOR_CC)
    run_updater(temp_repo, mock_ingestor)

    calls = _calls(mock_ingestor)
    # (H) `auto w = widget(1)` runs the ctor; INSTANTIATES to the class alone
    # (H) leaves the ctor unreachable (the fmt buffer.buffer dead-list class).
    assert any(
        src.endswith(".use_widget") and dst.endswith(".widget.widget")
        for src, dst in calls
    ), calls


def test_construction_call_reaches_reattached_orphan_ctor(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "os.cc").write_text(ORPHANED_CTOR_CC)
    run_updater(temp_repo, mock_ingestor)

    calls = _calls(mock_ingestor)
    assert any(
        src.endswith(".use_file") and dst.endswith(".file.file") for src, dst in calls
    ), calls


def test_orphan_ctor_body_calls_attributed_to_method(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "os.cc").write_text(ORPHANED_CTOR_CC)
    run_updater(temp_repo, mock_ingestor)

    calls = _calls(mock_ingestor)
    assert any(
        src.endswith(".file.file") and dst.endswith(".helper") for src, dst in calls
    ), calls


def test_named_param_orphan_without_class_stays_module_function(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "fmt.cc").write_text(NO_CLASS_NAMED_PARAM_CC)
    run_updater(temp_repo, mock_ingestor)

    functions = _nodes(mock_ingestor, cs.NodeLabel.FUNCTION)
    # (H) Named parameters prove the definition real even when no class bears
    # (H) the name (a free function whose return type recovery destroyed).
    assert any(qn.endswith(".write2digits") for qn in functions), functions
