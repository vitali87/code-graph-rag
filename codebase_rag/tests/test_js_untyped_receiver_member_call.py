# (H) A JS member call on an UNTYPED receiver (`view.render(opts, cb)` inside
# (H) `tryRender(view, cb)` -- the instance was constructed in the CALLER, so the
# (H) param has no inferred type) fell to the bare-name trie and mis-bound to the
# (H) same-module free function `render` (express's application.render): a false
# (H) edge AND the real prototype method View.render reported dead. A member call
# (H) targets a MEMBER: resolve to the unique member-like candidate (its parent qn
# (H) is itself registered) or drop; never rebind to a free function.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def test_untyped_receiver_member_call_binds_unique_prototype_method(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "exview"
    root.mkdir(parents=True)
    (root / "view.js").write_text(
        "function View(name) {\n"
        "  this.name = name\n"
        "}\n"
        "View.prototype.render = function render(options, callback) {\n"
        "  return callback(null, options)\n"
        "}\n"
        "module.exports = View\n",
        encoding="utf-8",
    )
    (root / "application.js").write_text(
        "var View = require('./view')\n"
        "exports.render = function (name, done) {\n"
        "  var view = new View(name)\n"
        "  tryRender(view, done)\n"
        "}\n"
        "function tryRender(view, callback) {\n"
        "  view.render({}, callback)\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    calls = {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }
    assert any(
        f.endswith(".tryRender") and t.endswith(".View.render") for f, t in calls
    ), sorted(t for f, t in calls if "render" in t)
    assert not any(
        f.endswith(".tryRender") and t.endswith(".application.render") for f, t in calls
    ), "member call mis-bound to the same-module free function"


def test_var_require_visibility_disambiguates_member_call(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) With a same-named prototype method in an UNRELATED module (express's
    # (H) examples GithubView.render), uniqueness needs the import-visibility
    # (H) filter -- and express binds View with `var View = require('./view')`,
    # (H) a variable_declaration the require-mapping walk did not visit (it only
    # (H) handled const/let lexical_declarations).
    root = temp_repo / "exvis"
    root.mkdir(parents=True)
    (root / "view.js").write_text(
        "function View(name) {\n"
        "  this.name = name\n"
        "}\n"
        "View.prototype.render = function render(options, callback) {\n"
        "  return callback(null, options)\n"
        "}\n"
        "module.exports = View\n",
        encoding="utf-8",
    )
    (root / "github-view.js").write_text(
        "function GithubView(name) {\n"
        "  this.name = name\n"
        "}\n"
        "GithubView.prototype.render = function render(options, callback) {\n"
        "  return callback(null, options)\n"
        "}\n"
        "module.exports = GithubView\n",
        encoding="utf-8",
    )
    (root / "application.js").write_text(
        "var View = require('./view')\n"
        "exports.render = function (name, done) {\n"
        "  var view = new View(name)\n"
        "  tryRender(view, done)\n"
        "}\n"
        "function tryRender(view, callback) {\n"
        "  view.render({}, callback)\n"
        "}\n",
        encoding="utf-8",
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    calls = {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, "CALLS")
    }
    assert any(
        f.endswith(".tryRender") and t.endswith(".view.View.render") for f, t in calls
    ), sorted(t for f, t in calls if "render" in t)
    assert not any(
        f.endswith(".tryRender") and t.endswith(".GithubView.render") for f, t in calls
    ), "member call bound the invisible module's twin"
