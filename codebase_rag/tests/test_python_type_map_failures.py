"""A failing Python local-type pass is reported like every other engine's.

The Python builder wrapped its ten passes in one `except Exception` logged at
debug. A failure therefore never reached the call pass's handler (#3173), the
sync summary never named the file, and the half-built map it returned still
carried the `self`/`cls` seed that typed `self.m()` calls.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from codebase_rag import constants as cs
from codebase_rag.parsers.py.type_inference import PythonTypeInferenceEngine
from codebase_rag.tests.conftest import create_and_run_updater
from codebase_rag.types_defs import ASTNode

_SOURCE = """\
class Worker:
    def run(self):
        return 1


def helper():
    return 2


def other():
    return 3


def simple():
    return other()


def dispatch():
    w = Worker()
    helper()
    return w.run()
"""


def _calls(repo: Path, mock: MagicMock) -> set[tuple[str, str]]:
    prefix = f"{repo.name}."
    return {
        (str(c.args[0][2]).removeprefix(prefix), str(c.args[2][2]).removeprefix(prefix))
        for c in mock.ensure_relationship_batch.call_args_list
        if c.args[1] == cs.RelationshipType.CALLS.value
    }


def test_a_failing_pass_is_recorded_against_its_caller(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    _fail_on_swallowed_pass_errors: list[str],
) -> None:
    original = PythonTypeInferenceEngine._infer_property_return_types

    def explode(
        self: PythonTypeInferenceEngine,
        caller_node: ASTNode,
        local_var_types: dict[str, str],
        module_qn: str,
    ) -> None:
        name = caller_node.child_by_field_name(cs.FIELD_NAME)
        if name is not None and name.text == b"dispatch":
            raise RuntimeError("property pass exploded")
        original(self, caller_node, local_var_types, module_qn)

    (temp_repo / "app.py").write_text(_SOURCE, encoding="utf-8")
    with patch.object(
        PythonTypeInferenceEngine, "_infer_property_return_types", explode
    ):
        updater = create_and_run_updater(temp_repo, mock_ingestor)

    calls = _calls(temp_repo, mock_ingestor)
    assert ("app.simple", "app.other") in calls, calls
    assert ("app.dispatch", "app.helper") in calls, calls
    logged = _fail_on_swallowed_pass_errors
    assert len(logged) == 1, logged
    assert f"{temp_repo.name}.app.dispatch (app.py)" in logged[0], logged
    assert updater.call_pass_failures == [temp_repo / "app.py"]
    logged.clear()
