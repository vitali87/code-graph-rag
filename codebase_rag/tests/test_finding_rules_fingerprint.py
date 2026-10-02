# A finding is a function of (source file, ast-grep rule set), but the parser
# fingerprint keyed only the parser code, so a release that changed a rule left
# every unchanged file on the in-sync fast path: the findings the OLD rules
# produced stayed in the graph and the new rules never ran (review of #2533,
# where the narrowed innerhtml_xss rule kept all its constant-literal hits on a
# re-sync). The bundled rule YAML and the analyzer that applies it are now
# fingerprint inputs, so a rule change re-parses once and re-runs the analysis.
from __future__ import annotations

import functools
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_updater
from codebase_rag.analyzers import ast_grep_analyzer
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_fingerprint import compute_parser_fingerprint
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.test_graph_updater_incremental_rename import (
    PROJECT_NAME,
    InMemoryGraph,
)
from codebase_rag.types_defs import PropertyDict

_RULES_REL = Path("analyzers") / "ast_grep_rules"
_SECURITY_JS = _RULES_REL / "security" / "javascript.yaml"
_SHIPPED_SECURITY_JS = Path(ast_grep_analyzer.__file__).parent.parent / _SECURITY_JS

# main's innerhtml_xss before #2533: any right-hand side matched.
_OLD_SECURITY_JS = """\
ast_grep_id: javascript
extensions: [.js]
rules:
  - id: innerhtml_xss
    message: "Possible XSS: assignment to innerHTML/outerHTML"
    rule:
      any:
        - pattern: "$EL.innerHTML = $X"
        - pattern: "$EL.innerHTML += $X"
        - pattern: "$EL.outerHTML = $X"
        - pattern: "$EL.outerHTML += $X"
"""

_TERMYNAL_JS = (
    "this.container.innerHTML = '';\n"
    'restart.innerHTML = "restart";\n'
    "div.innerHTML = `<span>${line.value}</span>`;\n"
)

HAS_VULNERABILITY = cs.RelationshipType.HAS_VULNERABILITY.value


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class _MergingGraph(InMemoryGraph):
    # Memgraph writes nodes with `MERGE ... SET n += props`. The base double
    # replaces them, so the later partial write of a JS Module (its unresolved
    # specifiers) dropped the `path` the module delete matches on.
    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        uid = properties[cs.NODE_UNIQUE_CONSTRAINTS[label]]
        self.nodes.setdefault((str(label), uid), {}).update(properties)


class TestRuleFilesAreFingerprintInputs:
    @pytest.mark.parametrize(
        "rel",
        [
            _SECURITY_JS,
            _RULES_REL / "smells" / "python.yaml",
            Path("parsers") / "ast_grep_patterns" / "swift.yaml",
            Path("analyzers") / "ast_grep_analyzer.py",
        ],
    )
    def test_changes_when_an_ast_grep_input_changes(
        self, tmp_path: Path, rel: Path
    ) -> None:
        pkg = tmp_path / "pkg"
        _write(pkg / rel, "a: 1\n")
        before = compute_parser_fingerprint(pkg)
        _write(pkg / rel, "a: 2\n")
        assert compute_parser_fingerprint(pkg) != before

    def test_unchanged_rules_keep_the_fingerprint(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        _write(pkg / _SECURITY_JS, _OLD_SECURITY_JS)
        assert compute_parser_fingerprint(pkg) == compute_parser_fingerprint(pkg)

    def test_a_non_rule_file_beside_the_rules_is_not_an_input(
        self, tmp_path: Path
    ) -> None:
        # Only the YAML the loaders read decides findings; a README or a stray
        # editor backup next to it must not force a full re-parse.
        pkg = tmp_path / "pkg"
        _write(pkg / _SECURITY_JS, _OLD_SECURITY_JS)
        notes = pkg / _RULES_REL / "README.md"
        _write(notes, "one\n")
        before = compute_parser_fingerprint(pkg)
        _write(notes, "two\n")
        assert compute_parser_fingerprint(pkg) == before


@pytest.fixture
def rules_pkg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # The analyzer and the fingerprint both read the rules from this package
    # root, so a rule edit here is what a release changing a rule looks like.
    pkg = tmp_path / "pkg"
    _write(pkg / _SECURITY_JS, _OLD_SECURITY_JS)
    monkeypatch.setattr(ast_grep_analyzer, "_RULES_DIR", pkg / _RULES_REL)
    monkeypatch.setattr(
        graph_updater,
        "compute_parser_fingerprint",
        functools.partial(compute_parser_fingerprint, pkg),
    )
    return pkg


def _sync(repo: Path, graph: _MergingGraph) -> GraphUpdater:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=graph,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT_NAME,
        capture=resolve_capture(["+findings"]),
    )
    updater.run()
    return updater


def _linked_finding_lines(graph: _MergingGraph) -> list[int]:
    lines: list[int] = []
    for _fl, _fk, _fv, rel, tl, _tk, tv in graph.rels:
        if rel != HAS_VULNERABILITY:
            continue
        line = graph.nodes[(tl, tv)][cs.KEY_START_LINE]
        assert isinstance(line, int)
        lines.append(line)
    return sorted(lines)


def test_a_rule_change_refreshes_findings_of_an_unchanged_file(
    tmp_path: Path, rules_pkg: Path
) -> None:
    repo = tmp_path / "repo"
    _write(repo / "termynal.js", _TERMYNAL_JS)
    graph = _MergingGraph()
    _sync(repo, graph)
    assert _linked_finding_lines(graph) == [1, 2, 3], (
        "fixture guard: the old rule must flag every assignment"
    )

    _write(rules_pkg / _SECURITY_JS, _SHIPPED_SECURITY_JS.read_text(encoding="utf-8"))
    updater = _sync(repo, graph)

    assert _linked_finding_lines(graph) == [3], (
        "the re-sync kept the old rules' constant-literal findings"
    )
    assert updater._reparsed_file_keys == {"termynal.js"}


def test_unchanged_rules_keep_the_in_sync_fast_path(
    tmp_path: Path, rules_pkg: Path
) -> None:
    repo = tmp_path / "repo"
    _write(repo / "termynal.js", _TERMYNAL_JS)
    graph = _MergingGraph()
    _sync(repo, graph)

    updater = _sync(repo, graph)

    assert updater._reparsed_file_keys == set()
    assert _linked_finding_lines(graph) == [1, 2, 3]
