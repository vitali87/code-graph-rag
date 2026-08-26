# The with/without-reranking comparison for issue #385, criterion 3.
#
# `evals/semantic_search.py` already grades recall@k for natural-language
# queries against known-correct functions. This adds the reranked condition
# over the SAME cases and the SAME baseline ranking, so the two differ by
# exactly one variable and any recall difference is attributable to the
# reranker alone.
from __future__ import annotations

from evals.semantic_search import (
    SemanticCase,
    reranked_semantic_ranking,
    score_semantic,
)


def test_reranking_promotes_a_clustered_hit_above_an_isolated_one() -> None:
    """The measurement has to be able to show a difference at all.

    The expected answer sits second in the baseline. It is connected to the
    third hit; the first is isolated. If reranking cannot move it up here, the
    comparison can never report anything but "no change" and criterion 3 is
    unmeasurable.
    """
    ranking = {"q": ["iso.a", "want.b", "want.c"]}
    adjacency = {"want.b": {"want.c"}, "want.c": {"want.b"}}

    reranked = reranked_semantic_ranking(ranking, adjacency, weight=1.0)

    assert reranked["q"][0] == "want.b", reranked["q"]


def test_reranking_leaves_an_unconnected_ranking_untouched() -> None:
    """The control: with no edges, the output must be the input.

    Without this, an implementation that shuffled arbitrarily would satisfy
    the test above and the comparison would measure noise.
    """
    ranking = {"q": ["a", "b", "c"]}

    assert reranked_semantic_ranking(ranking, {}, weight=1.0)["q"] == ["a", "b", "c"]


def test_a_single_hit_is_returned_unchanged() -> None:
    """One result has nothing to be proximate to."""
    assert reranked_semantic_ranking({"q": ["only"]}, {})["q"] == ["only"]


def test_the_comparison_scores_both_conditions_the_same_way() -> None:
    """Both conditions must go through the SAME scorer.

    Scoring them differently would make the numbers incomparable, which is the
    quiet way a benchmark ends up reporting a difference it did not measure.
    """
    cases = [SemanticCase("q", "want.b")]
    baseline = {"q": ["iso.a", "want.b"]}
    adjacency = {"want.b": {"want.z"}}

    before = score_semantic(cases, baseline)
    after = score_semantic(cases, reranked_semantic_ranking(baseline, adjacency))

    # Both find the expected qn in top-k, so recall is equal here; the point is
    # that both produce a comparable ScoreResult from one scorer.
    assert before.rows and after.rows
    # ScoreRow is a TypedDict, so the fields are subscripts.
    assert before.rows[0]["label"] == after.rows[0]["label"]
    assert before.rows[0]["recall"] == after.rows[0]["recall"] == 1.0


def test_an_edge_outside_the_result_set_does_not_count() -> None:
    """Proximity is in-set only, matching the reranker.

    A hit wired to half the codebase but to none of the other results is not
    more relevant to THIS query. Counting such edges would rank popular
    definitions highly for every query.
    """
    # `b` is SECOND and heavily connected -- but only to nodes outside the
    # result set. Counting those edges would promote it over `a`; counting
    # only in-set edges leaves the order alone.
    #
    # The connected node must start BELOW the one it would overtake, or the
    # test passes whether or not out-of-set edges are counted: boosting a hit
    # that is already first changes nothing.
    # THREE hits, so the positional gap between neighbours (0.5) is smaller
    # than the weight -- with two hits the gap is 1.0 and no boost can ever
    # overturn it, which would make this pass regardless of the fix.
    ranking = {"q": ["a", "b", "c"]}
    adjacency = {"a": set(), "b": {"x", "y", "z"}, "c": set()}

    assert reranked_semantic_ranking(ranking, adjacency, weight=1.0)["q"] == [
        "a",
        "b",
        "c",
    ]
