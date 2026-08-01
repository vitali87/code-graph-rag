# JS/TS calls resolving to synthetic `builtin.*` qns (console.log,
# Date.now, JSON.stringify) emitted CALLS edges to Function nodes that are
# never created, so the database dropped every one (issue #652: 485 across
# the fixture suite). A builtin is not a first-party callee; mirroring the
# C++ builtin-operator rule, no edge is emitted at all. A first-party
# callback handed to a builtin must still be kept reachable via the
# argument-reference path.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_relationships, run_updater

APP_JS = """
function formatUser(user) {
    return JSON.stringify(user);
}

function logStartup() {
    console.log("starting", Date.now());
}

function onTick() {
    return 42;
}

function scheduleTick() {
    setTimeout(onTick, 100);
}
"""

BUILTIN_QN_PREFIX = f"{cs.BUILTIN_PREFIX}{cs.SEPARATOR_DOT}"


def test_builtin_calls_emit_no_phantom_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "app.js").write_text(APP_JS)
    run_updater(temp_repo, mock_ingestor)

    calls = get_relationships(mock_ingestor, cs.RelationshipType.CALLS.value)
    builtin_targets = [
        call.args
        for call in calls
        if str(call.args[2][2]).startswith(BUILTIN_QN_PREFIX)
    ]
    assert not builtin_targets, builtin_targets


def test_callback_passed_to_builtin_stays_reachable(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Dropping the builtin edge must not drop the argument-flow edge that
    # keeps a first-party callback handed to the builtin reachable.
    (temp_repo / "app.js").write_text(APP_JS)
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    called = {
        (call.args[0][2], call.args[2][2])
        for call in get_relationships(mock_ingestor, cs.RelationshipType.CALLS.value)
    }
    assert (f"{project}.app.scheduleTick", f"{project}.app.onTick") in called, called
