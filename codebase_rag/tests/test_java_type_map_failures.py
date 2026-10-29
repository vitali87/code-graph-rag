"""A failing Java local-type pass is reported like every other engine's.

The Java builder caught every exception and logged it at error level with
no file, so the failure never reached the call pass's per-caller handler
(#3173) and the sync summary never named the file.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from codebase_rag import constants as cs
from codebase_rag.parsers.java.variable_analyzer import JavaVariableAnalyzer
from codebase_rag.tests.conftest import create_and_run_updater, recorded_edges
from codebase_rag.types_defs import ASTNode

_SOURCE = """\
package app;

class Worker {
    int run() { return 1; }
}

class Main {
    int helper() { return 2; }

    int simple() { return helper(); }

    int dispatch() {
        Worker w = new Worker();
        helper();
        return w.run();
    }
}
"""


def _calls(repo: Path, mock: MagicMock) -> set[tuple[str, str]]:
    return recorded_edges(mock, cs.RelationshipType.CALLS.value)


def test_a_failing_pass_is_recorded_against_its_caller(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    _fail_on_swallowed_pass_errors: list[str],
) -> None:
    original = JavaVariableAnalyzer._collect_all_variable_types

    def explode(
        self: JavaVariableAnalyzer,
        scope_node: ASTNode,
        local_var_types: dict[str, str],
        module_qn: str,
    ) -> None:
        name = scope_node.child_by_field_name(cs.FIELD_NAME)
        if name is not None and name.text == b"dispatch":
            raise RuntimeError("java pass exploded")
        original(self, scope_node, local_var_types, module_qn)

    source = temp_repo / "app" / "Main.java"
    source.parent.mkdir()
    source.write_text(_SOURCE, encoding="utf-8")
    with patch.object(JavaVariableAnalyzer, "_collect_all_variable_types", explode):
        updater = create_and_run_updater(temp_repo, mock_ingestor)

    calls = _calls(temp_repo, mock_ingestor)
    assert any("Main.simple(" in c and "Main.helper(" in t for c, t in calls), calls
    assert any("Main.dispatch(" in c and "Main.helper(" in t for c, t in calls), calls
    logged = _fail_on_swallowed_pass_errors
    assert len(logged) == 1, logged
    assert "Main.dispatch" in logged[0], logged
    assert updater.call_pass_failures == [source]
    logged.clear()
