"""The findings pass runs on structural-tier files too (Ruby).

The shipped Ruby rule packs (`security/ruby.yaml`, `smells/ruby.yaml`,
`patterns/ruby.yaml`) never ran: the analyzer visits the modules in the map
it is handed, and both the full run and the scoped re-ingest handed it the
tree-sitter definition processor's map, which never holds a module the
ast-grep tier emitted. `--capture findings` on a Ruby repo wrote no
`SecurityIssue` at all (issue #2781).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("ast_grep_py")

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

_DEPLOYER = (
    "class Deployer\n"
    "  def run(cmd)\n"
    '    api_key = "sk_live_51Habcdefghijklmnop"\n'
    "    system(cmd)\n"
    "    eval(cmd)\n"
    "  end\n"
    "end\n"
)
_ISSUE_FINDINGS = {("eval_use", 5), ("os_command", 4), ("hardcoded_secret", 3)}


def _build(root: Path, files: dict[str, str]) -> tuple[GraphUpdater, MagicMock]:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    parsers, queries = load_parsers()
    mock = MagicMock()
    updater = GraphUpdater(
        ingestor=mock,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture([cs.CaptureGroup.FINDINGS.value]),
    )
    updater.run()
    return updater, mock


def _findings(mock: MagicMock, module_tail: str) -> set[tuple[str, int]]:
    """(rule, line) of every finding linked from the module whose last segment
    is `module_tail` (the ast-grep tier names `deployer.rb` `deployer_rb`)."""
    rels = {
        str(c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == cs.RelationshipType.HAS_VULNERABILITY.value
        and str(c.args[0][2]).rsplit(".", 1)[-1] == module_tail
    }
    found: set[tuple[str, int]] = set()
    for c in mock.ensure_node_batch.call_args_list:
        props = c.args[1]
        if str(props.get(cs.KEY_QUALIFIED_NAME, "")) in rels:
            found.add((str(props[cs.KEY_NAME]), int(props[cs.KEY_START_LINE])))
    return found


def test_the_ruby_security_pack_runs_on_a_full_index(tmp_path: Path) -> None:
    _, mock = _build(tmp_path, {"deployer.rb": _DEPLOYER})
    assert _ISSUE_FINDINGS <= _findings(mock, "deployer_rb"), _findings(
        mock, "deployer_rb"
    )


def test_a_reingested_ruby_file_keeps_its_findings(tmp_path: Path) -> None:
    updater, mock = _build(tmp_path, {"deployer.rb": _DEPLOYER})
    mock.reset_mock()
    target = tmp_path / "deployer.rb"
    target.write_text(_DEPLOYER.replace("eval(cmd)", "eval(cmd) # edited"), "utf-8")
    updater.reingest((target,))
    assert _ISSUE_FINDINGS <= _findings(mock, "deployer_rb"), _findings(
        mock, "deployer_rb"
    )


def test_other_files_are_analysed_as_before(tmp_path: Path) -> None:
    # Negatives: a Python file beside the Ruby one keeps its own finding, and
    # a clean Ruby file gets none.
    _, mock = _build(
        tmp_path,
        {
            "deployer.rb": _DEPLOYER,
            "clean.rb": "class Clean\n  def run\n    1\n  end\nend\n",
            "risky.py": "def run(data):\n    return eval(data)\n",
        },
    )
    assert _findings(mock, "risky"), "the Python finding disappeared"
    assert _findings(mock, "clean_rb") == set()
