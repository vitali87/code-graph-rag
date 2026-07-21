# ast-grep finding analyzer (issue #413). Runs categorized YAML rules over
# indexed source files and emits Pattern/CodeSmell/SecurityIssue nodes linked
# to each file's Module. The FINDINGS capture group is opt-in, so these tests
# build a selection with it enabled and assert the finding nodes/edges land.
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
    # FINDINGS is not in DEFAULT_CAPTURE_GROUPS; a default index emits none.
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


def test_same_line_findings_get_distinct_ids(tmp_path: Path) -> None:
    # two matches of one rule on a single line must not collapse into one
    # node; the qualified_name has to distinguish them by column.
    from codebase_rag.analyzers import FindingAnalyzer

    src = tmp_path / "a.js"
    src.write_text("console.log(1); console.log(2);\n", encoding="utf-8")
    ing = _FakeIngestor()
    FindingAnalyzer(ing, tmp_path, resolve_capture(["+findings"])).analyze(
        {"proj.a": src}
    )
    qns = [
        p[cs.KEY_QUALIFIED_NAME]
        for label, p in ing.nodes
        if label == CODE_SMELL and p[cs.KEY_NAME] == "console_log"
    ]
    assert len(qns) == 2, qns
    assert len(set(qns)) == 2, qns


def test_hardcoded_secret_ignores_empty_and_format_templates(tmp_path: Path) -> None:
    # empty strings and format/message templates (an f-string/.format {..}
    # placeholder or a printf %s/%d specifier) are not secrets. Real secret
    # literals that legitimately contain %, spaces, embedded-credential URLs
    # or SCREAMING_SNAKE shapes MUST still be caught (a security rule favours
    # recall). Lines 4-6 are the three false-negative shapes Greptile flagged.
    from codebase_rag.analyzers import FindingAnalyzer

    src = tmp_path / "s.py"
    src.write_text(
        'token = ""\n'
        'TOKEN_COUNT_FAILED = "Context token count failed: {error}"\n'
        'LOG_SECRET = "processed %s rows in %d ms"\n'
        'db_password = "postgres://admin:hardcoded-secret@db/prod"\n'
        'API_KEY = "sk-abcd1234efgh5678"\n'
        'GEN_TOKEN = "A1B2_C3D4_E5F6G7"\n',
        encoding="utf-8",
    )
    ing = _FakeIngestor()
    FindingAnalyzer(ing, tmp_path, resolve_capture(["+findings"])).analyze(
        {"proj.s": src}
    )
    secrets = sorted(
        p[cs.KEY_START_LINE]
        for label, p in ing.nodes
        if label == SECURITY_ISSUE and p[cs.KEY_NAME] == "hardcoded_secret"
    )
    assert secrets == [4, 5, 6], secrets


def test_tsx_files_get_findings(tmp_path: Path) -> None:
    from codebase_rag.analyzers import FindingAnalyzer
    from codebase_rag.analyzers.ast_grep_analyzer import load_finding_rules

    assert ".tsx" in load_finding_rules()
    src = tmp_path / "c.tsx"
    src.write_text(
        "const C = () => { console.log('x'); return null; };\n", encoding="utf-8"
    )
    ing = _FakeIngestor()
    FindingAnalyzer(ing, tmp_path, resolve_capture(["+findings"])).analyze(
        {"proj.c": src}
    )
    names = [p[cs.KEY_NAME] for label, p in ing.nodes if label == CODE_SMELL]
    assert "console_log" in names, names
