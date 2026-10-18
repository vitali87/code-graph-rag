"""The shared retrieval driver keeps each language's published outputs.

The per-language commands used to spell their own file names, diff keys and
row label; they are now derived from the spec's stem, so a typo in the
template would rename every language's results at once. Driven through
`run_retrieval_eval` with a stub oracle, so no toolchain is needed.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from evals import constants as ec
from evals.retrieval_eval import RETRIEVAL_SPECS, run_retrieval_eval
from evals.types_defs import CallOracle


@pytest.mark.parametrize("lang", list(ec.RetrievalLanguage))
def test_every_language_registers_a_spec(lang: ec.RetrievalLanguage) -> None:
    assert RETRIEVAL_SPECS[lang].lang == lang


def test_outputs_keep_their_per_language_names(tmp_path: Path) -> None:
    # An empty target gives cgr no edges, so the oracle's one edge is a miss
    # and both the row and its diff bucket are written.
    target = tmp_path / "src"
    target.mkdir()
    spec = RETRIEVAL_SPECS[ec.RetrievalLanguage.GO]._replace(
        available=lambda: True,
        run_oracle=lambda _target: CallOracle(
            {("main.go", "helper")}, frozenset({"helper"})
        ),
    )

    run_retrieval_eval(spec, target, "", tmp_path / "out")

    with (tmp_path / "out" / "go_retrieval_scores.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [(r["label"], r["fn"]) for r in rows] == [("graph", "1")]
    diff = json.loads(
        (tmp_path / "out" / "go_retrieval_diff.json").read_text(encoding="utf-8")
    )
    assert diff == {
        "go-retrieval:graph": {"missing": ["main.go -> helper"], "extra": []}
    }
