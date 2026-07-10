# (H) `this.method()` inside a prototype-assigned function dispatches to a
# (H) sibling method of the same prototype target (Date.prototype.strftime
# (H) calling this.getTwoDigitMonth(), django admin's core.js). Binding `this`
# (H) only to module-level functions (the CommonJS pattern) leaves every such
# (H) sibling call unresolved and the methods falsely dead.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import get_relationships, run_updater

CORE_JS = """Date.prototype.getTwoDigitMonth = function () {
    return this.getMonth() + 1;
};

Date.prototype.strftime = function (format) {
    if (format === "m") {
        return this.getTwoDigitMonth();
    }
    return "";
};
"""


def _calls(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }


def test_prototype_this_call_resolves_to_sibling_method(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "proto_this"
    project.mkdir()
    (project / "core.js").write_text(CORE_JS, encoding="utf-8")

    run_updater(project, mock_ingestor, skip_if_missing="javascript")

    assert (
        "proto_this.core.Date.strftime",
        "proto_this.core.Date.getTwoDigitMonth",
    ) in _calls(mock_ingestor)


def test_module_this_call_still_resolves_to_free_function(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) The CommonJS module pattern (`this.render()` at module scope binding a
    # (H) free function) must keep resolving as before.
    project = temp_repo / "module_this"
    project.mkdir()
    (project / "app.js").write_text(
        "function render() {\n"
        "    return 1;\n"
        "}\n\n"
        "function page() {\n"
        "    return this.render();\n"
        "}\n",
        encoding="utf-8",
    )

    run_updater(project, mock_ingestor, skip_if_missing="javascript")

    assert ("module_this.app.page", "module_this.app.render") in _calls(mock_ingestor)
