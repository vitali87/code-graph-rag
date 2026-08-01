from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

STRATEGIES = (
    "class BatchingStrategyBase:\n    def apply(self, batch):\n        return batch\n"
)


def _run_rels(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str, str]]:
    # Build the graph for `files` and return (caller_qn, rel_type, callee_qn).
    parsers, queries = load_parsers()
    if "python" not in parsers:
        pytest.skip("python parser not available")
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock, repo_path=tmp_path, parsers=parsers, queries=queries
    ).run()
    return {
        (c.args[0][2], str(c.args[1]), c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
    }


def test_external_attribute_constructor_receiver_is_not_misbound(
    tmp_path: Path,
) -> None:
    # df is constructed by an external attribute constructor (pd.DataFrame); its
    # .apply must NOT rebind by bare name to an unrelated first-party apply. The
    # callback reference must still be emitted.
    files = {
        "strategies.py": STRATEGIES,
        "report.py": (
            "import pandas as pd\n\n"
            "def write_report(results):\n"
            "    df = pd.DataFrame(results)\n"
            "    def build_suggestion(row):\n"
            "        return row\n"
            "    df['suggestions'] = df.apply(build_suggestion, axis=1)\n"
            "    return df\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert not any(
        r == "CALLS" and b.endswith("BatchingStrategyBase.apply") for _, r, b in rels
    )
    # With the receiver judged external the callee resolves to nothing, so the
    # callback keep-alive edge comes from the external-callee path (CALLS) rather
    # than the first-party REFERENCES path; either keeps it reachable.
    assert any(
        r in ("CALLS", "REFERENCES")
        and a.endswith("write_report")
        and b.endswith("write_report.build_suggestion")
        for a, r, b in rels
    )


def test_first_party_attribute_constructor_receiver_resolves(tmp_path: Path) -> None:
    # The same attribute-constructor shape with a FIRST-PARTY class must resolve
    # the member call precisely (models.Helper() -> helper.process()).
    files = {
        "models.py": ("class Helper:\n    def process(self):\n        return 1\n"),
        "app.py": (
            "import models\n\n"
            "def run():\n"
            "    helper = models.Helper()\n"
            "    return helper.process()\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert any(
        a.endswith("app.run") and r == "CALLS" and b.endswith("models.Helper.process")
        for a, r, b in rels
    )
