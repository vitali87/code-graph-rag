# The with/without-reranking comparison for issue #385, criterion 3.
#
# `evals/semantic_search.py` already grades recall@k for natural-language
# queries against known-correct functions. This adds the reranked condition
# over the SAME cases and the SAME baseline ranking, so the two differ by
# exactly one variable and any recall difference is attributable to the
# reranker alone.
from __future__ import annotations

from itertools import permutations
from pathlib import Path
from unittest import mock

import pytest
import typer

from evals import constants as ec
from evals.semantic_search import (
    SemanticCase,
    reranked_semantic_ranking,
    score_semantic,
    score_semantic_mrr,
)
from evals.types_defs import LocationStats, ScoreResult, ScoreRow


def test_reranking_promotes_a_clustered_hit_above_an_isolated_one() -> None:
    """The measurement has to be able to show a difference at all.

    The expected answer sits second in the baseline. It is connected to the
    third hit; the first is isolated. If reranking cannot move it up here, the
    comparison can never report anything but "no change" and criterion 3 is
    unmeasurable.
    """
    ranking = {"q": ["iso.a", "want.b", "want.c"]}
    adjacency = {"want.b": {"want.c": {"CALLS"}}, "want.c": {"want.b": {"CALLS"}}}

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
    adjacency = {"want.b": {"want.z": {"CALLS"}}}

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
    adjacency: dict[str, dict[str, set[str]]] = {
        "a": {},
        "b": {"x": {"CALLS"}, "y": {"CALLS"}, "z": {"CALLS"}},
        "c": {},
    }

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
        "cl.a": {"cl.b": {"CALLS"}, "cl.c": {"CALLS"}},
        "cl.b": {"cl.a": {"CALLS"}, "cl.c": {"CALLS"}},
        "cl.c": {"cl.a": {"CALLS"}, "cl.b": {"CALLS"}},
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


def test_the_eval_reranker_counts_distinct_types_not_neighbours() -> None:
    """The PRODUCTION eval path must weigh a multi-type pair more (#1477).

    `test_graph_rerank.py` compares two local mirrors of the two proximity
    models. That catches a divergence between the models but says nothing
    about `reranked_semantic_ranking` itself, so reverting THIS function to
    presence-only counting left every test in that module green. The guard has
    to run the shipped code.

    `mid` and `low` are both adjacent to one in-set neighbour, but `mid`'s edge
    carries two distinct types (an override that calls up). Counting
    neighbours scores them equally and stable sort keeps the baseline order;
    counting distinct types promotes `mid`.

    weight=2.0 rather than 1.0: `mid` sits last, so it starts 0.5 of
    positional similarity behind `low`, and its proximity advantage after
    normalisation is also 0.5. At weight 1.0 those cancel exactly and stable
    sort keeps `low` ahead -- the test would then pass for BOTH counting
    models, which is the defect this whole issue is about.
    """
    ranking = {"q": ["top", "low", "mid"]}
    adjacency: dict[str, dict[str, set[str]]] = {
        "mid": {"top": {"CALLS", "OVERRIDES"}},
        "top": {"mid": {"CALLS", "OVERRIDES"}},
        "low": {"top": {"CALLS"}},
    }

    reranked = reranked_semantic_ranking(ranking, adjacency, weight=2.0)

    assert reranked["q"].index("mid") < reranked["q"].index("low"), (
        f"{reranked['q']}: a pair joined by two distinct relationship types "
        "did not outrank one joined by a single type, so the eval is counting "
        "neighbours rather than distinct types"
    )


def test_eval_proximity_keeps_separating_above_two_types() -> None:
    """Degree is PROPORTIONAL to in-set connection, with no ceiling.

    The test above states only that a multi-type pair is weighed "more". That
    is satisfied by any implementation which distinguishes 1 from 2 and then
    stops -- capping the degree at 2 passes every other test in this file,
    because no other fixture contains a node above degree 2.

    An understated property is the default failure: a docstring explains why a
    test exists, and the minimal reason is usually narrower than the contract
    the code implements. A well-fixtured test for the narrow property guards
    less than the code does. Found by checking the stated property against the
    code's DEFINING behaviour rather than checking the fixture against the
    stated property.

    `hub` reaches three in-set neighbours; `rival` reaches one by two distinct
    types. Uncapped they are 3 and 2 and separate; capped at 2 they are equal
    and stable sort keeps the baseline order.
    """
    ranking = {"q": ["rival", "hub", "n1", "n2", "n3"]}
    adjacency: dict[str, dict[str, set[str]]] = {
        "hub": {"n1": {"CALLS"}, "n2": {"CALLS"}, "n3": {"CALLS"}},
        "n1": {"hub": {"CALLS"}, "rival": {"CALLS", "OVERRIDES"}},
        "n2": {"hub": {"CALLS"}},
        "n3": {"hub": {"CALLS"}},
        "rival": {"n1": {"CALLS", "OVERRIDES"}},
    }

    reranked = reranked_semantic_ranking(ranking, adjacency, weight=2.0)

    assert reranked["q"].index("hub") < reranked["q"].index("rival"), (
        f"{reranked['q']}: a node with in-set degree 3 did not outrank one "
        "with degree 2, so proximity stops distinguishing above two types "
        "rather than scaling with how connected a hit is"
    )


