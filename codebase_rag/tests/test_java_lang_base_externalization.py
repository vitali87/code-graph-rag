# Java implicitly imports java.lang, so `extends Exception` or
# `implements Runnable` names a POSITIVELY external base with no import
# statement to map it. The deferred-inherits resolver used to treat these
# as unresolvable module-anchored guesses and emit nothing, losing real
# inheritance knowledge the graph should keep. Mirroring the JS global
# rule (Error -> builtin.Error), a bare base in the java.lang table now
# emits onto an ExternalModule node (java.lang.Exception), reusing the
# JAVA_LANG_PREFIX machinery the receiver type resolver already has.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_relationships, run_updater

EXCEPTIONS_JAVA = """
package errors;

public class CustomException extends Exception {
    public CustomException(String message) {
        super(message);
    }
}
"""

TASK_JAVA = """
package work;

public class Task implements Runnable, Comparable<Task> {
    public void run() {}

    public int compareTo(Task other) {
        return 0;
    }
}
"""

UNKNOWN_BASE_JAVA = """
package widgets;

public class FancyWidget extends Widget {
    public int size() { return 1; }
}
"""


def _pairs(mock_ingestor: MagicMock, rel_type: cs.RelationshipType) -> set[tuple]:
    return {
        (call.args[0][2], str(call.args[2][0]), call.args[2][2])
        for call in get_relationships(mock_ingestor, rel_type.value)
    }


def _node_keys(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (str(c.args[0]), c.args[1].get("qualified_name"))
        for c in mock_ingestor.ensure_node_batch.call_args_list
    }


def test_java_lang_exception_base_emits_external_edge_and_node(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "CustomException.java").write_text(EXCEPTIONS_JAVA)
    run_updater(temp_repo, mock_ingestor, skip_if_missing="java")

    project = temp_repo.name
    inherits = _pairs(mock_ingestor, cs.RelationshipType.INHERITS)
    expected = (
        f"{project}.CustomException.CustomException",
        cs.NodeLabel.EXTERNAL_MODULE.value,
        f"{cs.JAVA_LANG_PREFIX}Exception",
    )
    assert expected in inherits, inherits
    assert (
        cs.NodeLabel.EXTERNAL_MODULE.value,
        f"{cs.JAVA_LANG_PREFIX}Exception",
    ) in _node_keys(mock_ingestor)


def test_java_lang_interfaces_emit_external_implements(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "Task.java").write_text(TASK_JAVA)
    run_updater(temp_repo, mock_ingestor, skip_if_missing="java")

    project = temp_repo.name
    implements = _pairs(mock_ingestor, cs.RelationshipType.IMPLEMENTS)
    child = f"{project}.Task.Task"
    for iface in ("Runnable", "Comparable"):
        expected = (
            child,
            cs.NodeLabel.EXTERNAL_MODULE.value,
            f"{cs.JAVA_LANG_PREFIX}{iface}",
        )
        assert expected in implements, implements


def test_unknown_bare_base_externalizes_under_written_name(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A bare base NOT in the java.lang table is still a syntactic
    # inheritance fact, and a name resolving to no indexed class is by
    # construction defined outside the indexed tree (a generated Iface, a
    # dependency class). The thrift oracle re-measure showed dropping
    # these loses real recall, so the WRITTEN name externalizes; the
    # java.lang table now only refines the qn for known java.lang types.
    (temp_repo / "FancyWidget.java").write_text(UNKNOWN_BASE_JAVA)
    run_updater(temp_repo, mock_ingestor, skip_if_missing="java")

    project = temp_repo.name
    inherits = _pairs(mock_ingestor, cs.RelationshipType.INHERITS)
    expected = (
        f"{project}.FancyWidget.FancyWidget",
        cs.NodeLabel.EXTERNAL_MODULE.value,
        "Widget",
    )
    assert expected in inherits, inherits
