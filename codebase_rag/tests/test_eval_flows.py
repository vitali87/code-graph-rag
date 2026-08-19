# FLOWS_TO ground truth stage 1 (issue #1190): the hand-labelled corpus in
# evals/flows_corpus must score exactly 1.0/1.0 per language. A regression on
# either side is a real finding: a lost edge (recall) means a flow-walk break
# like the Scala subscript_type mis-wiring this corpus caught on its first
# run; a new edge (precision) means the walk fabricated a flow the labeller
# did not sanction, and the label set must only grow by hand.
from __future__ import annotations

from evals.flow_ground_truth import load_expected, score_corpus


def test_corpus_covers_every_expected_language() -> None:
    assert set(load_expected()) == {
        "python",
        "javascript",
        "go",
        "java",
        "scala",
        "dart",
    }


def test_flows_match_the_hand_labels_exactly() -> None:
    scores = score_corpus()
    assert scores, "corpus produced no scores"
    for s in scores:
        assert s.precision == 1.0, (s.language, "precision", s)
        assert s.recall == 1.0, (s.language, "recall", s)