def test_a_negative_cutoff_is_rejected_rather_than_sliced() -> None:
    """`hits[:-1]` is "all but the last", not a top-k window.

    Left unguarded, a negative k reports a plausible-looking number from a
    nonsensical window -- the same class of quietly-wrong measurement this
    metric exists to prevent (CodeRabbit, #1478).
    """
    cases = [SemanticCase("q", "want")]
    ranking = {"q": ["iso.a", "want", "iso.c"]}

    for k in (-1, -3):
        with pytest.raises(ValueError, match="non-negative"):
            score_semantic(cases, ranking, k=k)
        with pytest.raises(ValueError, match="non-negative"):
            score_semantic_mrr(cases, ranking, k=k)


def test_a_zero_cutoff_retrieves_nothing() -> None:
    """k=0 is a valid empty window, distinct from an invalid negative one."""
    cases = [SemanticCase("q", "want")]
    ranking = {"q": ["want"]}

    assert score_semantic(cases, ranking, k=0).rows[0]["recall"] == 0.0
    assert score_semantic_mrr(cases, ranking, k=0) == 0.0


# --- the entry point must actually use the new measurements (#1478) ---------
#
# Greptile's P1 on #1478: adding a cutoff-aware scorer and an order-sensitive
# metric changes nothing if the eval's entry path still calls the default
# full-ranking recall. A capability nothing invokes is indistinguishable from
# one that was never added.


def test_the_cli_scores_both_conditions_at_the_cutoff() -> None:
    """`main` must pass its cutoff into scoring and score BOTH conditions.

    Asserted by capturing what the scorers were called with, rather than by
    reading the printed table: a table showing two rows proves the rows were
    rendered, not that the reranked condition was scored at the cutoff.
    """
    from evals import semantic_search

    seen_recall: list[int | None] = []
    seen_mrr: list[int | None] = []
    rankings: list[dict[str, list[str]]] = []

    def fake_ranking(
        target: object, project: str, queries: list[str], top_k: int
    ) -> dict[str, list[str]]:
        # Deeper than the cutoff, or reranking has nothing to demote from.
        assert top_k > ec.SEMANTIC_TOP_K, top_k
        return {query: ["a", "b", "c", "d"] for query in queries}

    def fake_score(
        cases: object, ranking: dict[str, list[str]], k: int | None = None
    ) -> ScoreResult:
        seen_recall.append(k)
        rankings.append(ranking)
        return ScoreResult(
            rows=[
                ScoreRow(
                    category="retrieval",
                    label=ec.SEMANTIC_LABEL,
                    tp=1,
                    fp=0,
                    fn=0,
                    precision=1.0,
                    recall=1.0,
                    f1=1.0,
                )
            ],
            location=LocationStats(0, 0, 0, 0.0, 0),
            diff={},
        )

    def fake_mrr(
        cases: object, ranking: dict[str, list[str]], k: int | None = None
    ) -> float:
        seen_mrr.append(k)
        return 1.0

    with mock.patch.multiple(
        semantic_search,
        cgr_semantic_ranking=fake_ranking,
        proximity_edges=lambda *_a, **_kw: {},
        score_semantic=fake_score,
        score_semantic_mrr=fake_mrr,
    ):
        semantic_search.main(target=Path("codebase_rag"), top_k=ec.SEMANTIC_TOP_K)

    # Both conditions, both metrics, all at the cutoff -- not the default None.
    assert seen_recall == [ec.SEMANTIC_TOP_K, ec.SEMANTIC_TOP_K], seen_recall
    assert seen_mrr == [ec.SEMANTIC_TOP_K, ec.SEMANTIC_TOP_K], seen_mrr
    assert len(rankings) == 2, "only one condition was scored"


def test_the_cli_rejects_a_retain_that_leaves_nothing_to_demote() -> None:
    """retain=1 truncates at the cutoff, reintroducing #1474 at the call site.

    With no candidates below k, no reranking can move a hit across the cutoff
    and the comparison reports no difference whatever the reranker does.
    """
    from evals import semantic_search

    with pytest.raises(typer.BadParameter, match="demote from"):
        semantic_search.main(target=Path("codebase_rag"), retain=1)

    with pytest.raises(typer.BadParameter, match="at least 1"):
        semantic_search.main(target=Path("codebase_rag"), top_k=0)
