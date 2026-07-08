# (H) The special JS ingestion paths (object-literal property functions,
# (H) prototype-method assignments, assignment functions) hardcoded their
# (H) DEFINES parent to the module, or deferred onto a constructor guess whose
# (H) failure fell back to the module. For a function NESTED inside another
# (H) function that parent is wrong: the lexical parent rule (the same one
# (H) plain nested functions follow) applies. On thrift's lib/js this
# (H) surfaced as module-parented duplicates of correctly-parented nodes once
# (H) the dangling-edge campaign made the fallback edges real.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_relationships, run_updater

NESTED_JS = """
function createClient(SCl) {
    var self = this;
    SCl.prototype.new_seqid = function() {
        return self.seqid;
    };
    var jqRequest = {
        beforeSend: function (xreq) {
            return xreq;
        }
    };
    return jqRequest;
}

function Widget() {
    this.size = 1;
}

Widget.prototype.grow = function() {
    this.size += 1;
};
"""


def test_nested_special_functions_take_their_lexical_parent(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "app.js").write_text(NESTED_JS)
    run_updater(temp_repo, mock_ingestor)

    project = temp_repo.name
    defines = {
        (call.args[0][2], call.args[2][2])
        for call in get_relationships(mock_ingestor, cs.RelationshipType.DEFINES.value)
    }
    module_qn = f"{project}.app"
    creator = f"{module_qn}.createClient"

    nested_children = {
        to
        for frm, to in defines
        if to.endswith(".new_seqid") or to.endswith(".beforeSend")
    }
    assert nested_children, defines
    for frm, to in defines:
        if to in nested_children:
            assert frm == creator, (frm, to)

    # (H) The top-level prototype method keeps its registered constructor
    # (H) parent (Widget is a real Function node).
    grow_parents = {frm for frm, to in defines if to.endswith(".grow")}
    assert grow_parents == {f"{module_qn}.Widget"}, defines
