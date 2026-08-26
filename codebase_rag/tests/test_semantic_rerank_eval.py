# The with/without-reranking comparison for issue #385, criterion 3.
#
# `evals/semantic_search.py` already grades recall@k for natural-language
# queries against known-correct functions. This adds the reranked condition
# over the SAME cases and the SAME baseline ranking, so the two differ by
# exactly one variable and any recall difference is attributable to the
# reranker alone.
from __future__ import annotations

from itertools import permutations

from evals.semantic_search import (
    SemanticCase,
    reranked_semantic_ranking,
    score_semantic,
    score_semantic_mrr,
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

    # Asserted separately so a failure names which condition produced no rows;
    # a composite assertion here would report "one of them was empty".
    assert before.rows
    assert after.rows
    # ScoreRow is a TypedDict, so the fields are subscripts.
    assert before.rows[0]["label"] == after.rows[0]["label"]
    # Nothing is demoted out of top-k here, so equal recall is the CORRECT
    # answer rather than the metric failing to look. The earlier version of
    # this test asserted both were 1.0 and offered that as evidence the
    # comparison worked -- but a membership scorer returns 1.0 for every
    # permutation, so the assertion held whether or not the metric could see
    # order at all. That is the defect in #1474 written into its own guard.
    # `test_demoting_the_expected_answer_out_of_top_k_costs_recall` is what
    # distinguishes the two implementations; this one only fixes the scorer.
    assert before.rows[0]["recall"] == after.rows[0]["recall"]


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


# --- #1474: the metric must be able to see order ---------------------------
#
# A reranker returns a PERMUTATION of the candidate list. Membership is
# invariant under permutation, so a membership-based recall@k scores the
# baseline and the reranked condition identically for every input -- by
# construction, not by coincidence. The tests below are the ones that fail
# against that scorer; without them the fix could be deleted and every other
# test in this file would still pass.


def test_demoting_the_expected_answer_out_of_top_k_costs_recall() -> None:
    """The measurement #1474 exists for: position must change the score.

    Both rankings contain the expected answer, so a membership scorer returns
    1.0 for each and this assertion fails. Only a scorer that truncates to k
    before checking can tell the two apart.
    """
    cases = [SemanticCase("q", "want")]
    kept = {"q": ["want", "iso.a", "iso.c"]}
    demoted = {"q": ["iso.a", "iso.c", "want"]}

    kept_recall = score_semantic(cases, kept, k=2).rows[0]["recall"]
    demoted_recall = score_semantic(cases, demoted, k=2).rows[0]["recall"]

    assert kept_recall > demoted_recall, (
        f"recall@2 scored a demoted-out-of-top-k hit ({demoted_recall}) the same "
        f"as one held at rank 1 ({kept_recall}); the metric is order-insensitive "
        "and reranking cannot be measured with it"
    )
    assert demoted_recall == 0.0


def test_recall_at_k_is_not_invariant_across_permutations() -> None:
    """Pin the general property, not just the one pair above.

    Scoring every permutation of a fixed candidate set proves the metric
    responds to ORDER. A membership scorer produces one distinct value here;
    an order-sensitive one produces more than one.
    """
    cases = [SemanticCase("q", "want")]
    scores = {
        score_semantic(cases, {"q": list(perm)}, k=2).rows[0]["recall"]
        for perm in permutations(["iso.a", "want", "iso.c"])
    }

    assert len(scores) > 1, (
        f"every permutation scored the same ({scores}); recall@k discards the "
        "ordering that reranking exists to change"
    )


def test_reranking_that_demotes_the_answer_scores_worse_end_to_end() -> None:
    """Acceptance criterion 1, through the real reranker rather than by hand.

    `want` starts SECOND and is isolated. The hits around it form a connected
    cluster, so proximity reranking promotes them and pushes `want` out of the
    top k=2. That must cost recall, or the comparison on #385 is measuring
    nothing.

    Four hits rather than three, because positional similarity spreads over
    `len - 1`: with three the gap between neighbours is 0.5 and a maximally
    boosted last-place hit only TIES the isolated one, which stable sort then
    leaves in place. The fixture has to make the demoted hit lose outright,
    not tie.
    """
    cases = [SemanticCase("q", "want")]
    baseline = {"q": ["cl.a", "want", "cl.b", "cl.c"]}
    # Every hit except `want` is wired to the others, so all three outrank it.
    adjacency = {
        "cl.a": {"cl.b", "cl.c"},
        "cl.b": {"cl.a", "cl.c"},
        "cl.c": {"cl.a", "cl.b"},
    }

    reranked = reranked_semantic_ranking(baseline, adjacency, weight=1.0)
    assert reranked["q"][-1] == "want", reranked["q"]

    before = score_semantic(cases, baseline, k=2).rows[0]["recall"]
    after = score_semantic(cases, reranked, k=2).rows[0]["recall"]

    assert after < before, (
        f"reranking demoted the expected answer out of top-2 but recall did not "
        f"move ({before} -> {after})"
    )


def test_k_defaults_to_scoring_the_whole_ranking() -> None:
    """Omitting k must preserve the published numbers.

    The existing eval truncates upstream in `cgr_semantic_ranking`, so callers
    that already pass a top-k list must keep scoring exactly as before.
    """
    cases = [SemanticCase("q", "want")]
    ranking = {"q": ["iso.a", "iso.c", "want"]}

    assert score_semantic(cases, ranking).rows[0]["recall"] == 1.0
    assert score_semantic(cases, ranking, k=None).rows[0]["recall"] == 1.0


def test_mrr_rewards_a_higher_rank() -> None:
    """The order-sensitive instrument: position is the measurement.

    Unlike recall@k, MRR distinguishes rank 1 from rank 2 even when both are
    inside k, so it can measure a promotion that does not cross the cutoff.
    """
    cases = [SemanticCase("q", "want")]

    first = score_semantic_mrr(cases, {"q": ["want", "iso.a", "iso.c"]})
    second = score_semantic_mrr(cases, {"q": ["iso.a", "want", "iso.c"]})
    third = score_semantic_mrr(cases, {"q": ["iso.a", "iso.c", "want"]})

    assert first == 1.0
    assert second == 0.5
    assert third == 1 / 3
    assert first > second > third


def test_mrr_scores_a_missing_answer_zero() -> None:
    """An answer that never appears has no reciprocal rank."""
    cases = [SemanticCase("q", "want")]

    assert score_semantic_mrr(cases, {"q": ["iso.a", "iso.c"]}) == 0.0
    assert score_semantic_mrr(cases, {}) == 0.0


def test_mrr_respects_k() -> None:
    """A hit below the cutoff is not retrievable, so it scores zero."""
    cases = [SemanticCase("q", "want")]
    ranking = {"q": ["iso.a", "iso.c", "want"]}

    assert score_semantic_mrr(cases, ranking, k=2) == 0.0
    assert score_semantic_mrr(cases, ranking, k=3) == 1 / 3


def test_mrr_averages_across_cases() -> None:
    """The headline is a mean over cases, so one perfect and one missing is 0.5."""
    cases = [SemanticCase("q1", "a"), SemanticCase("q2", "b")]
    ranking = {"q1": ["a"], "q2": ["z"]}

    assert score_semantic_mrr(cases, ranking) == 0.5


def test_mrr_over_no_cases_is_zero() -> None:
    """No cases means no evidence, not a perfect score."""
    assert score_semantic_mrr([], {"q": ["a"]}) == 0.0
