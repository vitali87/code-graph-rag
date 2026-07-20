# (H) ast-grep finding analyzer (issue #413). Runs categorized YAML rules over
# (H) indexed source files and emits Pattern/CodeSmell/SecurityIssue nodes linked
# (H) to each file's Module. The FINDINGS capture group is opt-in, so these tests
# (H) build a selection with it enabled and assert the finding nodes/edges land.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

PATTERN = cs.NodeLabel.PATTERN.value
CODE_SMELL = cs.NodeLabel.CODE_SMELL.value
SECURITY_ISSUE = cs.NodeLabel.SECURITY_ISSUE.value
IMPLEMENTS_PATTERN = cs.RelationshipType.IMPLEMENTS_PATTERN.value
HAS_SMELL = cs.RelationshipType.HAS_SMELL.value
HAS_VULNERABILITY = cs.RelationshipType.HAS_VULNERABILITY.value

SINGLETON_PY = (
    "class Config:\n"
    "    _instance = None\n"
    "\n"
    "    def get(self):\n"
    "        return self._instance\n"
)

SQLI_PY = (
    "def lookup(db, user_id):\n"
    "    return db.execute('SELECT * FROM t WHERE id = ' + user_id)\n"
)

SAFE_PY = (
    "def lookup(db, user_id):\n"
    "    return db.execute('SELECT * FROM t WHERE id = ?', (user_id,))\n"
)


def _run(tmp_path: Path, files: dict[str, str], tokens: list[str]) -> MagicMock:
    parsers, queries = load_parsers()
    for rel, content in files.items():
        (tmp_path / rel).write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture(tokens),
    ).run()
    return mock


def _node_names(mock: MagicMock, label: str) -> set[str]:
    return {
        c.args[1].get(cs.KEY_NAME)
        for c in mock.ensure_node_batch.call_args_list
        if str(c.args[0]) == label
    }


def _rel_targets(mock: MagicMock, rel_type: str) -> set[str]:
    return {
        c.args[2][2]
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == rel_type
    }


def test_singleton_pattern_detected(tmp_path: Path) -> None:
    mock = _run(tmp_path, {"config.py": SINGLETON_PY}, ["+findings"])
    assert "singleton" in _node_names(mock, PATTERN)


def test_sql_injection_detected(tmp_path: Path) -> None:
    mock = _run(tmp_path, {"dao.py": SQLI_PY}, ["+findings"])
    assert "sqli_concat" in _node_names(mock, SECURITY_ISSUE)


def test_finding_linked_to_module(tmp_path: Path) -> None:
    mock = _run(tmp_path, {"dao.py": SQLI_PY}, ["+findings"])
    targets = _rel_targets(mock, HAS_VULNERABILITY)
    assert any(qn.endswith("sqli_concat") for qn in targets), targets


def test_safe_query_not_flagged(tmp_path: Path) -> None:
    mock = _run(tmp_path, {"dao.py": SAFE_PY}, ["+findings"])
    assert "sqli_concat" not in _node_names(mock, SECURITY_ISSUE)


def test_findings_opt_in_disabled_by_default(tmp_path: Path) -> None:
    # (H) FINDINGS is not in DEFAULT_CAPTURE_GROUPS; a default index emits none.
    mock = _run(tmp_path, {"config.py": SINGLETON_PY, "dao.py": SQLI_PY}, [])
    assert _node_names(mock, PATTERN) == set()
    assert _node_names(mock, SECURITY_ISSUE) == set()


class _FakeIngestor:
    def __init__(self) -> None:
        self.nodes: list[tuple[str, dict]] = []
        self.rels: list[tuple[tuple, str, tuple]] = []

    def ensure_node_batch(self, label, props) -> None:
        self.nodes.append((str(label), props))

    def ensure_relationship_batch(self, src, rel, dst) -> None:
        self.rels.append((src, str(rel), dst))


def _labelled(rules, label) -> list:
    return [r for r in rules if r.node_label == label]


def test_rules_load_and_meet_acceptance_counts() -> None:
    from codebase_rag.analyzers.ast_grep_analyzer import load_finding_rules

    rules = load_finding_rules()
    assert {".py", ".js", ".ts"} <= set(rules)
    py = rules[".py"].rules
    assert len(_labelled(py, cs.NodeLabel.PATTERN)) >= 5
    assert len(_labelled(py, cs.NodeLabel.CODE_SMELL)) >= 5
    assert len(_labelled(py, cs.NodeLabel.SECURITY_ISSUE)) >= 5
    for ext in (".js", ".ts"):
        js = rules[ext].rules
        assert len(_labelled(js, cs.NodeLabel.PATTERN)) >= 3
        assert len(_labelled(js, cs.NodeLabel.CODE_SMELL)) >= 3
        assert len(_labelled(js, cs.NodeLabel.SECURITY_ISSUE)) >= 3


def test_analyzer_emits_node_and_edge_directly(tmp_path: Path) -> None:
    from codebase_rag.analyzers import FindingAnalyzer

    src = tmp_path / "m.py"
    src.write_text(SINGLETON_PY, encoding="utf-8")
    ing = _FakeIngestor()
    FindingAnalyzer(ing, tmp_path, resolve_capture(["+findings"])).analyze(
        {"proj.m": src}
    )
    pattern_nodes = [p for label, p in ing.nodes if label == PATTERN]
    assert any(p[cs.KEY_NAME] == "singleton" for p in pattern_nodes)
    assert any(rel == IMPLEMENTS_PATTERN for _s, rel, _d in ing.rels)


def test_analyzer_noops_when_findings_disabled(tmp_path: Path) -> None:
    from codebase_rag.analyzers import FindingAnalyzer

    src = tmp_path / "m.py"
    src.write_text(SINGLETON_PY, encoding="utf-8")
    ing = _FakeIngestor()
    FindingAnalyzer(ing, tmp_path, resolve_capture([])).analyze({"proj.m": src})
    assert ing.nodes == []
    assert ing.rels == []
